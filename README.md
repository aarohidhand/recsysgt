# gtgen — Negotiation Ground-Truth Generator

`gtgen` builds turn-based procurement negotiation ground truth from government bid constraints
(`final_constraints.csv`) and product/supplier evidence. The main outputs are JSONL conversations,
rankings, and audit-friendly compliance details.

The pipeline is designed to be run **one bid per process**. A normal live batch uses
`scripts/live_all_bids.sh`, which starts one `pilot` subprocess per bid, logs each bid separately, and
checkpoints finished conversations before starting the next bid.

## Requirements

- Python 3.11+
- The packages in `requirements.txt`
- OpenRouter API keys (five role-specific keys are recommended; see below)
- The raw CSV data (not committed to the public repository)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env
# edit .env with real keys
```

## API keys — five unique keys

The project expects five OpenRouter keys, one per role/account. Using a different account per
variable spreads the per-key rate limits across the run:

| Variable | Used by |
|---|---|
| `OPENROUTER_API_KEY_PREP` | reframe, doc_classify, align, normalise |
| `OPENROUTER_API_KEY_CLAIMS` | doc_claim, claim_check |
| `OPENROUTER_API_KEY_PERSONA` | persona generation |
| `OPENROUTER_API_KEY_BUYER` | buyer opening/reply/reveal/check |
| `OPENROUTER_API_KEY_NEGOTIATOR` | every negotiator prompt |

`OPENROUTER_API_KEY` is the fallback used when a role variable is missing. Do not put real keys in
the repository; `.env` is git-ignored. `config/settings.yaml` also defines round-robin pools for
`claims`, `buyer`, and `negotiator`, so those stages can use all five keys instead of waiting on one
account's token pacer.

The configured model is the free `google/gemma-4-26b-a4b-it:free` route through Google AI Studio.
The code enforces per-key pacing and does not use a paid model. Check the limits in
`config/settings.yaml` before a large run.

## Data folder layout

A data folder is a directory containing the raw CSVs. The minimum useful files for a live pilot are:

```text
my-data-folder/
├── final_constraints.csv       # bids + buyer constraints (required)
├── products.csv                # candidate products
├── suppliers.csv               # supplier records
├── product_constraints.csv     # extracted product-side constraints
├── products_with_constraints.csv
├── gem_bids_metadata.csv       # optional; used by the METADATA fallback
├── supplier_products.csv       # optional/auxiliary
└── bids.txt                    # optional allowlist (see below)
```

`data/canonical/` parquet files are used by some analysis/coverage tools, but the live pilot reads
the raw CSVs. `data/raw/` is the default folder; `data/canonical/` is the default canonical path.

### ### Persona count per product category

`gtgen.llm.strata.personas_for_category` picks the number of persona sub-bands from the bid's `product_category`:

| Category match | Personas |
|---|---|
| `blanket` / `pump` (e.g. Submersible Pump) | 1 |
| `cable` / `transformer` / `valve` / `sluice` (e.g. Submersible Cable, Sluice Valve) | 2 |
| `office bag` / `laptop` (e.g. Laptop / Office Bag) | 3 |
| Everything else (uncatalogued, e.g. DI Fittings) | 1 |

The runner passes `--personas 1` by default (`scripts/live_all_bids.sh`); the category still selects the tau sub-bands inside `build_scenario()` via `personas_for_category`. Use more bids if you need more conversations.

`bids.txt` — optional allowlist

`bids.txt` contains **one full bid id per line**:

```text
GEM/2019/B/209245
GEM/2019/B/231682
GEM/2019/B/242710
GEM/2019/B/341930
GEM/2020/B/488257
```

Blank lines and lines beginning with `#` are ignored. Duplicate ids are collapsed. The runner looks
for the allowlist in this order:

1. `BIDS_FILE` if you set it explicitly
2. `<data-folder>/bids.txt`
3. `bids.txt` in the repository root
4. `<data-folder>/bid_ids.txt` (legacy alias, only if it is non-empty)

If a non-empty allowlist is found, **only those bids run**. If no allowlist is found, the runner
falls back to the folder's `final_constraints.csv` and runs every bid that has extracted constraints.
An explicitly supplied or discovered `bids.txt` that is empty is an error rather than a silent
all-bids run. Use `BIDS_FILE=-` to deliberately ignore an allowlist.

`bids.txt` affects the all-bids wrapper. The direct `python -m gtgen.cli pilot --bid ...` command
always runs exactly the bids you pass on its command line.

## Running an all-bids live run

### 1. Using the default `data/raw/` folder

```bash
# Uses data/raw/final_constraints.csv when data/raw/bids.txt is absent
scripts/live_all_bids.sh

# Preview the bid list without making any LLM calls
DRY_RUN=1 scripts/live_all_bids.sh

# Run only the bids in data/raw/bids.txt (if present)
scripts/live_all_bids.sh
```

### 2. Using a separate data folder

```bash
# Folder contains raw CSVs; bids.txt is optional
scripts/live_all_bids.sh /path/to/my-data-folder

# Folder + bids.txt: only the listed bids run
scripts/live_all_bids.sh /path/to/my-data-folder

# Folder without bids.txt: all runnable bids in that folder's final_constraints.csv run
scripts/live_all_bids.sh /path/to/my-data-folder

# Preview first
DRY_RUN=1 scripts/live_all_bids.sh /path/to/my-data-folder
```

You can also use environment variables:

```bash
DATA_DIR=/path/to/my-data-folder \
BIDS_FILE=/path/to/my-data-folder/bids.txt \
scripts/live_all_bids.sh
```

### 3. One bid, or a short explicit list

```bash
scripts/live_all_bids.sh GEM/2019/B/242710
scripts/live_all_bids.sh GEM/2019/B/242710 GEM/2019/B/231682
BIDS="GEM/2019/B/242710 GEM/2019/B/231682" scripts/live_all_bids.sh
```

### Useful all-bids options

| Variable | Meaning |
|---|---|
| `DATA_DIR` | Folder containing raw CSVs (default `data/raw`) |
| `BIDS_FILE` | Allowlist path; `-` disables the allowlist |
| `BIDS` | Space-separated explicit bid ids; overrides the allowlist |
| `METADATA` | CSV used when no allowlist exists (default `<DATA_DIR>/final_constraints.csv`) |
| `START` | 0-based offset into the bid list |
| `LIMIT` | Maximum number of bids; `0` means no limit |
| `FRESH=1` | Ignore cached LLM responses; every call is live |
| `LOG_DIR` | Per-bid logs and `failures.txt` (default `out/logs`) |
| `DRY_RUN=1` | Print the resolved bid list and exit |
| `GTGEN_RAW_DIR` | Raw-data override for direct CLI use |
| `GTGEN_CANONICAL_DIR` | Canonical-data override |
| `GTGEN_OUT_DIR` | Output override |
| `GTGEN_LLM_CACHE` | Cache override |

The runner always passes `--personas 1` per bid. D080 caps a pilot at one persona per bid; use more
bids if you need more conversations. The runner continues to the next bid if one bid fails and
records the failure in `out/logs/failures.txt`.

## Running one bid directly

```bash
python -m gtgen.cli pilot --live --personas 1 --bid GEM/2019/B/242710
python -m gtgen.cli pilot --live --personas 1 --fresh --bid GEM/2019/B/242710
python -m gtgen.cli pilot --live --personas 1 --data-dir /path/to/my-data-folder \
  --bid GEM/2019/B/242710
```

`--fresh` ignores the LLM cache for reads, so every LLM call is live. A normal run reuses cached
prompts/responses where possible. The cache may contain full prompts and responses; treat `.cache/`
as local data.

## Outputs, logs, and checkpoints

```text
out/
├── logs/
│   ├── GEM_2019_B_242710.log
│   └── failures.txt
└── gt/
    └── pilot_<timestamp>_<hex>/
        ├── run_meta.json
        ├── conversations.jsonl
        └── skipped.jsonl
```

- `run_meta.json` is written before the first LLM call.
- Each finished conversation is appended and fsynced to `conversations.jsonl` immediately.
- `skipped.jsonl` records bids/scenarios that failed validation or preparation.
- `out/logs/failures.txt` lists bids whose subprocess exited non-zero.

Pretty-print a run:

```bash
python show_conv.py out/gt/<run_id>/conversations.jsonl
python show_conv.py out/gt/<run_id>/conversations.jsonl 242710
```

Evaluate a run:

```bash
python -m gtgen.cli eval --run <run_id>
```

## Audits and tests

Run the full test suite from the repository root:

```bash
pytest -q
```

The public upload uses a smaller default test selection in `pyproject.toml`. Run the focused
live/audit tests directly:

```bash
pytest -q tests/llm/test_d054_live.py tests/llm/test_api_keys.py tests/dialogue/test_d088.py
```

Deterministic audit checks on a completed run:

```bash
python -m gtgen.eval.persona_diversity --run <run_id>
python -m gtgen.eval.realism --run <run_id>
```

`python -m gtgen.cli eval --run <run_id>` also prints persona-diversity and realism summaries.
`audit/` contains repository/data-flow audit reports; `docs/decisions.md` records the design
decisions (D080, D082–D090, and earlier) that explain why the pipeline behaves the way it does.

## Important operational notes

- **Do not modify `data/raw/final_constraints.csv`.** It is the user-expanded source constraint
  table. Add or fix data through a separate, documented process.
- **Do not commit** `data/`, `out/`, `.cache/`, `.env`, or `.venv/`. They are git-ignored in the
  main repository and excluded from the public upload.
- **One bid per process.** Do not run multiple live bids in parallel from one process; the rate
  limiter and checkpoints assume sequential bids.
- **Check the first live call.** A failing first live call stops that bid immediately so an auth or
  provider problem is visible in the log instead of being retried across the whole batch.
- **A `bids.txt` allowlist is authoritative.** If it is present and non-empty, no other bids run.
  If you want the old all-bids behavior, remove/rename it or use `BIDS_FILE=-`.
- **Folder runs still write outputs to `out/` by default.** Set `GTGEN_OUT_DIR` if you want run
  artifacts next to an external data folder.
- **The five role keys should belong to different accounts.** Sharing one account defeats the
  round-robin pools and makes token/request pacing the bottleneck.
