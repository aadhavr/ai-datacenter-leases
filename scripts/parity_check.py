"""Check that the workbook formulas give the same numbers as scripts/model.py.

The workbook has formulas but no cached values when openpyxl writes it.
To compare, the file needs recalculated values:
  - With LibreOffice installed, this script recalculates a copy headless.
  - Without it, open output/coreweave_model.xlsx in Excel, save, and run with --no-recalc.

Run:  python scripts/parity_check.py [--no-recalc]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, load_data, path  # noqa: E402
from model import compute, run_checks  # noqa: E402

def find_soffice():
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",
              r"C:\Program Files\LibreOffice\program\soffice.exe"):
        if Path(p).exists():
            return p
    return None


def recalc_copy(src: Path) -> Path:
    """Recalculate a copy with LibreOffice (headless convert xlsx -> xlsx).

    The workbook is saved with fullCalcOnLoad, so LibreOffice computes every formula on load.
    """
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice not found. Open the workbook in Excel, save it, and run with --no-recalc.")
    tmp = Path(tempfile.mkdtemp())
    outdir = tmp / "out"
    cmd = [soffice, f"-env:UserInstallation={(tmp / 'profile').as_uri()}", "--headless", "--norestore",
           "--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", str(outdir), str(src)]
    subprocess.run(cmd, check=True, timeout=180, capture_output=True)
    return outdir / src.name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-recalc", action="store_true", help="use cached values already in the workbook")
    ap.add_argument("--tol", type=float, default=1e-6)
    args = ap.parse_args()

    cfg, data = load_config(), load_data()
    res = compute(data, cfg)
    checks = {c["check_id"]: c for c in run_checks(data, cfg, res)}
    wbp = path(cfg, "output")
    cmap = json.loads((wbp.parent / "cell_map.json").read_text(encoding="utf-8"))
    src = wbp if args.no_recalc else recalc_copy(wbp)
    wb = load_workbook(src, data_only=True)

    bad, n = [], 0
    for key, ref in cmap.items():
        kind, name, per = key.split("|")
        sheet, cell = ref.split("!")
        got = wb[sheet][cell].value
        if kind == "check":
            exp = True if name == "ALL" else checks[name]["pass"]
        elif kind == "quarterly":
            exp = res["Q"][(name, per)]
        else:
            k = float(per) if name == "sens_rev_per_gpu_hour" else per
            exp = res["M"].get((name, k))
        n += 1
        if got is None and exp is None:
            continue
        if isinstance(exp, bool) or isinstance(got, bool):
            ok = bool(got) == bool(exp)
        else:
            ok = got is not None and exp is not None and abs(float(got) - float(exp)) <= args.tol * max(1.0, abs(float(exp)))
        if not ok:
            bad.append((key, ref, got, exp))
    if all(wb[r.split("!")[0]][r.split("!")[1]].value is None for r in cmap.values()):
        print("No cached values found. Recalculate first (LibreOffice, or open and save in Excel).")
        return 2
    print(f"parity: {n - len(bad)}/{n} outputs match")
    for key, ref, got, exp in bad[:30]:
        print(f"  MISMATCH {key} at {ref}: workbook={got} model={exp}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
