# CoreWeave neocloud model — replicable pipeline

Every number in the model lives in a CSV file in `data/` with a source ID. Scripts download
the sources, check each number against them, compute the model in Python, build an Excel
workbook with live formulas, and confirm that the workbook and the Python model agree.

```
data/*.csv ──► model.py ──► reports/model_outputs.csv, reports/checks.csv
     │                                   ▲
     ├──────► build_workbook.py ──► output/coreweave_model.xlsx ──► parity_check.py
     │
sources.csv ──► fetch_sources.py ──► raw/ (+ manifest.csv with SHA-256, Wayback links)
                                          │
                               verify.py ◄┘ ──► reports/verification.csv
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 1. put your name and email in config.yaml -> sec_user_agent (the SEC requires it)
python scripts/run_all.py --fetch --wayback            # download, model, build, parity, verify
python tests/test_pipeline.py                          # or: pytest
```

Without `--fetch`, the pipeline runs fully offline on the files already in `raw/`.
Parity needs LibreOffice (`soffice` on PATH). Without it, open the workbook in Excel,
save it, and run `python scripts/parity_check.py --no-recalc`.

## Lease panel (next post): what does a powered megawatt cost?

```bash
python scripts/leases_scrape.py all      # search SEC filings, download, extract candidates, add UNREVIEWED rows
# open data/lease_panel.csv; check each value against its evidence sentence and the source;
# fix values, fill missing fields from reports/lease_candidate_facts.csv, set status REVIEWED or REJECTED
python scripts/run_all.py                # verify checks every REVIEWED value in its source; analysis runs on REVIEWED rows
```

After a review pass, reduce the file to the price panel:

```bash
python scripts/reduce_panel.py data/lease_panel_reviewed.csv   # -> data/lease_panel.csv + reports/lease_confirm_queue.csv
```

Statuses: REVIEWED (signed lease with price terms: in the analysis), NO_TERMS (signed lease, terms not
disclosed: counted), CONFIRM (human decision needed, e.g. split a multi-lease row), EXCLUDED with a
`reason_code` (not_a_lease, duplicate, loi, portfolio_total, gpu_cloud_contract, filer_is_tenant).

Review decisions are data too: `data/lease_decisions.csv` (one row per decision) and
`data/lease_additions.csv` (rows split out of multi-lease documents). Apply them with
`python scripts/apply_lease_decisions.py`; it stops if a deal has two canonical rows or if any
value is missing from its evidence sentence.

The scraper proposes; it never confirms. `verify.py` fails a REVIEWED value if its evidence sentence is
not in the source document, or if the value does not appear in the evidence sentence.
Search queries, tenant names and the date range are in `config.yaml` under `leases`.
Outputs: `reports/lease_search_hits.csv`, `lease_candidate_docs.csv`, `lease_candidate_facts.csv`,
`lease_panel_metrics.csv`, `lease_panel_summary.csv`.

## Tenant view per MW (data-only)

`python scripts/stack.py` computes the value of one MW of critical IT load per year, layer by layer
(landlord rent, electricity, GPU capital, other opex, neocloud margin), reconciles it with CoreWeave's
reported cost of revenue, D&A and EBITDA per MW, and measures lease-to-revenue lags.
See `POST_DESIGN.md`. Inputs: `power_definitions.csv`, `power_deals.csv`, `power_ramp.csv`,
`stack_inputs.csv`, `calibration.csv`. Outputs: `reports/stack_*.csv`.

## Data files (`data/`)

| File | One row = | Key columns |
|---|---|---|
| `sources.csv` | one document | `doc_id`, `url`, `doc_date`, `type` (Primary/Secondary/Transcript), `confidence` |
| `reported.csv` | one line item in one reported block (3M, 6M, FY) | `item`, `block`, `value_musd`, `doc_id`, `locator` |
| `balance_sheet.csv` | one balance-sheet line at one date | `item`, `as_of`, `value_musd`, `doc_id` |
| `kpis.csv` | one operating KPI at one quarter end | `value`, `qualifier`, `doc_id`, **`evidence`** (exact phrase from the source) |
| `claims.csv` | one capacity claim | `class` (Announced / Contracted / Active / Available), `as_of_or_target`, `outcome` |
| `assumptions.csv` | one assumption or guidance input | `value`, `basis`, `doc_id`, `confidence`, `is_key` (yellow in workbook) |
| `reconciliation.csv` | one conflict between sources | `figure_a`/`figure_b` (number, `=expr`, or `{item:quarter}` token), `status` |
| `xchecks.csv` | one cross-source check | `model_ref` token, `reference_value`, `tolerance` |
| `ebitda_bridge.csv` | extra non-GAAP bridge items | used by the adjusted-EBITDA bridge check |
| `change_log.csv` | one change to an input | date, old, new, reason |

Standalone quarters are derived from reported blocks by rules in `config.yaml`
(e.g. `Q3'25 = FY25 − 6M'25 − Q4'25`). To add a quarter: add the new blocks to
`reported.csv`, add the block and quarter to `config.yaml`, add KPI rows, and run again.

## How to add or change a number

1. Add the source to `sources.csv` (new `doc_id`).
2. Add or edit the row in the right data file. For KPIs, copy an exact phrase from the source into `evidence`.
3. Log the change in `change_log.csv`.
4. `python scripts/fetch_sources.py --only Dxx` then `python scripts/run_all.py`.

Never type a number into the workbook. It is rebuilt from the CSV files each run.

## Verification statuses (`reports/verification.csv`)

| Status | Meaning |
|---|---|
| PASS | Evidence phrase found in the archived source, or value matches SEC XBRL within tolerance |
| FAIL | Phrase not found, value differs from XBRL, or a raw file changed after download |
| NO_FILE | Source not downloaded yet (or the site blocked the script: save the page as `raw/<doc_id>.html`) |
| NOT_FOUND | No XBRL fact with those dates for the mapped concepts — fix `xbrl_map.yaml` |
| NO_XBRL / SKIP | No mapping, or a non-GAAP line (adjusted EBITDA etc.); check by hand |

Q4 three-month values are checked as FY minus 9M, because 10-K filings report the full year only.

`xbrl_map.yaml` lists candidate XBRL concepts. CoreWeave uses custom concepts for some lines
(for example "Technology and infrastructure"); for those the script searches concept labels.
On the first run, expect some NOT_FOUND rows: open `raw/xbrl/companyfacts.json`, find the
concept, and add it to the map.

## Publishing

- Commit `data/`, `scripts/`, `tests/`, `config.yaml`, `xbrl_map.yaml`, `raw/manifest.csv` and `reports/`.
- Keep third-party articles in `raw/` local (they are copyrighted). The manifest records the URL,
  the SHA-256 hash and the Wayback snapshot, so readers can get the same files.
- SEC filings can be shared freely.

## Current state (first build)

- History: Q1'25 to Q2'26 income statement and cash flow, balance sheets Dec-24/Dec-25/Jun-26, KPIs, 19 capacity claims.
- 26 internal checks pass; workbook and Python model agree on 290 outputs.
- Open items: see `data/reconciliation.csv` (status = Open) and every source with `verified_vs_primary` other than "Yes".
- Not built yet: debt schedule by facility, forecast, downside case, other neoclouds.
