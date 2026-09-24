"""Pure-Python version of the model.

It computes the same numbers as the workbook formulas, from the same CSV files.
Use it to test the model without Excel, and to check parity with the workbook.

Run:  python scripts/model.py      -> writes reports/model_outputs.csv and reports/checks.csv
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, load_data, path, validate  # noqa: E402

TOKEN = re.compile(r"^\{(.+):(.+)\}$")


def compute(data: dict, cfg: dict) -> dict:
    A = {k: v["value"] for k, v in data["assumptions"].items()}
    quarters = cfg["quarters"]
    qlab = [q["label"] for q in quarters]
    rep = data["reported"]

    # Standalone quarters
    Q = {}
    for item in data["items"]:
        for q in quarters:
            v = sum(rep[(item, b)] for b in q["plus"]) - sum(rep[(item, b)] for b in q["minus"])
            Q[(item, q["label"])] = v
    days = {q["label"]: (q["end"] - q["start"]).days + 1 for q in quarters}

    def kpi(metric, q):
        r = data["kpis"].get((metric, q))
        return float(r["value"]) if r else None

    out = {"Q": Q, "days": days, "qlab": qlab, "M": {}}
    M = out["M"]

    # Ratios
    for i, q in enumerate(qlab):
        rev = Q[("Revenue", q)]
        M[("growth_qoq", q)] = None if i == 0 else rev / Q[("Revenue", qlab[i - 1])] - 1
        y = cfg["yoy"].get(q)
        if y is None:
            M[("growth_yoy", q)] = None
        else:
            if "assumption" in y:
                base = A[y["assumption"]]
            elif "block" in y:
                base = rep[("Revenue", y["block"])]
            else:
                base = Q[("Revenue", y["quarter"])]
            M[("growth_yoy", q)] = rev / base - 1
        M[("adj_ebitda_margin", q)] = Q[("Adjusted EBITDA", q)] / rev
        M[("op_margin", q)] = Q[("Operating income (loss)", q)] / rev
        M[("da_pct_rev", q)] = Q[("Depreciation and amortization", q)] / rev
        M[("interest_pct_rev", q)] = Q[("Interest expense, net", q)] / rev
        M[("ebitda_interest_cover", q)] = Q[("Adjusted EBITDA", q)] / Q[("Interest expense, net", q)]
        M[("capex_to_rev", q)] = Q[("Purchase of PP&E (cash capex)", q)] / rev

    # Analysis
    kw = A["kw_per_gpu"]
    hpd = A["hours_per_day"]
    for i, q in enumerate(qlab):
        rev = Q[("Revenue", q)]
        act, con, bkl = kpi("active_mw", q), kpi("contracted_gw", q), kpi("backlog_busd", q)
        M[("backlog", q)] = bkl
        M[("contracted_gw", q)] = con
        M[("active_gw", q)] = act / 1000
        M[("pipeline_gw", q)] = con - act / 1000
        M[("active_share", q)] = (act / 1000) / con
        M[("backlog_per_gw", q)] = bkl / con
        M[("backlog_years", q)] = bkl * 1000 / (rev * 365 / days[q])
        if i == 0:
            for k in ("avg_active_mw", "rev_per_mw", "ebitda_per_mw", "gpus_k", "gpu_hours_m",
                      "rev_per_gpu_hour", "bookings", "added_mw", "quarters_to_activate"):
                M[(k, q)] = None
            continue
        p = qlab[i - 1]
        avg = (kpi("active_mw", p) + act) / 2
        M[("avg_active_mw", q)] = avg
        M[("rev_per_mw", q)] = rev * 365 / days[q] / avg
        M[("ebitda_per_mw", q)] = Q[("Adjusted EBITDA", q)] * 365 / days[q] / avg
        M[("gpus_k", q)] = avg / kw
        M[("gpu_hours_m", q)] = M[("gpus_k", q)] * days[q] * hpd / 1000
        M[("rev_per_gpu_hour", q)] = rev / M[("gpu_hours_m", q)]
        M[("bookings", q)] = bkl - kpi("backlog_busd", p) + rev / 1000
        added = act - kpi("active_mw", p)
        M[("added_mw", q)] = added
        M[("quarters_to_activate", q)] = None if added <= 0 else M[("pipeline_gw", q)] * 1000 / added

    # Balance-sheet derived
    bs = data["bs"]
    for d in data["bs_dates"]:
        M[("total_debt", d)] = bs[("Debt, current (recourse + non-recourse)", d)] + bs[("Debt, non-current (recourse + non-recourse)", d)]

    # Implied depreciation and interest rates
    for rp in cfg["rate_periods"]:
        lab = rp["label"]
        ndays = (rp["end"] - rp["start"]).days + 1
        fac = 365 / ndays
        s, e = rp["bs_start"].isoformat(), rp["bs_end"].isoformat()
        avg_ppe = (bs[("Property and equipment, net", s)] + bs[("Property and equipment, net", e)]) / 2
        avg_debt = (M[("total_debt", s)] + M[("total_debt", e)]) / 2
        da = rep[("Depreciation and amortization", rp["block"])]
        intr = rep[("Interest expense, net", rp["block"])]
        M[("dep_rate", lab)] = da * fac / avg_ppe
        M[("implied_life", lab)] = 1 / M[("dep_rate", lab)]
        M[("interest_rate", lab)] = intr * fac / avg_debt

    # Guidance check (FY26)
    h1 = rep[("Revenue", cfg.get("h1_block", "6M'26"))]
    q3_end = (kpi("active_mw", qlab[-1]) + A["ye26_active_mw"]) / 2
    q4_avg = (q3_end + A["ye26_active_mw"]) / 2
    for side in ("low", "high"):
        h2 = A[f"fy26_rev_{side}"] - h1
        q4 = h2 - A[f"q3_26_rev_{side}"]
        M[("g_h2_rev", side)] = h2
        M[("g_q4_rev", side)] = q4
        M[("g_q4_growth", side)] = q4 / A[f"q3_26_rev_{side}"] - 1
        M[("g_q4_avg_mw", side)] = q4_avg
        M[("g_q4_rev_per_mw", side)] = q4 * 365 / 92 / q4_avg
        M[("g_change_needed", side)] = M[("g_q4_rev_per_mw", side)] / M[("rev_per_mw", qlab[-1])] - 1

    # Sensitivity
    last = qlab[-1]
    for k in cfg["sensitivity_kw_per_gpu"]:
        M[("sens_rev_per_gpu_hour", k)] = Q[("Revenue", last)] / ((M[("avg_active_mw", last)] / k) * days[last] * 24 / 1000)
    return out


def resolve(token, res):
    """Resolve a value from reconciliation/xchecks: number, '=expr', or '{name:period}'."""
    if token is None or isinstance(token, float):
        return token
    s = str(token).strip()
    try:
        return float(s)
    except ValueError:
        pass
    if s.startswith("="):
        expr = s[1:]
        if not re.fullmatch(r"[0-9.+\-*/() ]+", expr):
            raise ValueError(f"unsafe expression {s!r}")
        return eval(expr)  # noqa: S307 - digits and operators only
    m = TOKEN.match(s)
    if not m:
        raise ValueError(f"cannot resolve {s!r}")
    name, per = m.group(1), m.group(2)
    if (name, per) in res["Q"]:
        return res["Q"][(name, per)]
    if (name, per) in res["M"]:
        return res["M"][(name, per)]
    raise KeyError(f"unknown token {s!r}")


def run_checks(data, cfg, res) -> list[dict]:
    Q, qlab = res["Q"], res["qlab"]
    out = []

    def add(cid, label, model, ref, tol, src):
        ok = model is not None and ref is not None and abs(model - ref) <= tol
        out.append({"check_id": cid, "label": label, "pass": ok, "model_value": model,
                    "reference_value": ref, "tolerance": tol, "reference_source": src})

    for q in qlab:
        s = sum(Q[(n, q)] for n in ["Cost of revenue", "Technology and infrastructure", "Sales and marketing", "General and administrative"])
        add(f"A-opex-{q}", f"Opex lines sum to total opex, {q}", s, Q[("Total operating expenses", q)], 0.5, "Arithmetic")
        add(f"A-opinc-{q}", f"Revenue - opex = operating income, {q}", Q[("Revenue", q)] - Q[("Total operating expenses", q)], Q[("Operating income (loss)", q)], 0.5, "Arithmetic")
    bs = data["bs"]
    for d in data["bs_dates"]:
        lhs = bs[("Total liabilities", d)] + bs[("Redeemable convertible preferred stock", d)] + bs[("Total stockholders' equity (deficit)", d)]
        add(f"A-bs-{d}", f"Liabilities + preferred + equity = assets, {d}", lhs, bs[("Total assets", d)], 0.5, "Arithmetic")
    # EBITDA bridge per block in ebitda_bridge.csv
    for blk in sorted({b["block"] for b in data["bridge"]}):
        rep = data["reported"]
        extra = sum(float(b["value_musd"]) for b in data["bridge"] if b["block"] == blk)
        lhs = rep[("Net loss", blk)] + rep[("Depreciation and amortization", blk)] + rep[("Interest expense, net", blk)] + rep[("Stock-based compensation", blk)] + extra
        add(f"A-bridge-{blk}", f"Adj. EBITDA bridge, {blk}", lhs, rep[("Adjusted EBITDA", blk)], 1, "ebitda_bridge.csv")
    for x in data["xchecks"]:
        add(x["check_id"], x["label"], resolve(x["model_ref"], res), float(x["reference_value"]), float(x["tolerance"]), x["reference_source"])
    return out


def write_reports(cfg, res, checks):
    rdir = path(cfg, "reports")
    rdir.mkdir(parents=True, exist_ok=True)
    with open(rdir / "model_outputs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["kind", "name", "period", "value"])
        for (n, p), v in res["Q"].items():
            w.writerow(["quarterly", n, p, v])
        for (n, p), v in res["M"].items():
            w.writerow(["metric", n, p, v])
    with open(rdir / "checks.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(checks[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(checks)


def main() -> int:
    cfg, data = load_config(), load_data()
    problems = validate(data, cfg)
    if problems:
        print("DATA PROBLEMS:")
        for p in problems:
            print("  -", p)
        return 1
    res = compute(data, cfg)
    checks = run_checks(data, cfg, res)
    write_reports(cfg, res, checks)
    failed = [c for c in checks if not c["pass"]]
    print(f"model: {len(res['Q']) + len(res['M'])} values; checks: {len(checks) - len(failed)}/{len(checks)} pass")
    for c in failed:
        print(f"  FAIL {c['check_id']}: {c['label']} model={c['model_value']} ref={c['reference_value']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
