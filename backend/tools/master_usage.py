"""Recompute every pipeline number the paper quotes, from the master's log.

The extraction log on the master grows as the corpus grows, so the
numbers in the paper have to be recomputed and stored, not remembered.
This script runs the same counting on the master and writes the result
to backend/data/cache/pipeline_stats.json, which is what the paper
quotes.

Usage::

    D:\\anaconda3\\python.exe tools/master_usage.py
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\42033\.claude")
from ssh_login import connect, run  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
OUT = BACKEND / "data" / "cache" / "pipeline_stats.json"

REMOTE_PY = r'''
import json
import re

LOG = "CORPUS_DIR/extraction.log"

PAPER = re.compile(r"--- ingest (\S+) ---")
SUMMARY = re.compile(
    r"\[INFO\]\s+extracted=(\d+) stored=(\d+) skipped_hallucination=(\d+)\s*$"
)
PROPOSED = re.compile(r"LLM extracted (\d+) records")
DROP = re.compile(r"dropping (\w+) for (.+?) — (.*)$")
USAGE = re.compile(
    r"LLM usage: (\d+) calls, prompt (\d+) \(cached (\d+)\), "
    r"completion (\d+) \(reasoning (\d+)\)"
)

EVIDENCE = "no verbatim evidence found"
BINDING = "is not stated for this material"
CALCULATION = "is a calculation"
OTHER = "other"

# Per paper, the last run wins -- the same caliber the gate statistics use.
papers = {}
current = None
run_reason = {}
run_field = {}
run_proposed = 0
run_usage = None
segments = {}
seg_prev = None
usage_lines = 0

FIELDS = ("calls", "prompt", "cached", "completion", "reasoning")
usage_total = dict.fromkeys(FIELDS, 0)

with open(LOG, encoding="utf-8", errors="replace") as handle:
    for line in handle:
        found = PAPER.search(line)
        if found:
            current = found.group(1)
            run_reason = {}
            run_field = {}
            run_proposed = 0
            continue

        found = PROPOSED.search(line)
        if found:
            run_proposed = int(found.group(1))
            continue

        found = DROP.search(line)
        if found:
            field, _material, reason = found.groups()
            if EVIDENCE in reason:
                key = EVIDENCE
            elif BINDING in reason:
                key = BINDING
            elif CALCULATION in reason:
                key = CALCULATION
            else:
                key = OTHER
            run_reason[key] = run_reason.get(key, 0) + 1
            run_field[f"{field} · {key}"] = run_field.get(f"{field} · {key}", 0) + 1
            continue

        found = USAGE.search(line)
        if found:
            usage_lines += 1
            values = dict(zip(FIELDS, (int(x) for x in found.groups())))
            if seg_prev is not None and values["calls"] < seg_prev["calls"]:
                for key in FIELDS:
                    usage_total[key] += seg_prev[key]
            seg_prev = values
            continue

        found = SUMMARY.search(line)
        if found and current:
            extracted, stored, skipped = (int(x) for x in found.groups())
            papers[current] = {
                "extracted": extracted,
                "stored": stored,
                "skipped_hallucination": skipped,
                "proposed": run_proposed,
                "reasons": run_reason,
                "fields": run_field,
            }
            continue

if seg_prev is not None:
    for key in FIELDS:
        usage_total[key] += seg_prev[key]

by_reason = {}
by_field = {}
for record in papers.values():
    for key, count in record["reasons"].items():
        by_reason[key] = by_reason.get(key, 0) + count
    for key, count in record["fields"].items():
        by_field[key] = by_field.get(key, 0) + count

usage_total["usage_lines"] = usage_lines
usage_total["total_tokens"] = usage_total["prompt"] + usage_total["completion"]

result = {
    "papers": len(papers),
    "records_proposed": sum(r["proposed"] for r in papers.values()),
    "records_extracted": sum(r["extracted"] for r in papers.values()),
    "records_stored": sum(r["stored"] for r in papers.values()),
    "skipped_hallucination": sum(r["skipped_hallucination"] for r in papers.values()),
    "fields_rejected": sum(by_reason.values()),
    "rejected_by_reason": by_reason,
    "rejected_by_field": sorted(by_field.items(), key=lambda kv: -kv[1]),
    "llm_usage": usage_total,
}
print(json.dumps(result, indent=2))
'''


def main() -> int:
    payload = base64.b64encode(REMOTE_PY.encode("utf-8")).decode("ascii")
    ssh = connect()
    try:
        out = run(
            f"echo {payload} | base64 -d > /tmp/_pipeline_stats.py && "
            "python3 /tmp/_pipeline_stats.py",
            ssh=ssh,
        )
    finally:
        ssh.close()

    marker = "}"
    start = out.find("{")
    data = json.loads(out[start:])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    for key in (
        "papers",
        "records_proposed",
        "records_extracted",
        "records_stored",
        "skipped_hallucination",
        "fields_rejected",
    ):
        print(f"  {key:24} {data[key]}")
    print("  rejected_by_reason:", data["rejected_by_reason"])
    print("  llm_usage:", data["llm_usage"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
