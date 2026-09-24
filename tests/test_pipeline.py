"""Tests. Run with pytest, or directly: python tests/test_pipeline.py

The verify test copies the project to a temporary folder, writes fixture source
files (no network), and runs scripts/verify.py there.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from common import load_config, load_data, validate  # noqa: E402
from model import compute, run_checks  # noqa: E402
from verify import normalize  # noqa: E402


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1, abs(b))


def test_data_valid():
    assert validate(load_data(), load_config()) == []


def test_derived_quarters():
    res = compute(load_data(), load_config())
    Q = res["Q"]
    assert Q[("Revenue", "Q1'25")] == 982
    assert Q[("Revenue", "Q3'25")] == 1365
    assert Q[("Revenue", "Q1'26")] == 2078
    assert Q[("Net loss", "Q1'26")] == -740
    assert Q[("Purchase of PP&E (cash capex)", "Q1'26")] == 7695


def test_key_metrics():
    M = compute(load_data(), load_config())["M"]
    assert close(M[("rev_per_mw", "Q2'26")], 2575 * 365 / 91 / 1250)
    assert close(M[("bookings", "Q1'26")], 99.4 - 66.8 + 2.078)
    assert close(M[("rev_per_gpu_hour", "Q2'26")], 2575 / (625 * 91 * 24 / 1000))
    assert close(M[("dep_rate", "FY25")], 2454 / ((11915 + 30557) / 2))
    assert close(M[("g_change_needed", "low")], 0.17063787874705505)


def test_all_checks_pass():
    cfg, data = load_config(), load_data()
    checks = run_checks(data, cfg, compute(data, cfg))
    failed = [c["check_id"] for c in checks if not c["pass"]]
    assert failed == [], failed


def test_normalize():
    assert normalize("Grew total contracted power to approximately 3.7 GW") in normalize(
        "<p>Grew total contracted power to\n approximately <b>3.7</b> GW while ...</p>".replace("<b>", "").replace("</b>", ""))
    assert normalize("$30,100,000,000") == "30100000000"


def test_verify_end_to_end_offline():
    tmp = Path(tempfile.mkdtemp())
    proj = tmp / "proj"
    shutil.copytree(ROOT, proj, ignore=shutil.ignore_patterns("output", "reports", "raw", "__pycache__", ".git"))
    raw = proj / "raw"
    (raw / "xbrl").mkdir(parents=True)
    # Evidence fixture: D01 contains two KPI phrases; one phrase is deliberately absent (Q2'26 backlog is in D06).
    (raw / "D01.html").write_text(
        "<html><body><ul><li>Expanded active power by nearly 500 MWs to reach 1.5 GW</li>"
        "<li>Grew total contracted power to approximately 3.7 GW while further diversifying</li></ul></body></html>",
        encoding="utf-8")
    (raw / "D29.html").write_text("<p>Across Phases I through III, CoreWeave has committed to 526 MW of critical IT load, the full 800 MW.</p>", encoding="utf-8")
    # XBRL fixture: revenue Q2'26 (3M) correct; FY25 and 9M'25 to derive Q4'25; one wrong value to force FAIL.
    facts = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"label": "Revenue", "units": {"USD": [
            {"start": "2026-04-01", "end": "2026-06-30", "val": 2575e6, "form": "10-Q", "filed": "2026-08-12"},
            {"start": "2025-01-01", "end": "2025-12-31", "val": 5131e6, "form": "10-K", "filed": "2026-02-27"},
            {"start": "2025-01-01", "end": "2025-09-30", "val": 3559e6, "form": "10-Q", "filed": "2025-11-12"}]}},
        "CostOfRevenue": {"label": "Cost of revenue", "units": {"USD": [
            {"start": "2026-04-01", "end": "2026-06-30", "val": 900e6, "form": "10-Q", "filed": "2026-08-12"}]}},
        "Assets": {"label": "Assets", "units": {"USD": [
            {"end": "2026-06-30", "val": 77070e6, "form": "10-Q", "filed": "2026-08-12"}]}},
    }}}
    (raw / "xbrl" / "companyfacts.json").write_text(json.dumps(facts), encoding="utf-8")
    subprocess.run([sys.executable, str(proj / "scripts" / "verify.py")], cwd=proj, capture_output=True, text=True)
    import csv
    rows = list(csv.DictReader(open(proj / "reports" / "verification.csv", encoding="utf-8")))

    def status(kind, key, period):
        return next(r["status"] for r in rows if r["kind"] == kind and r["key"] == key and r["period"] == period)

    assert status("evidence:kpis", "active_mw", "Q2'26") == "PASS"
    assert status("evidence:kpis", "contracted_gw", "Q2'26") == "PASS"
    assert status("evidence:kpis", "active_mw", "Q2'25") == "NO_FILE"
    assert status("evidence:power_deals", "GLXY", "") == "PASS"
    assert status("evidence:power_deals", "CORZ", "") == "NO_FILE"
    assert status("xbrl", "Revenue", "Q2'26 (3M)") == "PASS"
    assert status("xbrl", "Revenue", "Q4'25 (3M)") == "PASS"      # 5131 - 3559 = 1572
    assert status("xbrl", "Cost of revenue", "Q2'26 (3M)") == "FAIL"  # 900 vs 879
    assert status("xbrl", "Total assets", "2026-06-30") == "PASS"
    assert status("xbrl", "Adjusted EBITDA", "Q2'26 (3M)") == "SKIP"
    shutil.rmtree(tmp)


def test_stack():
    from stack import run
    outputs, deals, checks = run()
    d = {x["deal_id"]: x for x in deals}
    assert close(d["CORZ"]["rent_musd_per_it_mw_yr"], 850 / 590)
    assert close(d["APLD"]["rent_musd_per_it_mw_yr"], 11000 / 15 / 400)
    assert close(d["GLXY"]["gross_to_it"], 800 / 526)
    g = {(o["section"], o["name"], o["period"]): o["value"] for o in outputs}
    assert close(g[("definitions", "coreweave_active_kw_per_gpu", "Q4'24")], 1.44)
    assert close(g[("tenant", "revenue", "Q2'26")], 2575 * 365 / 91 / 1250)
    assert close(g[("tenant", "revenue", "Q1'25")], 982 * 365 / 90 / 390)   # uses Q4'24 active power from the S-1
    assert close(g[("tenant", "margin_after_da_interest", "Q2'26")],
                 (1510 - 1393 - 640) * 365 / 91 / 1250)
    assert g[("definitions", "active_power_test", "")] is None              # pending datasheet
    assert all(c["pass"] for c in checks)


def test_lease_extraction_units():
    from leases_scrape import extract_facts, html_to_text, parse_efts, doc_url, doc_id_for
    fx = ROOT / "tests" / "fixtures"
    rows = parse_efts(json.loads((fx / "efts_sample.json").read_text()))
    assert rows[0]["company"] == "Applied Digital Corp.  (APLD)".replace("  ", "  ") or rows[0]["company"].startswith("Applied Digital")
    assert doc_url(rows[0]) == "https://www.sec.gov/Archives/edgar/data/1144879/000149315225012458/ex99-1.htm"
    assert doc_id_for(rows[0]) == "L000149315225012458_ex99-1"
    text = html_to_text((fx / "L000149315225012458_ex99-1.html").read_text())
    facts = extract_facts("x", text, ["CoreWeave"])
    kinds = {(f.kind, f.value) for f in facts}
    assert ("mw_it", 400.0) in kinds
    assert ("mw_unspecified", 150.0) in kinds
    assert ("contract_value", 11000.0) in kinds
    assert ("term_years", 15.0) in kinds
    assert ("tenant", "CoreWeave") in kinds
    assert ("financing_or_capex", 375.0) in kinds


def test_lease_template_and_verify_offline():
    import csv as _csv
    tmp = Path(tempfile.mkdtemp())
    proj = tmp / "proj"
    shutil.copytree(ROOT, proj, ignore=shutil.ignore_patterns("output", "reports", "raw", "__pycache__", ".git"))
    (proj / "reports").mkdir()
    docs = proj / "raw" / "leases" / "docs"
    docs.mkdir(parents=True)
    fx = ROOT / "tests" / "fixtures"
    for name in ("L000149315225012458_ex99-1.html", "L000999999926000001_ex99-1.html"):
        shutil.copy(fx / name, docs / name)
    # search hits file as step_search would write it
    sys.path.insert(0, str(proj / "scripts"))
    from leases_scrape import parse_efts, doc_url, doc_id_for
    rows = parse_efts(json.loads((fx / "efts_sample.json").read_text()))
    with open(proj / "reports" / "lease_search_hits.csv", "w", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=["doc_id", "company", "cik", "form", "file_type", "file_date", "description", "adsh", "file_name", "url", "query"])
        w.writeheader()
        for r in rows:
            r.update({"url": doc_url(r), "doc_id": doc_id_for(r), "query": "test"})
            w.writerow(r)
    (proj / "data" / "lease_panel.csv").unlink(missing_ok=True)
    r = subprocess.run([sys.executable, str(proj / "scripts" / "leases_scrape.py"), "template"], cwd=proj, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    panel = list(_csv.DictReader(open(proj / "data" / "lease_panel.csv")))
    assert len(panel) == 1, panel                     # the non-lease document scores too low
    row = panel[0]
    assert row["status"] == "UNREVIEWED" and row["it_mw"] == "400" and row["tenant"] == "CoreWeave"
    assert row["contract_value_musd"] == "11000" and row["term_years"] == "15"
    # Reviewer confirms; verify must pass. Then a wrong value must fail.
    row["status"] = "REVIEWED"
    def write(rows_):
        with open(proj / "data" / "lease_panel.csv", "w", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=list(rows_[0].keys()))
            w.writeheader(); w.writerows(rows_)
    write([row])
    subprocess.run([sys.executable, str(proj / "scripts" / "verify.py")], cwd=proj, capture_output=True, text=True)
    res = [x for x in _csv.DictReader(open(proj / "reports" / "verification.csv")) if x["kind"] == "lease_panel"]
    assert res and all(x["status"] == "PASS" for x in res), res
    row["it_mw"] = "450"
    write([row])
    subprocess.run([sys.executable, str(proj / "scripts" / "verify.py")], cwd=proj, capture_output=True, text=True)
    res = {x["period"]: x["status"] for x in _csv.DictReader(open(proj / "reports" / "verification.csv")) if x["kind"] == "lease_panel"}
    assert res["it_mw"] == "FAIL"
    r = subprocess.run([sys.executable, str(proj / "scripts" / "leases_analyze.py")], cwd=proj, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    shutil.rmtree(tmp)


def test_reduce_panel():
    from reduce_panel import classify
    base = {"it_mw": "200", "gross_mw": "", "term_years": "15", "contract_value_musd": "3000", "avg_annual_revenue_musd": ""}
    assert classify({**base, "status": "REVIEWED", "notes": "REVIEWED: ok"}) == ("REVIEWED", "")
    assert classify({**base, "status": "REVIEWED", "contract_value_musd": "", "notes": ""}) == ("NO_TERMS", "")
    assert classify({**base, "status": "REJECTED", "notes": "REJECTED: not a data-center lease (M&A)"}) == ("EXCLUDED", "not_a_lease")
    assert classify({**base, "status": "REJECTED", "notes": "REJECTED: portfolio totals only"}) == ("EXCLUDED", "portfolio_total")
    assert classify({**base, "status": "REJECTED", "notes": "REJECTED: lease mentioned but no terms"}) == ("NO_TERMS", "")
    assert classify({**base, "status": "REQUIRES HUMAN CONFIRMATION", "notes": "CONFIRM: possible duplicate of P077"}) == ("EXCLUDED", "duplicate")
    assert classify({**base, "status": "REQUIRES HUMAN CONFIRMATION", "notes": "CONFIRM: LOI, not a signed lease"}) == ("EXCLUDED", "loi")
    assert classify({**base, "status": "REQUIRES HUMAN CONFIRMATION", "notes": "CONFIRM: IREN-Microsoft GPU cloud contract (not a lease)"}) == ("EXCLUDED", "gpu_cloud_contract")
    assert classify({**base, "status": "REQUIRES HUMAN CONFIRMATION", "notes": "CONFIRM: HIVE appears to be the customer"}) == ("EXCLUDED", "filer_is_tenant")
    assert classify({**base, "status": "REQUIRES HUMAN CONFIRMATION", "notes": "CONFIRM: row combines several leases; split"}) == ("CONFIRM", "split_or_aggregate")
    assert classify({**base, "status": "REQUIRES HUMAN CONFIRMATION", "notes": "CONFIRM: BCE, check filing"}) == ("CONFIRM", "unclassified")


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    bad = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            bad += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"{len(tests) - bad}/{len(tests)} tests pass")
    sys.exit(1 if bad else 0)
