"""Tenant view per MW (data-only): CoreWeave's filed P&L per active MW, next to landlord rent.

Rule: every number is a filed value, market data, or arithmetic on them. No estimates.
Where filings do not split a layer, the layer stays combined.

Per quarter (CoreWeave filings, annualized, divided by average active MW):
  revenue, cost of revenue, other cash operating cost (= revenue - cost of revenue - adj. EBITDA),
  adj. EBITDA, D&A, interest, margin after D&A and interest,
  margin if D&A used Nebius's filed life instead of CoreWeave's.
Landlord: rent per IT MW from lease disclosures; profit only where a margin is filed.
Definitions: gross/IT ratios; Dec-24 kW per GPU on active power; the IT-load test runs
only when the H100 system power (datasheet) is in stack_inputs.csv.

Run:  python scripts/stack.py
"""
from __future__ import annotations

import csv
import datetime as dt
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, load_data, path, read_csv  # noqa: E402
from model import compute  # noqa: E402


def f(x):
    return None if x in (None, "") else float(x)


def load_inputs():
    return {r["key"]: {k: f(r[k]) for k in ("low", "base", "high")} | {"unit": r["unit"], "doc_id": r["doc_id"], "provenance": r["provenance"]}
            for r in read_csv("stack_inputs.csv")}


def deal_table(deals):
    out = []
    for d in deals:
        it, gross = f(d["it_mw"]), f(d["gross_mw"])
        ann = f(d["avg_annual_revenue_musd"])
        if ann is None and f(d["total_contract_value_musd"]) is not None:
            ann = f(d["total_contract_value_musd"]) / f(d["term_years"])
        per_mw = ann / it
        out.append({"deal_id": d["deal_id"], "landlord": d["landlord"], "it_mw": it, "gross_mw": gross,
                    "gross_to_it": (gross / it) if gross else None, "term_years": f(d["term_years"]),
                    "avg_annual_revenue_musd": ann, "rent_musd_per_it_mw_yr": per_mw,
                    "rent_usd_per_kw_month": per_mw * 1e6 / 1000 / 12,
                    "power_passthrough": d["power_passthrough"], "tenant_funds_fitout": d["tenant_funds_fitout"],
                    "doc_id": d["doc_id"], "confidence": d["confidence"]})
    return out


def months_between(a, b):
    return (b - a).days / 30.4375


def parse_month(s):
    if not s:
        return None
    if "-Q" in s:
        y, q = s.split("-Q")
        return dt.date(int(y), 3 * int(q) - 1, 15)
    parts = s.split("-")
    if len(parts) == 2:
        return dt.date(int(parts[0]), int(parts[1]), 15)
    return dt.date.fromisoformat(s)


def run():
    cfg, data = load_config(), load_data()
    res = compute(data, cfg)
    M, Q, qlab, days = res["M"], res["Q"], res["qlab"], res["days"]
    I = load_inputs()
    deals = deal_table(read_csv("power_deals.csv"))
    cal = {r["key"]: f(r["value"]) for r in read_csv("calibration.csv")}
    kpi = {(m, q): float(r["value"]) for (m, q), r in data["kpis"].items()}
    outputs, checks = [], []

    def out(section, name, period, value, unit, note=""):
        outputs.append({"section": section, "name": name, "period": period, "value": value, "unit": unit, "note": note})

    # Definitions and calibration
    for d in deals:
        if d["gross_to_it"]:
            out("definitions", f"gross_to_it_{d['deal_id']}", "", d["gross_to_it"], "ratio", "gross power / critical IT load (filed)")
    kw = cal["active_mw_dec24"] * 1000 / cal["gpus_dec24"]
    out("definitions", "coreweave_active_kw_per_gpu", "Q4'24", kw, "kW per GPU", "both inputs filed as 'more than': approximate")
    ratios = [d["gross_to_it"] for d in deals if d["gross_to_it"]]
    out("definitions", "it_kw_per_gpu_if_active_were_gross", "min", kw / max(ratios), "kW per GPU")
    out("definitions", "it_kw_per_gpu_if_active_were_gross", "max", kw / min(ratios), "kW per GPU")
    spec = I.get("kw_per_gpu_h100_it", {}).get("base")
    if spec is None:
        out("definitions", "active_power_test", "", None, "flag", "PENDING: add H100 system power per GPU (datasheet)")
    else:
        out("definitions", "active_power_test", "", 1.0 if kw / min(ratios) < spec <= kw * 1.05 else 0.0, "flag",
            "1 = filed Dec-24 ratio fits 'active = IT load' and not 'active = gross'")
    q4 = data["reported"][("Revenue", "Q4'24 (3M)")]
    out("definitions", "revenue_per_installed_gpu_hour", "Q4'24", q4 * 1e6 / (cal["gpus_dec24"] * 92 * 24), "$ per GPU-hour",
        "Q4'24 revenue / (Dec-24 GPUs x hours); approximate (GPU count filed as 'more than')")

    # Tenant view per active MW, each quarter
    prev_q = {q: (qlab[i - 1] if i else "Q4'24") for i, q in enumerate(qlab)}
    rents = sorted(d["rent_musd_per_it_mw_yr"] for d in deals)
    rent = {"min": rents[0], "median": statistics.median(rents), "max": rents[-1]}
    for k, v in rent.items():
        out("landlord", f"rent_{k}", "", v, "$M per IT MW-year", "lease disclosures")
    life_cw, life_nb = I["useful_life_coreweave"]["base"], I["useful_life_nebius"]["base"]
    series = {}
    for q in qlab:
        p = prev_q[q]
        if ("active_mw", p) not in kpi:
            continue
        avg = (kpi[("active_mw", p)] + kpi[("active_mw", q)]) / 2
        a = 365 / days[q] / avg
        L = {"avg_active_mw": avg,
             "revenue": Q[("Revenue", q)] * a,
             "cost_of_revenue": Q[("Cost of revenue", q)] * a,
             "adj_ebitda": Q[("Adjusted EBITDA", q)] * a,
             "da": Q[("Depreciation and amortization", q)] * a,
             "interest": Q[("Interest expense, net", q)] * a}
        L["other_cash_opex"] = L["revenue"] - L["cost_of_revenue"] - L["adj_ebitda"]
        L["margin_after_da_interest"] = L["adj_ebitda"] - L["da"] - L["interest"]
        L["margin_if_nebius_life"] = L["adj_ebitda"] - L["da"] * life_cw / life_nb - L["interest"]
        series[q] = L
        for k, v in L.items():
            out("tenant", k, q, v, "MW" if k == "avg_active_mw" else "$M per active MW-year")
        checks.append({"check": f"Cost of revenue per MW >= highest observed rent ({q})",
                       "pass": L["cost_of_revenue"] >= rent["max"], "model": L["cost_of_revenue"], "reference": rent["max"]})
    for k in ("revenue", "margin_after_da_interest"):
        vals = [series[q][k] for q in series]
        out("tenant_range", f"{k}_min", "", min(vals), "$M per active MW-year", "observed quarters")
        out("tenant_range", f"{k}_max", "", max(vals), "$M per active MW-year", "observed quarters")
    m = I["landlord_cash_margin_corz"]
    corz = next(d for d in deals if d["deal_id"] == "CORZ")
    out("landlord", "corz_cash_profit", "low", corz["rent_musd_per_it_mw_yr"] * m["low"], "$M per IT MW-year", "rent x filed margin target")
    out("landlord", "corz_cash_profit", "high", corz["rent_musd_per_it_mw_yr"] * m["high"], "$M per IT MW-year", "rent x filed margin target")

    # Time and duration
    ramp = read_csv("power_ramp.csv")
    for d in read_csv("power_deals.csv"):
        s, r = parse_month(d["signed_date"]), parse_month(d["first_rent_or_rfs"])
        if s and r:
            out("lags", f"months_signed_to_first_rent_or_rfs_{d['deal_id']}", "", months_between(s, r), "months", d["rent_basis_note"])
    cz = sorted((dt.date.fromisoformat(r["as_of"]), float(r["mw"])) for r in ramp if r["series"] == "CORZ billing")
    for (d0, m0), (d1, m1) in zip(cz, cz[1:]):
        out("lags", "corz_billing_add_rate", f"{d0} to {d1}", (m1 - m0) / months_between(d0, d1), "MW per month")
    for q in series:
        out("lags", "coreweave_active_added", q, (kpi[("active_mw", q)] - kpi[("active_mw", prev_q[q])]) / 3, "MW per month")
    terms = [d["term_years"] for d in deals]
    out("duration", "landlord_lease_term", "min", min(terms), "years")
    out("duration", "landlord_lease_term", "max", max(terms), "years")
    out("duration", "coreweave_useful_life_filed", "", life_cw, "years")
    bs, dl = data["bs"], data["bs_dates"][-1]
    out("duration", "coreweave_operating_lease_liabilities", dl,
        bs[("Operating lease liabilities, current", dl)] + bs[("Operating lease liabilities, non-current", dl)], "$M")
    out("duration", "coreweave_total_debt", dl, M[("total_debt", dl)], "$M")
    return outputs, deals, checks


def main() -> int:
    cfg = load_config()
    outputs, deals, checks = run()
    rdir = path(cfg, "reports")
    rdir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("stack_outputs.csv", outputs), ("stack_deals.csv", deals), ("stack_checks.csv", checks)):
        with open(rdir / name, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
    g = {(o["section"], o["name"], o["period"]): o["value"] for o in outputs}
    qs = [o["period"] for o in outputs if o["section"] == "tenant" and o["name"] == "revenue"]
    print("CoreWeave per active MW-year ($M): " + "  ".join(f"{q:>7s}" for q in qs))
    for k in ("revenue", "cost_of_revenue", "other_cash_opex", "adj_ebitda", "da", "interest", "margin_after_da_interest", "margin_if_nebius_life"):
        print(f"  {k:26s}" + "  ".join(f"{g[('tenant', k, q)]:7.2f}" for q in qs))
    print(f"Landlord rent ($M per IT MW-year): min {g[('landlord', 'rent_min', '')]:.2f}, median {g[('landlord', 'rent_median', '')]:.2f}, max {g[('landlord', 'rent_max', '')]:.2f}")
    fails = [c for c in checks if not c["pass"]]
    print(f"checks: {len(checks) - len(fails)}/{len(checks)} pass")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
