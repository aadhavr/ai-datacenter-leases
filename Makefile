PY ?= python
.PHONY: all fetch model stack build parity verify test leases lease-analysis
all:    ; $(PY) scripts/run_all.py
fetch:  ; $(PY) scripts/fetch_sources.py --wayback
model:  ; $(PY) scripts/model.py
stack:  ; $(PY) scripts/stack.py
build:  ; $(PY) scripts/build_workbook.py
parity: ; $(PY) scripts/parity_check.py
verify: ; $(PY) scripts/verify.py
test:   ; $(PY) tests/test_pipeline.py
leases: ; $(PY) scripts/leases_scrape.py all
lease-analysis: ; $(PY) scripts/leases_analyze.py
