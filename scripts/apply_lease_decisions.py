"""Apply the recorded review decisions to the reduced lease panel.

Inputs (all in data/):
  lease_panel_reduced.csv   output of reduce_panel.py (not changed)
  lease_decisions.csv       one row per decision: status, reason_code, deal_key, n_leases,
                            set_fields (JSON; "@col" copies another column), note
  lease_additions.csv       new rows split out of multi-lease documents
  lease_candidate_facts.csv extracted sentences, used to check every evidence sentence
Output: data/lease_panel.csv

Checks (the script stops if one fails):
  - every decision refers to an existing row; every addition has a new lease_id
  - each deal_key has exactly one canonical row (REVIEWED or NO_TERMS)
  - every REVIEWED/NO_TERMS value has an evidence sentence that is in the extracted
    sentences of the same document, and the value appears in that sentence

Run:  python scripts/apply_lease_decisions.py
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT  # noqa: E402
from verify import value_in_evidence  # noqa: E402

D = ROOT / "data"
NEW_COLS = ["mw_unstated", "mw_unstated_evidence", "signed_date", "signed_evidence", "n_leases",
            "credit_type", "first_rfs", "first_rfs_evidence"]
PAIRS = [("site", "site_evidence"), ("state", "state_evidence"), ("it_mw", "it_mw_evidence"), ("gross_mw", "gross_mw_evidence"), ("mw_unstated", "mw_unstated_evidence"),
         ("term_years", "term_evidence"), ("contract_value_musd", "contract_value_evidence"),
         ("avg_annual_revenue_musd", "annual_revenue_evidence"), ("credit_support", "credit_support_evidence"),
         ("signed_date", "signed_evidence"), ("first_rfs", "first_rfs_evidence")]
CANONICAL = ("REVIEWED", "NO_TERMS")


STATES = {"AL": "Alabama", "AR": "Arkansas", "AZ": "Arizona", "CA": "California", "CO": "Colorado", "GA": "Georgia",
          "IA": "Iowa", "IL": "Illinois", "IN": "Indiana", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
          "MI": "Michigan", "MN": "Minnesota", "MO": "Missouri", "MS": "Mississippi", "MT": "Montana", "NC": "North Carolina",
          "ND": "North Dakota", "NE": "Nebraska", "NM": "New Mexico", "NV": "Nevada", "NY": "New York", "OH": "Ohio",
          "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "SC": "South Carolina", "SD": "South Dakota",
          "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VA": "Virginia", "WA": "Washington", "WI": "Wisconsin",
          "WV": "West Virginia", "WY": "Wyoming", "SK": "Saskatchewan", "AB": "Alberta", "BC": "British Columbia",
          "QC": "Quebec", "ON": "Ontario", "MB": "Manitoba"}


def site_needles(val: str) -> list[str]:
    """Names that identify the site: the main name, and a place name in brackets (e.g. 'Ellendale')."""
    core = re.sub(r"\(.*?\)", "", val).split(";")[0].split(",")[0].strip()
    core = re.sub(r"\b(campus|site)\b", "", core, flags=re.I).strip()
    extra = [x.strip() for x in re.findall(r"\((.*?)\)", val) if re.fullmatch(r"[A-Z][a-z]+(?: [A-Z][a-z]+)*", x.strip())]
    return [n for n in [core, *extra] if len(n) >= 3]


def state_needles(val: str) -> list[str]:
    v = val.strip()
    return [STATES[v], f", {v}"] if v in STATES else [v]


def read(name):
    with open(D / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    rows = read("lease_panel_reduced.csv")
    fields = list(rows[0].keys()) + [c for c in NEW_COLS if c not in rows[0]]
    by_id = {r["lease_id"]: r for r in rows}
    for r in rows:
        for c in NEW_COLS:
            r.setdefault(c, "")
    errors = []

    for dcs in read("lease_decisions.csv"):
        r = by_id.get(dcs["lease_id"])
        if r is None:
            errors.append(f"decision for unknown row {dcs['lease_id']}")
            continue
        before = r["status"]
        sets = json.loads(dcs["set_fields"] or "{}")
        snapshot = dict(r)
        for col, val in sets.items():
            r[col] = snapshot[val[1:]] if isinstance(val, str) and val.startswith("@") else val
        r["status"], r["reason_code"] = dcs["status"], dcs["reason_code"]
        if dcs["deal_key"]:
            r["deal_key"] = dcs["deal_key"]
        if dcs["n_leases"]:
            r["n_leases"] = dcs["n_leases"]
        r["notes"] = (r["notes"] or "") + f" | decision: {before} -> {dcs['status']}" + \
            (f" ({dcs['reason_code']})" if dcs["reason_code"] else "") + (f": {dcs['note']}" if dcs["note"] else "")

    for a in read("lease_additions.csv"):
        if a["lease_id"] in by_id:
            errors.append(f"addition reuses lease_id {a['lease_id']}")
            continue
        new = {c: "" for c in fields}
        new.update({k: v for k, v in a.items() if k in new})
        rows.append(new)
        by_id[a["lease_id"]] = new

    # Enrichment: extra fields with evidence (data/lease_enrichment.csv).
    # evidence_needle selects the shortest extracted sentence of the row's document that contains it.
    sents0 = {}
    for f in read("lease_candidate_facts.csv"):
        sents0.setdefault(f["doc_id"], set()).add(f["evidence"])
    if (D / "lease_enrichment.csv").exists():
        for en in read("lease_enrichment.csv"):
            r = by_id.get(en["lease_id"])
            if r is None:
                errors.append(f"enrichment for unknown row {en['lease_id']}")
                continue
            if en["field"] == "notes":
                r["notes"] += " | " + en["value"].lstrip("+")
                continue
            r[en["field"]] = en["value"]
            if en["evidence_col"]:
                hits = sorted((x for x in sents0.get(r["doc_id"], ()) if en["evidence_needle"] in x), key=len)
                if not hits:
                    errors.append(f"{en['lease_id']}.{en['field']}: no sentence contains {en['evidence_needle']!r}")
                else:
                    r[en["evidence_col"]] = hits[0]

    # Site and state: text fields need evidence too. Find a sentence in the same document
    # that names the site (or the state); if none exists, clear the value and say so in the notes.
    sents = {}
    for f in read("lease_candidate_facts.csv"):
        sents.setdefault(f["doc_id"], set()).add(f["evidence"])
    for r in rows:
        if r["status"] not in CANONICAL:
            continue
        for col in ("site", "state"):
            val = (r.get(col) or "").strip()
            if not val or (r.get(f"{col}_evidence") or "").strip():
                continue
            needles = site_needles(val) if col == "site" else state_needles(val)
            hit = next((s_ for s_ in sorted(sents.get(r["doc_id"], ()), key=len)
                        if any(re.search(rf"\b{re.escape(n)}\b", s_) for n in needles)), None)
            if hit:
                r[f"{col}_evidence"] = hit
            else:
                r[col] = ""
                r["notes"] += f" | {col} '{val}' cleared: no sentence in this document names it"

    # One canonical row per deal_key
    canon = {}
    for r in rows:
        if r["status"] in CANONICAL:
            if not r["deal_key"]:
                errors.append(f"{r['lease_id']}: {r['status']} row has no deal_key")
            elif r["deal_key"] in canon:
                errors.append(f"deal_key {r['deal_key']} has two canonical rows: {canon[r['deal_key']]} and {r['lease_id']}")
            else:
                canon[r["deal_key"]] = r["lease_id"]

    # Evidence checks against the extracted sentences.
    # Rows from documents that are not in the candidate facts (added by hand from filings)
    # are listed here and checked by verify.py against the downloaded document.
    deferred = sorted({r["lease_id"] for r in rows if r["status"] in CANONICAL and r["doc_id"] not in sents})
    for r in rows:
        if r["status"] not in CANONICAL:
            continue
        for col, ev in PAIRS:
            v, e = (r.get(col) or "").strip(), (r.get(ev) or "").strip()
            if not v:
                continue
            if r["lease_id"] in deferred:
                if e and col != "first_rfs" and not value_in_evidence(v, e):
                    errors.append(f"{r['lease_id']}.{col}={v}: value not in its evidence sentence")
                if col == "first_rfs" and v[:4] not in e:
                    errors.append(f"{r['lease_id']}.first_rfs={v}: year not in its evidence sentence")
                if not e:
                    errors.append(f"{r['lease_id']}.{col}={v}: no evidence sentence")
                continue
            if not e:
                errors.append(f"{r['lease_id']}.{col}={v}: no evidence sentence")
            elif e not in sents.get(r["doc_id"], set()) and not any(e in s for s in sents.get(r["doc_id"], set())):
                errors.append(f"{r['lease_id']}.{col}: evidence not among the document's extracted sentences")
            elif col == "first_rfs" and v[:4] not in e and not (v[:4] == "2026" and "1H26" in e):
                errors.append(f"{r['lease_id']}.first_rfs={v}: year not in its evidence sentence")
            elif not value_in_evidence(v, e):
                errors.append(f"{r['lease_id']}.{col}={v}: value not in its evidence sentence")

    if errors:
        print("ERRORS:")
        for e in errors:
            print("  -", e)
        return 1
    rows.sort(key=lambda r: (int("".join(ch for ch in r["lease_id"][1:4] if ch.isdigit()) or 0), r["lease_id"]))
    with open(D / "lease_panel.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    count = {}
    for r in rows:
        k = r["status"] + (f" ({r['reason_code']})" if r["reason_code"] else "")
        count[k] = count.get(k, 0) + 1
    print(f"{len(rows)} rows -> data/lease_panel.csv")
    for k, v in sorted(count.items()):
        print(f"  {k:32s} {v}")
    print(f"leases (canonical rows): {len(canon)}")
    if deferred:
        print(f"checked against full documents by verify.py only (not in candidate facts): {', '.join(deferred)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
