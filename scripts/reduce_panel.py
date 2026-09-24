"""Reduce the reviewed candidate file to the price panel.

Input:  the reviewed file from the handoff (default: data/lease_panel_reviewed.csv)
Output: data/lease_panel.csv (same rows, simplified statuses) and reports/lease_confirm_queue.csv

Four statuses:
  REVIEWED  signed lease with disclosed price terms (IT or gross MW, and contract value + term or annual revenue)
  NO_TERMS  signed lease, but no price terms disclosed (counted, for the selection discussion)
  CONFIRM   needs a human decision (for example a row that combines several leases and must be split)
  EXCLUDED  not in the panel; reason_code says why:
            not_a_lease, duplicate, loi, portfolio_total, gpu_cloud_contract, filer_is_tenant

The mapping reads the reviewer's status and the notes. It never changes a value.
Rows that the rules cannot classify stay CONFIRM, with the reason 'unclassified'.

Run:  python scripts/reduce_panel.py [path/to/lease_panel_reviewed.csv]
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT  # noqa: E402

EXTRA = ["reason_code", "deal_key", "site_evidence", "state_evidence"]

# Order matters: the first match wins.
RULES = [
    ("duplicate", r"\bduplicate\b|restate|same lease as|already in (?:row )?p\d{3}"),
    ("loi", r"\bLOI\b|letter of intent|non-binding"),
    ("gpu_cloud_contract", r"gpu cloud|cloud services contract|cloud contract|gpu-as-a-service|not a lease.*gpu"),
    ("filer_is_tenant", r"(?:company|filer|hive)\s+(?:is|appears to be)\s+the\s+(?:customer|tenant)|company is the tenant"),
    ("portfolio_total", r"portfolio total|portfolio totals|earnings deck|totals only"),
]
NO_TERMS_RE = re.compile(r"no (?:lease )?terms|terms not disclosed|without terms", re.I)
MULTI_RE = re.compile(r"combine|several leases|multiple leases|multi-lease|aggregate|split", re.I)
PRICE_COLS = ("contract_value_musd", "avg_annual_revenue_musd")


def has_terms(r: dict) -> bool:
    mw = (r.get("it_mw") or r.get("gross_mw") or "").strip()
    annual = (r.get("avg_annual_revenue_musd") or "").strip()
    tcv_term = (r.get("contract_value_musd") or "").strip() and (r.get("term_years") or "").strip()
    return bool(mw and (annual or tcv_term))


def classify(r: dict) -> tuple[str, str]:
    status = (r.get("status") or "").upper()
    note = r.get("notes") or ""
    if status.startswith("REVIEWED"):
        return ("REVIEWED", "") if has_terms(r) else ("NO_TERMS", "")
    if status.startswith("REJECTED"):
        if NO_TERMS_RE.search(note):
            return "NO_TERMS", ""
        for code, pat in RULES:
            if re.search(pat, note, re.I):
                return "EXCLUDED", code
        return "EXCLUDED", "not_a_lease"
    # CONFIRM / REQUIRES HUMAN CONFIRMATION / anything else
    for code, pat in RULES:
        if re.search(pat, note, re.I):
            return "EXCLUDED", code
    if MULTI_RE.search(note):
        return "CONFIRM", "split_or_aggregate"
    if NO_TERMS_RE.search(note):
        return "NO_TERMS", ""
    return "CONFIRM", "unclassified"


def main(argv: list[str]) -> int:
    src = Path(argv[1]) if len(argv) > 1 else ROOT / "data" / "lease_panel_reviewed.csv"
    if not src.exists():
        print(f"Not found: {src}. Put the reviewed file there or give its path.")
        return 1
    with open(src, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    fields = list(rows[0].keys())
    for e in EXTRA:
        if e not in fields:
            fields.append(e)
    counts, queue = {}, []
    for r in rows:
        before = r.get("status", "")
        st, code = classify(r)
        r["status"], r["reason_code"] = st, code or r.get("reason_code", "")
        for e in EXTRA:
            r.setdefault(e, "")
        r["notes"] = (r.get("notes") or "") + f" | reduce_panel: {before} -> {st}" + (f" ({code})" if code else "")
        counts[(st, code)] = counts.get((st, code), 0) + 1
        if st == "CONFIRM":
            queue.append({k: r.get(k, "") for k in ("lease_id", "landlord", "tenant", "announce_date", "it_mw", "gross_mw", "reason_code", "url", "notes")})
    out = ROOT / "data" / "lease_panel.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    rdir = ROOT / "reports"
    rdir.mkdir(exist_ok=True)
    with open(rdir / "lease_confirm_queue.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["lease_id", "landlord", "tenant", "announce_date", "it_mw", "gross_mw", "reason_code", "url", "notes"], lineterminator="\n")
        w.writeheader()
        w.writerows(queue)
    print(f"{len(rows)} rows -> {out}")
    for (st, code), n in sorted(counts.items()):
        print(f"  {st:9s} {code or '':22s} {n}")
    print(f"CONFIRM queue: {len(queue)} rows -> reports/lease_confirm_queue.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
