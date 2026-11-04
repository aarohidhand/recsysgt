# gtgen — Negotiation Ground-Truth Generator

Pipeline for generating turn-based procurement negotiation ground truth from government bid
constraints and product evidence.

## What is included

- `src/gtgen/` — pipeline code (data loading, matching, dialogue, LLM runners, ranking, evaluation)
- `prompts/` — versioned prompt files
- `config/settings.yaml` — runtime settings and role-based API-key configuration
- `scripts/` — live-run and diagnostic scripts
- `docs/`, `audit/`, `reports/` — design notes, decisions, and audit reports
- `tests/llm/test_d054_live.py` — live-mode/cache/provenance tests
- `tests/llm/test_api_keys.py` — role/key-pool tests
- `tests/dialogue/test_d088.py` — alignment/normalisation audit tests
- shared test helpers needed by those tests

## What is not included

- `data/` — raw and canonical source data
- `out/` — generated conversations and run logs
- `.cache/` — LLM response cache
- `.env` — API keys and local secrets

The pipeline expects the raw CSVs under `data/raw/` (for example `final_constraints.csv`,
`products.csv`, `suppliers.csv`, `product_constraints.csv`, and `gem_bids_metadata.csv`). See
`docs/dataset_specifications.md` for the expected layout.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env
# edit .env with your API keys
```

## Run

```bash
# list options
python -m gtgen --help

# one live bid, one persona, no cache reads
python -m gtgen pilot --live --fresh --bid GEM/2019/B/209245
```

Live mode uses the API-key roles and pools configured in `config/settings.yaml`; put the actual
key values in `.env` or your shell environment.

## Tests

```bash
pytest
```

`pyproject.toml` restricts the default test run to the included live/audit tests. The
`tests/llm/test_client.py` module is included because `test_api_keys.py` imports its shared fake
client helpers; it is not part of the default test selection.
