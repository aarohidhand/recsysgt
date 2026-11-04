#!/usr/bin/env bash
# D084: run the live pilot ONE BID AT A TIME, each in its own subprocess.
#
# Why one bid per process:
#   * a fresh LLMClient / rate-limiter state per bid (no cross-bid pacing surprises);
#   * one log per bid, so a failed bid is easy to inspect and re-run;
#   * the next bid starts only after the previous one finishes (no parallel key pressure).
#
# The claims, buyer, and negotiator stages use the round-robin key pools from config/settings.yaml
# (llm.api_key_pools), so the five accounts are used instead of only one account per role.  Personas inside a bid
# are chosen by gtgen.llm.strata.personas_for_category; the CLI's --personas is always 1 (D080).
#
# Each bid gets its own out/gt/<run_id>/ checkpoint: run_meta.json is written before the first LLM call and the
# finished conversation is appended to conversations.jsonl (fsync) before the next bid starts (D086).
#
# Usage:
#   scripts/live_all_bids.sh                         # all runnable bids (data/raw/final_constraints.csv)
#   scripts/live_all_bids.sh /path/to/data-folder    # folder's bids.txt if present, else folder's final_constraints.csv
#   scripts/live_all_bids.sh GEM/2019/B/242710       # exactly one bid
#   scripts/live_all_bids.sh /path/to/data-folder GEM/2019/B/242710
#   BIDS_FILE=/path/to/bids.txt DATA_DIR=/path/to/data-folder scripts/live_all_bids.sh
#   BIDS_FILE=- scripts/live_all_bids.sh             # ignore bids.txt and run the whole metadata list
#   START=1 LIMIT=2 scripts/live_all_bids.sh         # window into the bid list
#   FRESH=1 scripts/live_all_bids.sh GEM/2019/B/242710   # ignore the LLM cache
#   METADATA=data/raw/gem_bids_metadata.csv scripts/live_all_bids.sh  # all metadata bids
#
# A bids file is one full bid id per line. Blank lines and lines starting with '#' are ignored.
# If a non-empty bids file is present, it is the allowlist: only those bids run. If it is absent,
# the runner falls back to METADATA (default: the folder's final_constraints.csv).
#
# Default source is final_constraints.csv: only bids with extracted buyer constraints can
# produce a scenario.  gem_bids_metadata.csv has 1140 bids, but the other 1134 have no
# final_constraints rows and would be skipped by the validity gate.
set -u
set -o pipefail

# A directory as the first argument is the data folder. Resolve it before changing directory.
if [[ $# -gt 0 && -d "$1" ]]; then
    DATA_DIR="$(cd "$1" && pwd)"
    shift
fi

DATA_DIR="${DATA_DIR:-data/raw}"
cd "$(dirname "$0")/.." || exit 1

# Make a relative DATA_DIR relative to the repository root (where the runner now is).
if [[ "$DATA_DIR" != /* ]]; then
    DATA_DIR="$(cd "$DATA_DIR" 2>/dev/null && pwd)" || {
        echo "ERROR: data folder not found: $DATA_DIR" >&2
        exit 1
    }
fi

# The CLI reads paths.raw from settings.yaml; these env overrides let it use a folder outside data/raw.
export GTGEN_RAW_DIR="$DATA_DIR"
if [[ -d "$DATA_DIR/canonical" ]]; then
    export GTGEN_CANONICAL_DIR="$DATA_DIR/canonical"
fi

METADATA="${METADATA:-$DATA_DIR/final_constraints.csv}"
LOG_DIR="${LOG_DIR:-out/logs}"
START="${START:-0}"          # 0-based offset into the deduplicated bid list
LIMIT="${LIMIT:-0}"          # 0 = no limit
FRESH="${FRESH:-0}"          # 1 = pass --fresh (every LLM call is live)
BIDS="${BIDS:-}"             # optional space-separated bid ids; empty = bids file / METADATA
BIDS_FILE="${BIDS_FILE:-}"   # optional path; '-' disables the bids-file allowlist

if [[ $# -gt 0 ]]; then
    BIDS="$*"
fi

# Resolve the default allowlist only when the caller did not name one explicitly.
if [[ "$BIDS_FILE" == "-" ]]; then
    BIDS_FILE=""
elif [[ -z "$BIDS_FILE" ]]; then
    for candidate in "$DATA_DIR/bids.txt" "bids.txt"; do
        if [[ -f "$candidate" ]]; then
            BIDS_FILE="$candidate"
            break
        fi
    done
    # Backwards-compatible alias, but only when it actually contains bids.
    if [[ -z "$BIDS_FILE" && -s "$DATA_DIR/bid_ids.txt" ]]; then
        BIDS_FILE="$DATA_DIR/bid_ids.txt"
    fi
fi

mkdir -p "$LOG_DIR"
failures="$LOG_DIR/failures.txt"
: > "$failures"

run_one() {
    local bid="$1"
    local log="$LOG_DIR/${bid//\//_}.log"
    local args=(pilot --live --personas 1 --bid "$bid" --data-dir "$DATA_DIR")
    if [[ "$FRESH" == "1" ]]; then
        args+=(--fresh)
    fi
    echo "[$(date -u +%H:%M:%S)] $bid -> $log"
    if python -m gtgen.cli "${args[@]}" >"$log" 2>&1; then
        # The CLI exits 0 after a per-bid skip (D066), so a clean exit is not enough: report the skip as a failure.
        if grep -q 'SKIPPED ' "$log"; then
            echo "  SKIPPED (see $log)"
            echo "$bid skipped" >>"$failures"
        else
            echo "  ok"
        fi
    else
        local rc=$?
        echo "  FAILED rc=$rc (see $log)"
        echo "$bid rc=$rc" >>"$failures"
    fi
}

# Explicit bid ids on the command line / in BIDS override any file.
if [[ -n "$BIDS" ]]; then
    read -r -a bids <<< "$BIDS"
elif [[ -n "$BIDS_FILE" ]]; then
    if [[ ! -f "$BIDS_FILE" ]]; then
        echo "ERROR: bids file not found: $BIDS_FILE" >&2
        exit 1
    fi
    mapfile -t bids < <(python - "$BIDS_FILE" "$START" "$LIMIT" <<'PY'
import pathlib
import sys

path, start, limit = pathlib.Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
seen: set[str] = set()
bids: list[str] = []
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    bid = line.split()[0]              # allow "GEM/... # note"
    if bid not in seen:
        seen.add(bid)
        bids.append(bid)
if start:
    bids = bids[start:]
if limit:
    bids = bids[:limit]
print("\n".join(bids), end="")
PY
)
    if [[ ${#bids[@]} -eq 0 ]]; then
        echo "ERROR: bids file is empty (or START/LIMIT removed every bid): $BIDS_FILE" >&2
        echo "       Add one full bid id per line, or use BIDS_FILE=- to run the metadata list." >&2
        exit 1
    fi
else
    if [[ ! -f "$METADATA" ]]; then
        echo "ERROR: metadata not found: $METADATA" >&2
        exit 1
    fi
    mapfile -t bids < <(python - "$METADATA" "$START" "$LIMIT" <<'PY'
import csv
import sys

path, start, limit = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
seen: set[str] = set()
bids: list[str] = []
with open(path, newline="", encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        # final_constraints uses bid_id; gem_bids_metadata uses bid_number.
        bid = (row.get("bid_id") or row.get("bid_number") or "").strip()
        if bid and bid not in seen:
            seen.add(bid)
            bids.append(bid)
if start:
    bids = bids[start:]
if limit:
    bids = bids[:limit]
print("\n".join(bids), end="")
PY
)
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    printf '%s\n' "${bids[@]}"
    exit 0
fi

for bid in "${bids[@]}"; do
    run_one "$bid"
done

echo "done: ${#bids[@]} bid(s), $(wc -l <"$failures") failed; failures: $failures"
