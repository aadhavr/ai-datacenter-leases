"""Scrape candidate AI data center leases from SEC filings.

The script proposes; you confirm. It never writes a value into the panel as confirmed.

Steps (run all with `python scripts/leases_scrape.py all`):
  search    SEC full-text search (efts.sec.gov) for each query in config.yaml -> reports/lease_search_hits.csv
  fetch     download each relevant filing document -> raw/leases/docs/, hashes in raw/leases/manifest.csv
  extract   find MW, $, term and tenant mentions, each with its sentence -> reports/lease_candidate_facts.csv
            and a relevance summary per document -> reports/lease_candidate_docs.csv
  template  add one UNREVIEWED row per new relevant document to data/lease_panel.csv,
            pre-filled with the first candidate of each kind and its evidence sentence

Then open data/lease_panel.csv, check each value against its evidence and the source,
and set status to REVIEWED (or REJECTED). Only REVIEWED rows go into the analysis.

Network: SEC requires the User-Agent in config.yaml and max 10 requests per second.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_config, path  # noqa: E402

EFTS = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{fname}"

PANEL_FIELDS = [
    "lease_id", "status", "landlord", "tenant", "site", "state", "announce_date",
    "it_mw", "it_mw_evidence", "gross_mw", "gross_mw_evidence",
    "term_years", "term_evidence", "contract_value_musd", "contract_value_evidence",
    "avg_annual_revenue_musd", "annual_revenue_evidence",
    "power_paid_by", "power_evidence", "fitout_paid_by", "fitout_evidence",
    "credit_support", "credit_support_evidence", "rfs_date", "rfs_evidence",
    "doc_id", "url", "notes", "reason_code", "deal_key", "site_evidence", "state_evidence",
    "mw_unstated", "mw_unstated_evidence", "signed_date", "signed_evidence", "n_leases",
]

WORDNUM = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
WORDNUM.update({"twenty-five": 25, "thirty": 30})

RE_MW = re.compile(r"(?P<q>up to|approximately|approx\.|about|more than|over|nearly|~)?\s*(?P<v>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?P<u>MW|megawatts?|GW|gigawatts?)\b", re.I)
RE_USD = re.compile(r"(?P<q>up to|approximately|approx\.|about|more than|over|nearly|~)?\s*(?:US)?\$\s?(?P<v>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?P<u>billion|million|bn|B|M)\b\+?", re.I)
RE_TERM = re.compile(r"\b(?P<v>\d{1,2}|" + "|".join(sorted(WORDNUM, key=len, reverse=True)) + r")[- ](?:year|yr)\b", re.I)
RE_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(•\-])")


# ---------------------------------------------------------------- helpers
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", ". ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    t = html.unescape(raw).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


def sentences(text: str) -> list[str]:
    parts = RE_SENT.split(text)
    out = []
    for p in parts:
        p = p.strip(" .")
        if 20 <= len(p) <= 800:
            out.append(p)
        elif len(p) > 800:  # very long run: cut on semicolons
            out.extend(s.strip() for s in p.split(";") if len(s.strip()) >= 20)
    return out


def num(v: str) -> float:
    return float(v.replace(",", ""))


def to_mw(v: str, unit: str) -> float:
    x = num(v)
    return x * 1000 if unit.lower().startswith(("gw", "giga")) else x


def to_musd(v: str, unit: str) -> float:
    x = num(v)
    return x * 1000 if unit.lower() in ("billion", "bn", "b") else x


def mw_basis(sent: str, start: int, end: int) -> str:
    """Classify an MW mention by the words next to it."""
    win = sent[max(0, start - 120): end + 60].lower()
    if "critical it" in win or "it load" in win or "it capacity" in win or "billable" in win or "leasable" in win:
        return "it"
    if "gross" in win or "utility" in win or "interconnect" in win or "grid" in win:
        return "gross"
    return "unspecified"


def usd_kind(sent: str, start: int, end: int) -> str:
    win = sent[max(0, start - 80): end + 80].lower()
    if "annual" in win or "per year" in win or "per annum" in win or "run rate" in win or "run-rate" in win:
        return "annual_revenue"
    if "contract value" in win or "contracted revenue" in win or "lease revenue" in win or "revenue potential" in win or "over the term" in win or "total" in win:
        return "contract_value"
    if "financing" in win or "loan" in win or "notes" in win or "facility" in win or "capex" in win or "capital expenditure" in win:
        return "financing_or_capex"
    return "usd_other"


def flags(sent: str) -> dict:
    s = sent.lower()
    return {
        "mentions_power_cost": any(k in s for k in ("pass-through", "pass through", "pays for power", "power and utilities", "power costs")),
        "mentions_fitout": any(k in s for k in ("fit-out", "fit out", "build out", "build-out", "prepaid", "funded by")),
        "mentions_credit_support": any(k in s for k in ("backstop", "guarantee", "guaranteed", "credit support", "investment grade", "investment-grade")),
        "mentions_rfs": any(k in s for k in ("ready for service", "rent commencement", "commence", "energized", "delivered")),
    }


@dataclass
class Fact:
    doc_id: str
    kind: str
    value: float | str
    unit: str
    qualifier: str
    basis: str
    evidence: str


def extract_facts(doc_id: str, text: str, tenants: list[str]) -> list[Fact]:
    facts: list[Fact] = []
    for s in sentences(text):
        for m in RE_MW.finditer(s):
            basis = mw_basis(s, m.start(), m.end())
            facts.append(Fact(doc_id, f"mw_{basis}", to_mw(m["v"], m["u"]), "MW", (m["q"] or "").strip(), basis, s))
        for m in RE_USD.finditer(s):
            facts.append(Fact(doc_id, usd_kind(s, m.start(), m.end()), to_musd(m["v"], m["u"]), "$M", (m["q"] or "").strip(), "", s))
        for m in RE_TERM.finditer(s):
            v = m["v"].lower()
            yrs = float(WORDNUM[v]) if v in WORDNUM else float(v)
            if 3 <= yrs <= 30:
                facts.append(Fact(doc_id, "term_years", yrs, "years", "", "", s))
        for t in tenants:
            if re.search(rf"\b{re.escape(t)}\b", s):
                facts.append(Fact(doc_id, "tenant", t, "", "", "", s))
        for k, v in flags(s).items():
            if v:
                facts.append(Fact(doc_id, k.removeprefix("mentions_"), 1, "flag", "", "", s))
    return facts


def score_doc(facts: list[Fact]) -> int:
    kinds = {f.kind for f in facts}
    sc = 0
    sc += 2 if "mw_it" in kinds else (1 if kinds & {"mw_gross", "mw_unspecified"} else 0)
    sc += 1 if "tenant" in kinds else 0
    sc += 1 if "term_years" in kinds else 0
    sc += 1 if kinds & {"contract_value", "annual_revenue"} else 0
    lease_words = sum(1 for f in facts if re.search(r"\blease|colocation|license fee", f.evidence, re.I))
    sc += 1 if lease_words else 0
    return sc


# ---------------------------------------------------------------- network steps
class Sec:
    def __init__(self, ua: str):
        import requests  # local import so tests run without network
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        self.last = 0.0

    def get(self, url, **kw):
        wait = 0.15 - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        r = self.s.get(url, timeout=60, **kw)
        self.last = time.time()
        return r


def parse_efts(js: dict) -> list[dict]:
    """Turn one EFTS response into rows. Raises ValueError if the shape is not as expected."""
    try:
        hits = js["hits"]["hits"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"unexpected EFTS response keys: {list(js)[:10]}") from e
    rows = []
    for h in hits:
        src = h.get("_source", {})
        _id = h.get("_id", "")
        adsh, _, fname = _id.partition(":")
        ciks = src.get("ciks") or [""]
        names = src.get("display_names") or [""]
        rows.append({
            "adsh": adsh or src.get("adsh", ""), "file_name": fname, "cik": str(int(ciks[0])) if ciks[0] else "",
            "company": re.sub(r"\s*\(.*$", "", names[0]).strip(), "form": src.get("form", "") or src.get("root_form", ""),
            "file_type": src.get("file_type", ""), "file_date": src.get("file_date", ""),
            "description": src.get("file_description", "") or "",
        })
    return rows


def doc_url(row: dict) -> str:
    return ARCHIVE.format(cik=row["cik"], adsh=row["adsh"].replace("-", ""), fname=row["file_name"])


def doc_id_for(row: dict) -> str:
    return f"L{row['adsh'].replace('-', '')}_{Path(row['file_name']).stem}"[:60]


def step_search(cfg, sec: Sec) -> Path:
    L = cfg["leases"]
    rdir = path(cfg, "reports")
    cache = path(cfg, "raw") / "leases" / "search"
    cache.mkdir(parents=True, exist_ok=True)
    seen, rows = set(), []
    for q in L["queries"]:
        for page in range(L.get("max_pages_per_query", 10)):
            params = {"q": q, "dateRange": "custom", "startdt": str(L["start"]), "enddt": str(L["end"]),
                      "forms": ",".join(L["forms"]), "from": page * 100}
            key = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
            cf = cache / f"{key}.json"
            if cf.exists():
                js = json.loads(cf.read_text(encoding="utf-8"))
            else:
                r = sec.get(EFTS, params=params)
                if r.status_code != 200:
                    print(f"  search failed ({r.status_code}) for {q!r} page {page}")
                    break
                js = r.json()
                cf.write_text(json.dumps(js), encoding="utf-8")
            batch = parse_efts(js)
            for row in batch:
                k = (row["adsh"], row["file_name"])
                if k in seen or not row["file_name"]:
                    continue
                seen.add(k)
                row["query"] = q
                row["url"] = doc_url(row)
                row["doc_id"] = doc_id_for(row)
                rows.append(row)
            total = js.get("hits", {}).get("total", {}).get("value", 0)
            print(f"  {q!r} page {page}: {len(batch)} hits (total {total})")
            if len(batch) < 100:
                break
    keep = [r for r in rows if not L.get("file_types") or r["file_type"] in L["file_types"]]
    out = rdir / "lease_search_hits.csv"
    rdir.mkdir(parents=True, exist_ok=True)
    fields = ["doc_id", "company", "cik", "form", "file_type", "file_date", "description", "adsh", "file_name", "url", "query"]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(sorted(keep, key=lambda r: (r["company"], r["file_date"])))
    print(f"search: {len(rows)} unique documents, {len(keep)} after file-type filter -> {out}")
    return out


def read_rows(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def step_fetch(cfg, sec: Sec):
    hits = read_rows(path(cfg, "reports") / "lease_search_hits.csv")
    # Also fetch every document that the panel uses (canonical rows and their duplicates),
    # so verification works even without the search report.
    panel = read_rows(ROOT / "data" / "lease_panel.csv")
    have_ids = {h["doc_id"] for h in hits}
    for r in panel:
        needed = r["status"] in ("REVIEWED", "NO_TERMS") or r.get("reason_code") == "duplicate"
        if needed and r.get("url") and r["doc_id"] not in have_ids:
            hits.append({"doc_id": r["doc_id"], "url": r["url"]})
            have_ids.add(r["doc_id"])
    if not hits:
        print("fetch: nothing to download (no search report and no panel rows with URLs)")
    ddir = path(cfg, "raw") / "leases" / "docs"
    ddir.mkdir(parents=True, exist_ok=True)
    mpath = path(cfg, "raw") / "leases" / "manifest.csv"
    manifest = {r["doc_id"]: r for r in read_rows(mpath)}
    n_new = 0
    print(f"fetch: {len(hits)} documents listed; downloading the ones not in {ddir}")
    for h in hits:
        out = ddir / f"{h['doc_id']}.html"
        if out.exists():
            continue
        r = sec.get(h["url"])
        if r.status_code != 200:
            print(f"  {h['doc_id']}: HTTP {r.status_code}")
            continue
        out.write_bytes(r.content)
        manifest[h["doc_id"]] = {"doc_id": h["doc_id"], "url": h["url"], "sha256": sha256_bytes(r.content),
                                 "bytes": len(r.content), "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        n_new += 1
    with open(mpath, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["doc_id", "url", "sha256", "bytes", "fetched_at_utc"], lineterminator="\n")
        w.writeheader()
        w.writerows(manifest.values())
    print(f"fetch: {n_new} new documents; {len(manifest)} in manifest")


def step_extract(cfg):
    L = cfg["leases"]
    hits = {h["doc_id"]: h for h in read_rows(path(cfg, "reports") / "lease_search_hits.csv")}
    ddir = path(cfg, "raw") / "leases" / "docs"
    all_facts, docs = [], []
    for p in sorted(ddir.glob("*.html")):
        did = p.stem
        text = html_to_text(p.read_text(encoding="utf-8", errors="ignore"))
        facts = extract_facts(did, text, L["tenants"])
        sc = score_doc(facts)
        h = hits.get(did, {})
        kinds = {}
        for f in facts:
            kinds.setdefault(f.kind, []).append(f)
        docs.append({
            "doc_id": did, "score": sc, "company": h.get("company", ""), "file_date": h.get("file_date", ""),
            "form": h.get("form", ""), "tenants": ";".join(sorted({str(f.value) for f in kinds.get("tenant", [])})),
            "mw_it": ";".join(f"{f.qualifier} {f.value:g}".strip() for f in kinds.get("mw_it", [])[:6]),
            "mw_gross": ";".join(f"{f.qualifier} {f.value:g}".strip() for f in kinds.get("mw_gross", [])[:6]),
            "mw_unspecified": ";".join(f"{f.qualifier} {f.value:g}".strip() for f in kinds.get("mw_unspecified", [])[:6]),
            "terms": ";".join(sorted({f"{f.value:g}" for f in kinds.get("term_years", [])})),
            "contract_value_musd": ";".join(f"{f.value:g}" for f in kinds.get("contract_value", [])[:6]),
            "annual_revenue_musd": ";".join(f"{f.value:g}" for f in kinds.get("annual_revenue", [])[:6]),
            "url": h.get("url", ""),
        })
        if sc >= L["min_score"]:
            all_facts.extend(facts)
    rdir = path(cfg, "reports")
    with open(rdir / "lease_candidate_docs.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(docs[0].keys()) if docs else ["doc_id"], lineterminator="\n")
        w.writeheader()
        w.writerows(sorted(docs, key=lambda d: (-d["score"], d["company"], d["file_date"])))
    with open(rdir / "lease_candidate_facts.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["doc_id", "kind", "value", "unit", "qualifier", "basis", "evidence"], lineterminator="\n")
        w.writeheader()
        for f in all_facts:
            w.writerow(f.__dict__)
    rel = sum(1 for d in docs if d["score"] >= L["min_score"])
    print(f"extract: {len(docs)} documents, {rel} with score >= {L['min_score']}, {len(all_facts)} candidate facts")
    return docs, all_facts


def first(facts, kind):
    return next((f for f in facts if f.kind == kind), None)


def step_template(cfg, docs=None, facts=None):
    """Append one UNREVIEWED row per relevant document that is not yet in the panel."""
    L = cfg["leases"]
    if docs is None:
        docs, facts = step_extract(cfg)
    panel_p = ROOT / "data" / "lease_panel.csv"
    panel = read_rows(panel_p)
    have = {r["doc_id"] for r in panel}
    by_doc = {}
    for f in facts:
        by_doc.setdefault(f.doc_id, []).append(f)
    n = 0
    next_id = 1 + max([int(r["lease_id"][1:]) for r in panel if r["lease_id"][1:].isdigit()] or [0])
    for d in docs:
        if d["score"] < L["min_score"] or d["doc_id"] in have:
            continue
        fs = by_doc.get(d["doc_id"], [])
        row = {k: "" for k in PANEL_FIELDS}
        row.update({"lease_id": f"P{next_id:03d}", "status": "UNREVIEWED", "landlord": d["company"],
                    "announce_date": d["file_date"], "doc_id": d["doc_id"], "url": d["url"],
                    "notes": "Pre-filled by script: CHECK EVERY VALUE. Other candidates in reports/lease_candidate_facts.csv"})
        for kind, col, ev in (("mw_it", "it_mw", "it_mw_evidence"), ("mw_gross", "gross_mw", "gross_mw_evidence"),
                              ("term_years", "term_years", "term_evidence"), ("contract_value", "contract_value_musd", "contract_value_evidence"),
                              ("annual_revenue", "avg_annual_revenue_musd", "annual_revenue_evidence")):
            f = first(fs, kind)
            if f:
                row[col] = f"{f.value:g}"
                row[ev] = f.evidence[:400]
        t = first(fs, "tenant")
        if t:
            row["tenant"] = t.value
        for kind, ev in (("power_cost", "power_evidence"), ("fitout", "fitout_evidence"),
                         ("credit_support", "credit_support_evidence"), ("rfs", "rfs_evidence")):
            f = first(fs, kind)
            if f:
                row[ev] = f.evidence[:400]
        panel.append(row)
        next_id += 1
        n += 1
    with open(panel_p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PANEL_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(panel)
    print(f"template: {n} new UNREVIEWED rows -> {panel_p} (total {len(panel)})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["search", "fetch", "extract", "template", "all"])
    a = ap.parse_args()
    cfg = load_config()
    if a.step in ("search", "fetch", "all"):
        if "example.com" in cfg["sec_user_agent"]:
            print("Set sec_user_agent in config.yaml (name and email) first.")
            return 2
        sec = Sec(cfg["sec_user_agent"])
    if a.step in ("search", "all"):
        step_search(cfg, sec)
    if a.step in ("fetch", "all"):
        step_fetch(cfg, sec)
    if a.step == "extract":
        step_extract(cfg)
    if a.step in ("template", "all"):
        step_template(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
