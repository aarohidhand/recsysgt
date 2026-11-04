#!/usr/bin/env python3
"""Pretty-print GT conversations.jsonl turns to the terminal.
Usage: python show_conv.py out/gt/<run_id>/conversations.jsonl [scenario_id_substring]
"""
import json, sys

path = sys.argv[1]
needle = sys.argv[2] if len(sys.argv) > 2 else None

with open(path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        conv = json.loads(line)
        if needle and needle not in conv["scenario_id"]:
            continue
        print("=" * 80)
        print(f"{conv['scenario_id']}  outcome={conv['final']['outcome']}  "
              f"tau={conv['persona'].get('tau')}  turns={len(conv['turns'])}")
        print("=" * 80)
        for t in conv["turns"]:
            print(f"\n[{t['turn_idx']}] {t['speaker']} / {t['action']}")
            print(t["utterance"])
        print()