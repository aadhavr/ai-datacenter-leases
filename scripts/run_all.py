"""Run the full pipeline: validate -> model -> build -> parity -> verify.

Run:  python scripts/run_all.py [--fetch] [--wayback] [--strict]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from parity_check import find_soffice  # noqa: E402


def step(name, args):
    print(f"\n=== {name} ===")
    return subprocess.run([sys.executable, str(HERE / args[0]), *args[1:]]).returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="download sources first")
    ap.add_argument("--wayback", action="store_true", help="with --fetch: use Internet Archive snapshots")
    ap.add_argument("--strict", action="store_true", help="verify: fail on any unverified row")
    ap.add_argument("--leases", action="store_true", help="scrape SEC filings for new lease candidates first")
    a = ap.parse_args()
    rc = {}
    if a.fetch:
        rc["fetch"] = step("fetch", ["fetch_sources.py"] + (["--wayback"] if a.wayback else []))
    if a.leases:
        rc["leases"] = step("lease scrape (search, fetch, extract, template)", ["leases_scrape.py", "all"])
    rc["model"] = step("model + checks", ["model.py"])
    rc["stack"] = step("MW stack", ["stack.py"])
    rc["build"] = step("build workbook", ["build_workbook.py"])
    if find_soffice():
        rc["parity"] = step("parity (workbook vs model)", ["parity_check.py"])
    else:
        print("\n=== parity === skipped: LibreOffice not found (open and save in Excel, then run parity_check.py --no-recalc)")
    rc["verify"] = step("verify against sources", ["verify.py"] + (["--strict"] if a.strict else []))
    if (HERE.parent / "data" / "lease_panel.csv").exists():
        rc["lease_panel"] = step("lease panel analysis (REVIEWED rows)", ["leases_analyze.py"])
    print("\n=== summary ===")
    for k, v in rc.items():
        print(f"{k:8s} {'OK' if v == 0 else f'exit {v}'}")
    return 0 if all(v == 0 for v in rc.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
