"""Analyze the lease panel (REVIEWED rows only).

For each lease:
  rent_musd_per_it_mw_yr = avg annual revenue / IT MW   (or contract value / term / IT MW)
  rent_usd_per_kw_month  = the same in $ per kW of IT load per month
  gross_to_it            = gross MW / IT MW
  months_to_rfs          = announce date -> ready-for-service or rent date
Comparisons (descriptive only; small sample):
  H1 scarcity: rent by announcement half-year
  H2 risk:     rent with vs without stated credit support
  H3 speed:    rent vs months to RFS (rank correlation)
  Term:        rent vs term (rank correlation)
Comparability flags: power paid by, fit-out paid by. Compare like with like.

Run:  python scripts/leases_analyze.py
"""
from __future__ import annotations

import csv
import datetime as dt
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_config, path  # noqa: E402


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def d(x):
    try:
        return dt.date.fromisoformat(x)
    except (TypeError, ValueError):
        return None


def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(x, y):
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
    if len(pairs) < 4:
        return None, len(pairs)
    rx, ry = rank([p[0] for p in pairs]), rank([p[1] for p in pairs])
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return (num / den if den else None), len(pairs)


def metrics(rows):
    out = []
    for r in rows:
        it, gross, unst = f(r["it_mw"]), f(r["gross_mw"]), f(r.get("mw_unstated"))
        ann = f(r["avg_annual_revenue_musd"])
        tcv, term = f(r["contract_value_musd"]), f(r["term_years"])
        basis = "annual revenue (filed)"
        if ann is None and tcv is not None and term:
            ann, basis = tcv / term, "contract value / term"
        mw, mw_basis = (it, "IT") if it else ((unst, "unstated") if unst else ((gross, "gross") if gross else (None, "")))
        rent = ann / mw if (ann is not None and mw) else None
        a = d(r["announce_date"])
        fr = (r.get("first_rfs") or "").strip()
        rfs = dt.date(int(fr[:4]), int(fr[5:7]), 15) if len(fr) == 7 else None
        out.append({
            "lease_id": r["lease_id"], "landlord": r["landlord"], "tenant": r["tenant"], "state": r["state"],
            "announce_date": r["announce_date"], "deal_key": r.get("deal_key", ""), "n_leases": r.get("n_leases", ""),
            "it_mw": it, "gross_mw": gross, "mw_unstated": unst, "mw_basis": mw_basis, "term_years": term,
            "annual_revenue_musd": ann, "rent_basis": basis,
            "rent_musd_per_mw_yr": rent, "rent_usd_per_kw_month": rent * 1e6 / 1000 / 12 if rent else None,
            "gross_to_it": gross / it if (gross and it) else None,
            "months_to_rfs": (rfs - a).days / 30.4375 if (a and rfs) else None,
            "power_paid_by": r["power_paid_by"], "fitout_paid_by": r["fitout_paid_by"],
            "credit_support": r["credit_support"], "credit_type": r.get("credit_type", ""), "first_rfs": fr,
            "half_year": f"{a.year}H{1 if a.month <= 6 else 2}" if a else "",
        })
    return out


def summarize(m_all):
    lines = []
    for basis in ("IT", "unstated", "gross"):
        v = [x["rent_usd_per_kw_month"] for x in m_all if x["mw_basis"] == basis and x["rent_usd_per_kw_month"]]
        if v:
            lines.append(("by_basis", f"rent_usd_per_kw_month_{basis}", "n", len(v)))
            lines.append(("by_basis", f"rent_usd_per_kw_month_{basis}", "median", statistics.median(v)))
    m = [x for x in m_all if x["mw_basis"] == "IT"]  # hypotheses use IT-basis leases only
    rents = [x["rent_usd_per_kw_month"] for x in m if x["rent_usd_per_kw_month"]]
    if rents:
        lines.append(("all", "rent_usd_per_kw_month", "n", len(rents)))
        lines.append(("all", "rent_usd_per_kw_month", "median", statistics.median(rents)))
        lines.append(("all", "rent_usd_per_kw_month", "min", min(rents)))
        lines.append(("all", "rent_usd_per_kw_month", "max", max(rents)))
        if len(rents) > 1:
            lines.append(("all", "rent_usd_per_kw_month", "coef_of_variation", statistics.pstdev(rents) / statistics.mean(rents)))
    g = [x["gross_to_it"] for x in m if x["gross_to_it"]]
    if g:
        lines.append(("definitions", "gross_to_it", "median", statistics.median(g)))
        lines.append(("definitions", "gross_to_it", "min", min(g)))
        lines.append(("definitions", "gross_to_it", "max", max(g)))
    # H1: by half-year of announcement
    for hy in sorted({x["half_year"] for x in m if x["half_year"]}):
        v = [x["rent_usd_per_kw_month"] for x in m if x["half_year"] == hy and x["rent_usd_per_kw_month"]]
        if v:
            lines.append(("H1_scarcity", f"rent_{hy}", "median", statistics.median(v)))
            lines.append(("H1_scarcity", f"rent_{hy}", "n", len(v)))
    t = [(d(x["announce_date"]).toordinal() if d(x["announce_date"]) else None) for x in m]
    rho, n = spearman(t, [x["rent_usd_per_kw_month"] for x in m])
    lines.append(("H1_scarcity", "spearman_rent_vs_announce_date", f"n={n}", rho))
    # H2: credit type
    for ctype in ("ig_tenant", "parent_guarantee", "third_party_backstop", "anticipated", "none_stated"):
        v = [x["rent_usd_per_kw_month"] for x in m if x["credit_type"] == ctype and x["rent_usd_per_kw_month"]]
        if v:
            lines.append(("H2_risk", ctype, "median", statistics.median(v)))
            lines.append(("H2_risk", ctype, "n", len(v)))
    firm = [x["rent_usd_per_kw_month"] for x in m if x["credit_type"] in ("ig_tenant", "parent_guarantee", "third_party_backstop") and x["rent_usd_per_kw_month"]]
    weak = [x["rent_usd_per_kw_month"] for x in m if x["credit_type"] in ("anticipated", "none_stated") and x["rent_usd_per_kw_month"]]
    if firm and weak:
        lines.append(("H2_risk", "firm_credit_support_all_types", "median", statistics.median(firm)))
        lines.append(("H2_risk", "firm_credit_support_all_types", "n", len(firm)))
        lines.append(("H2_risk", "anticipated_or_none", "median", statistics.median(weak)))
        lines.append(("H2_risk", "anticipated_or_none", "n", len(weak)))
    # H3: speed
    rho, n = spearman([x["months_to_rfs"] for x in m], [x["rent_usd_per_kw_month"] for x in m])
    lines.append(("H3_speed", "spearman_rent_vs_months_to_rfs", f"n={n}", rho))
    rho, n = spearman([x["term_years"] for x in m], [x["rent_usd_per_kw_month"] for x in m])
    lines.append(("term", "spearman_rent_vs_term", f"n={n}", rho))
    return lines


def main() -> int:
    cfg = load_config()
    p = ROOT / "data" / "lease_panel.csv"
    if not p.exists():
        print("No data/lease_panel.csv. Run scripts/leases_scrape.py all first.")
        return 1
    with open(p, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    reviewed = [r for r in rows if r["status"] == "REVIEWED"]
    by = {}
    for r in rows:
        k = r["status"] + (f" ({r.get('reason_code')})" if r.get("reason_code") else "")
        by[k] = by.get(k, 0) + 1
    print(f"panel: {len(rows)} rows: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
    n_terms = len(reviewed)
    n_noterms = sum(r["status"] == "NO_TERMS" for r in rows)
    if n_terms + n_noterms:
        print(f"signed leases: {n_terms + n_noterms}; with price terms {n_terms} ({n_terms / (n_terms + n_noterms):.0%}); without {n_noterms}")
    if not reviewed:
        print("Nothing to analyze yet: review rows in data/lease_panel.csv and set status to REVIEWED.")
        return 0
    m = metrics(reviewed)
    rdir = path(cfg, "reports")
    with open(rdir / "lease_panel_metrics.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(m[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(m)
    s = summarize(m)
    with open(rdir / "lease_panel_summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["section", "metric", "stat", "value"])
        w.writerows(s)
    for row in s:
        v = row[3]
        print(f"  {row[0]:12s} {row[1]:36s} {row[2]:18s} {v if v is None else round(v, 3)}")
    print("Descriptive only. With a small sample, report as 'consistent with' / 'not consistent with'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
