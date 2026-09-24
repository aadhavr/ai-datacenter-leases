"""Shared helpers: paths, config, and data loaders.

All numbers enter the project through the CSV files in data/. Nothing is
hardcoded in the scripts, except layout and formula structure.
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key in ("blocks", "quarters", "rate_periods"):
        for row in cfg.get(key, []):
            for k in ("start", "end", "bs_start", "bs_end"):
                if k in row and isinstance(row[k], str):
                    row[k] = dt.date.fromisoformat(row[k])
    return cfg


def path(cfg: dict, key: str) -> Path:
    return ROOT / cfg["paths"][key]


def read_csv(name: str) -> list[dict]:
    with open(ROOT / "data" / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(x):
    """Parse a CSV cell to float. Empty -> None. Keep strings like '=1.5/4.2' or '{token}'."""
    if x is None:
        return None
    s = str(x).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return s


def ordered_unique(seq):
    seen, out = set(), []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def load_data() -> dict:
    rep_rows = read_csv("reported.csv")
    bs_rows = read_csv("balance_sheet.csv")
    kpi_rows = read_csv("kpis.csv")
    d = {
        "items": ordered_unique(r["item"] for r in rep_rows),
        "reported": {(r["item"], r["block"]): num(r["value_musd"]) for r in rep_rows},
        "reported_rows": rep_rows,
        "bs_items": ordered_unique(r["item"] for r in bs_rows),
        "bs_dates": ordered_unique(r["as_of"] for r in bs_rows),
        "bs": {(r["item"], r["as_of"]): num(r["value_musd"]) for r in bs_rows},
        "bs_rows": bs_rows,
        "kpi_metrics": ordered_unique(r["metric"] for r in kpi_rows),
        "kpis": {(r["metric"], r["quarter"]): r for r in kpi_rows},
        "assumptions": {r["key"]: {**r, "value": num(r["value"])} for r in read_csv("assumptions.csv")},
        "reconciliation": read_csv("reconciliation.csv"),
        "xchecks": read_csv("xchecks.csv"),
        "claims": read_csv("claims.csv"),
        "sources": read_csv("sources.csv"),
        "change_log": read_csv("change_log.csv"),
        "bridge": read_csv("ebitda_bridge.csv"),
    }
    return d


def validate(data: dict, cfg: dict) -> list[str]:
    """Structural checks on the data files. Returns a list of problems."""
    problems = []
    doc_ids = {s["doc_id"] for s in data["sources"]}
    block_labels = [b["label"] for b in cfg["blocks"]]
    for item in data["items"]:
        for b in block_labels:
            if (item, b) not in data["reported"]:
                problems.append(f"reported.csv: missing {item!r} for block {b!r}")
    for r in data["reported_rows"] + data["bs_rows"]:
        if r["doc_id"] not in doc_ids:
            problems.append(f"unknown doc_id {r['doc_id']!r} in row {r}")
    for (m, q), r in data["kpis"].items():
        if r["doc_id"] not in doc_ids:
            problems.append(f"kpis.csv: unknown doc_id {r['doc_id']!r} for {m} {q}")
        if not r["evidence"].strip():
            problems.append(f"kpis.csv: no evidence text for {m} {q}")
    for fname in ("power_definitions.csv", "power_deals.csv", "power_ramp.csv", "calibration.csv"):
        for r in read_csv(fname):
            if r["doc_id"] not in doc_ids:
                problems.append(f"{fname}: unknown doc_id {r['doc_id']!r}")
            if not r.get("evidence", "").strip():
                problems.append(f"{fname}: no evidence text in row {r}")
    for c in data["claims"]:
        if c["doc_id"] not in doc_ids:
            problems.append(f"claims.csv: unknown doc_id {c['doc_id']!r} for {c['claim_id']}")
    return problems
