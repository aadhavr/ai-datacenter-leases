"""Verify the data files against the archived sources in raw/.

Three checks:
 1. Integrity: each raw file still has the SHA-256 in raw/manifest.csv.
 2. Evidence: each row in kpis.csv has an evidence phrase. The phrase must appear in
    the archived source document (after normalization: lower case, only a-z, 0-9 and '.').
 3. XBRL: each GAAP line in reported.csv and balance_sheet.csv is compared with the
    SEC company facts (raw/xbrl/companyfacts.json), matched on period dates.
    A 3-month Q4 value is checked as FY minus 9M when no 3-month fact exists.

Output: reports/verification.csv. Exit code 1 if any FAIL (or any not-verified row with --strict).

Run:  python scripts/verify.py [--strict]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_config, load_data, path  # noqa: E402


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return html.unescape(raw)


def normalize(s: str) -> str:
    """Lower case, keep letters, digits and decimal points only.

    A period that is not between two digits is removed, so separators that one text
    extractor adds at paragraph or bullet breaks do not stop a match. Decimals stay: 1.5 is not 15.
    """
    s = s.lower().replace("\u200b", "")
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", "", s)
    return re.sub(r"[^a-z0-9.]", "", s)


def doc_text(p: Path) -> str | None:
    if p.suffix == ".pdf":
        try:
            from pypdf import PdfReader  # optional dependency
        except ImportError:
            return None
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(p)).pages)
    return html_to_text(p.read_text(encoding="utf-8", errors="ignore"))


def find_doc(raw: Path, doc_id: str) -> Path | None:
    for folder in (raw, raw / "leases" / "docs"):
        for e in (".html", ".htm", ".pdf", ".txt"):
            p = folder / f"{doc_id}{e}"
            if p.exists():
                return p
    return None


def value_in_evidence(value: str, evidence: str) -> bool:
    """True if the number (or its GW/$B form) appears in the evidence sentence."""
    try:
        v = float(value)
    except ValueError:
        return True
    ev = evidence.replace(",", "")
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
             11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen", 16: "sixteen", 17: "seventeen",
             18: "eighteen", 19: "nineteen", 20: "twenty", 25: "twenty-five", 30: "thirty"}
    if v.is_integer() and int(v) in words and re.search(rf"\b{words[int(v)]}\b", ev, re.I):
        return True
    forms = {f"{v:g}", f"{v / 1000:g}"}
    if (v * 1e6).is_integer():
        forms.add(str(int(v * 1e6)))  # $M value written in full dollars, e.g. $800,000,000
    if v.is_integer():
        forms.add(str(int(v)))
    return any(re.search(rf"(?<![\d.]){re.escape(x)}(?![\d])", ev) for x in forms)


# ---------------------------------------------------------------- XBRL
def load_facts(p: Path) -> dict | None:
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def concept_series(facts: dict, spec: dict) -> list[tuple[str, list]]:
    """Return [(concept_name, usd_fact_list)] for candidates, then label_search matches."""
    out = []
    tax = facts.get("facts", {})
    for cand in spec.get("candidates", []) or []:
        ns, name = (cand.split(":", 1) if ":" in cand else ("us-gaap", cand))
        c = tax.get(ns, {}).get(name)
        if c and "USD" in c.get("units", {}):
            out.append((f"{ns}:{name}", c["units"]["USD"]))
    if spec.get("label_search"):
        needle = spec["label_search"].lower()
        for ns, concepts in tax.items():
            for name, c in concepts.items():
                if needle in (c.get("label") or "").lower() and "USD" in c.get("units", {}):
                    out.append((f"{ns}:{name}", c["units"]["USD"]))
    return out


def pick(facts_list, start: str | None, end: str):
    """Latest-filed fact for exact start/end (start None = instant)."""
    hits = [f for f in facts_list if f.get("end") == end and (f.get("start") == start if start else "start" not in f)]
    if not hits:
        return None
    hits.sort(key=lambda f: f.get("filed", ""))
    return hits[-1]


def compare(value: float, fact_val: float, mode: str, tol: float) -> bool:
    x = fact_val / 1e6
    if mode == "abs":
        return abs(abs(x) - abs(value)) <= tol
    return abs(x - value) <= tol


def xbrl_duration(series, start: dt.date, end: dt.date):
    f = pick(series, start.isoformat(), end.isoformat())
    if f:
        return f["val"], f"{f.get('form')} filed {f.get('filed')}"
    # Q4 3-month value: FY minus 9M
    if end.month == 12 and (end - start).days < 100:
        fy = pick(series, dt.date(end.year, 1, 1).isoformat(), end.isoformat())
        nine = pick(series, dt.date(end.year, 1, 1).isoformat(), dt.date(end.year, 9, 30).isoformat())
        if fy and nine:
            return fy["val"] - nine["val"], f"derived FY({fy.get('form')}) - 9M({nine.get('form')})"
    return None, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="fail when a row cannot be verified")
    args = ap.parse_args()
    cfg, data = load_config(), load_data()
    raw = path(cfg, "raw")
    tol = float(cfg.get("xbrl_tolerance_musd", 1.0))
    xmap = yaml.safe_load((ROOT / "xbrl_map.yaml").read_text(encoding="utf-8"))
    rows = []

    def add(kind, key, period, doc, status, detail=""):
        rows.append({"kind": kind, "key": key, "period": period, "doc_id": doc, "status": status, "detail": detail})

    # 1. Integrity
    mpath = raw / "manifest.csv"
    if mpath.exists():
        with open(mpath, newline="", encoding="utf-8") as f:
            for m in csv.DictReader(f):
                lf = m.get("local_file") or ""
                p = raw / lf if lf else None
                if not p or not p.exists():
                    add("integrity", m["doc_id"], "", m["doc_id"], "NO_FILE", m.get("error", ""))
                elif m.get("sha256") and sha256(p) != m["sha256"]:
                    add("integrity", m["doc_id"], "", m["doc_id"], "FAIL", "hash differs from manifest")
                else:
                    add("integrity", m["doc_id"], "", m["doc_id"], "PASS", m.get("method", ""))
    else:
        add("integrity", "manifest", "", "", "NO_FILE", "run scripts/fetch_sources.py")

    # 2. Evidence text (every data file that has doc_id and evidence columns)
    cache = {}
    for fname in cfg.get("evidence_files", ["kpis.csv"]):
        with open(ROOT / "data" / fname, newline="", encoding="utf-8") as fh:
            ev_rows = list(csv.DictReader(fh))
        for r in ev_rows:
            key = next((r[k] for k in ("metric", "key", "company", "deal_id", "series") if k in r), "")
            period = next((r[k] for k in ("quarter", "as_of", "term") if k in r and r[k]), "")
            did, ev = r["doc_id"], r["evidence"]
            kind = f"evidence:{fname.removesuffix('.csv')}"
            p = find_doc(raw, did)
            if not p:
                add(kind, key, period, did, "NO_FILE", ev)
                continue
            if did not in cache:
                t = doc_text(p)
                cache[did] = normalize(t) if t is not None else None
            txt = cache[did]
            if txt is None:
                add(kind, key, period, did, "NO_TEXT", "install pypdf for PDF sources")
            elif normalize(ev) in txt:
                add(kind, key, period, did, "PASS", ev)
            else:
                add(kind, key, period, did, "FAIL", f"phrase not found: {ev}")

    # 2b. Lease panel: every REVIEWED value must have an evidence sentence found in its source,
    #     and the value must appear in that sentence.
    lp = ROOT / "data" / "lease_panel.csv"
    if lp.exists():
        with open(lp, newline="", encoding="utf-8") as fh:
            panel = [r for r in csv.DictReader(fh) if r["status"] in ("REVIEWED", "NO_TERMS")]
        pairs = [("it_mw", "it_mw_evidence"), ("gross_mw", "gross_mw_evidence"), ("term_years", "term_evidence"),
                 ("contract_value_musd", "contract_value_evidence"), ("avg_annual_revenue_musd", "annual_revenue_evidence"),
                 ("power_paid_by", "power_evidence"), ("fitout_paid_by", "fitout_evidence"),
                 ("credit_support", "credit_support_evidence"), ("rfs_date", "rfs_evidence"),
                 ("site", "site_evidence"), ("state", "state_evidence"),
                 ("mw_unstated", "mw_unstated_evidence"), ("signed_date", "signed_evidence"),
                 ("first_rfs", "first_rfs_evidence")]
        for r in panel:
            p = find_doc(raw, r["doc_id"])
            txt = None
            if p:
                if r["doc_id"] not in cache:
                    t = doc_text(p)
                    cache[r["doc_id"]] = normalize(t) if t is not None else None
                txt = cache[r["doc_id"]]
            for col, evcol in pairs:
                val, ev = r.get(col, "").strip(), r.get(evcol, "").strip()
                if not val:
                    continue
                if not ev:
                    add("lease_panel", r["lease_id"], col, r["doc_id"], "FAIL", "value has no evidence sentence")
                elif txt is None:
                    add("lease_panel", r["lease_id"], col, r["doc_id"], "NO_FILE", ev[:120])
                elif normalize(ev) not in txt:
                    add("lease_panel", r["lease_id"], col, r["doc_id"], "FAIL", f"evidence not in source: {ev[:120]}")
                elif not value_in_evidence(val, ev):
                    add("lease_panel", r["lease_id"], col, r["doc_id"], "FAIL", f"value {val} not in evidence: {ev[:120]}")
                else:
                    add("lease_panel", r["lease_id"], col, r["doc_id"], "PASS", ev[:120])

    # 3. XBRL
    facts = load_facts(raw / "xbrl" / "companyfacts.json")
    skip = set(xmap.get("skip", []))
    blocks = {b["label"]: b for b in cfg["blocks"]}
    for r in data["reported_rows"]:
        item, blk = r["item"], r["block"]
        if item in skip:
            add("xbrl", item, blk, r["doc_id"], "SKIP", "non-GAAP; check against press-release table")
            continue
        spec = xmap["items"].get(item)
        if facts is None or spec is None:
            add("xbrl", item, blk, r["doc_id"], "NO_XBRL", "no company facts file" if facts is None else "no mapping")
            continue
        b = blocks[blk]
        found = False
        for cname, series in concept_series(facts, spec):
            val, how = xbrl_duration(series, b["start"], b["end"])
            if val is None:
                continue
            found = True
            ok = compare(float(r["value_musd"]), val, spec.get("compare", "abs"), tol)
            add("xbrl", item, blk, r["doc_id"], "PASS" if ok else "FAIL", f"{cname} = {val / 1e6:,.1f} ({how})")
            break
        if not found:
            add("xbrl", item, blk, r["doc_id"], "NOT_FOUND", "no fact with these dates")
    for r in data["bs_rows"]:
        spec = xmap.get("balance_sheet", {}).get(r["item"])
        if spec is None:
            add("xbrl", r["item"], r["as_of"], r["doc_id"], "NO_XBRL", "no mapping (check by hand)")
            continue
        if facts is None:
            add("xbrl", r["item"], r["as_of"], r["doc_id"], "NO_XBRL", "no company facts file")
            continue
        found = False
        for cname, series in concept_series(facts, spec):
            f = pick(series, None, r["as_of"])
            if not f:
                continue
            found = True
            ok = compare(float(r["value_musd"]), f["val"], spec.get("compare", "abs"), tol)
            add("xbrl", r["item"], r["as_of"], r["doc_id"], "PASS" if ok else "FAIL", f"{cname} = {f['val'] / 1e6:,.1f} ({f.get('form')} filed {f.get('filed')})")
            break
        if not found:
            add("xbrl", r["item"], r["as_of"], r["doc_id"], "NOT_FOUND", "no fact with this date")

    rdir = path(cfg, "reports")
    rdir.mkdir(parents=True, exist_ok=True)
    with open(rdir / "verification.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["kind", "key", "period", "doc_id", "status", "detail"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    summary = {}
    for r in rows:
        summary.setdefault(r["kind"], {}).setdefault(r["status"], 0)
        summary[r["kind"]][r["status"]] += 1
    for kind, counts in summary.items():
        print(f"{kind:10s} " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    fails = [r for r in rows if r["status"] == "FAIL"]
    for r in fails:
        print(f"  FAIL {r['kind']} {r['key']} {r['period']} [{r['doc_id']}]: {r['detail']}")
    unverified = [r for r in rows if r["status"] in ("NO_FILE", "NO_TEXT", "NOT_FOUND", "NO_XBRL")]
    nofile = sum(r["status"] == "NO_FILE" for r in rows)
    if nofile:
        print(f"WARNING: {nofile} rows were NOT checked because the source file is missing (NO_FILE).")
        print("         Run: python3 scripts/fetch_sources.py  and  python3 scripts/leases_scrape.py fetch")
    print(f"report: {rdir / 'verification.csv'}")
    if fails or (args.strict and unverified):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
