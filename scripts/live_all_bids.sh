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
#   scripts/live_all_bids.sh                         # all runnable bids (final_constraints.csv), sequentially
#   scripts/live_all_bids.sh GEM/2019/B/242710       # exactly one bid
#   START=1 LIMIT=2 scripts/live_all_bids.sh         # window into the bid list
#   FRESH=1 scripts/live_all_bids.sh GEM/2019/B/242710   # ignore the LLM cache
#   METADATA=data/raw/gem_bids_metadata.csv scripts/live_all_bids.sh  # all metadata bids
#
# Default source is final_constraints.csv: only bids with extracted buyer constraints can
# produce a scenario.  gem_bids_metadata.csv has 1140 bids, but the other 1134 have no
# final_constraints rows and would be skipped by the validity gate.
set -u
set -o pipefail

cd "$(dirname "$0")/.." || exit 1

METADATA="${METADATA:-data/raw/final_constraints.csv}"
LOG_DIR="${LOG_DIR:-out/logs}"
START="${START:-0}"          # 0-based offset into the deduplicated bid list
LIMIT="${LIMIT:-0}"          # 0 = no limit
FRESH="${FRESH:-0}"          # 1 = pass --fresh (every LLM call is live)
BIDS="${BIDS:-}"             # optional space-separated bid ids; empty = read METADATA

if [[ $# -gt 0 ]]; then
    BIDS="$*"
fi

mkdir -p "$LOG_DIR"
failures="$LOG_DIR/failures.txt"
: > "$failures"

run_one() {
    local bid="$1"
    local log="$LOG_DIR/${bid//\//_}.log"
    local args=(pilot --live --personas 1 --bid "$bid")
    if [[ "$FRESH" == "1" ]]; then
        args+=(--fresh)
    fi
    echo "[$(date -u +%H:%M:%S)] $bid -> $log"
    if python -m gtgen.cli "${args[@]}" >"$log" 2>&1; then
        echo "  ok"
    else
        local rc=$?
        echo "  FAILED rc=$rc (see $log)"
        echo "$bid rc=$rc" >>"$failures"
    fi
}

if [[ -n "$BIDS" ]]; then
    for bid in $BIDS; do
        run_one "$bid"
    done
    echo "done: $(wc -l <"$failures") failed"
    exit 0
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
print("\n".join(bids))
PY
)

for bid in "${bids[@]}"; do
    run_one "$bid"
done

echo "done: ${#bids[@]} bid(s), $(wc -l <"$failures") failed; failures: $failures"
