# AI data center leases

A dataset of signed AI data center leases from SEC filings (2024–2026), with the price of each lease per kW of critical IT load. It also contains a per-megawatt model of CoreWeave, used as the tenant view.

Every number has the exact sentence from its source filing. A script downloads each filing and checks that the sentence is in it and that the number is in the sentence.

Blog post: [link]

## Data

| File | Contents |
|---|---|
| `data/lease_panel.csv` | All screened filings. Rows with status `REVIEWED` are signed leases with price terms; `NO_TERMS` are signed leases without them; `EXCLUDED` rows give a `reason_code`. |
| `data/lease_decisions.csv`, `data/lease_additions.csv`, `data/lease_enrichment.csv` | Every review decision, split row and added field, applied by `scripts/apply_lease_decisions.py`. |
| `data/sources.csv` | Sources for the CoreWeave model and power data. |
| `raw/manifest.csv`, `raw/leases/manifest.csv` | URL and SHA-256 hash of each downloaded source. The documents themselves are not in the repository. |

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SEC_USER_AGENT="Your Name your.email@example.com"   # required by the SEC

python3 scripts/fetch_sources.py          # CoreWeave and power sources
python3 scripts/leases_scrape.py fetch    # lease filings
python3 scripts/run_all.py                # model, checks, verification, lease analysis
python3 scripts/lease_charts.py           # charts -> output/charts/
python3 tests/test_pipeline.py            # tests
```

To search SEC filings for new lease candidates: `python3 scripts/leases_scrape.py all`.

## Method

Pipeline details: [`docs/pipeline.md`](docs/pipeline.md).

- Rent = filed average annual revenue ÷ MW, or contract value ÷ term ÷ MW, in $ per kW per month.
- MW is critical IT load where the filing states it. Leases with an unstated basis are reported separately.
- One row per lease. Duplicates, letters of intent, portfolio totals and GPU cloud contracts are excluded.

## Limits

Listed landlords that file with the SEC only. Only leases that disclose terms can be priced. Contract values include escalators, so each rent is an average over the term.

## License

[choose: e.g. MIT for code, CC BY 4.0 for data]
