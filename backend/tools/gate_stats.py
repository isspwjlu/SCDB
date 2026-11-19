"""Count what the extraction gates rejected.

The extraction log on the master records every value the gates threw
away, with the reason, and a per-paper summary line. This turns that
log into the numbers the paper quotes: how many records the LLM
proposed, how many extractions each gate rejected, and by which field.

A paper that was extracted more than once is counted once -- the later
run wins, because it is the one whose result is in the database.

Usage::

    python3 gate_stats.py [path/to/extraction.log] > gate_stats.json
"""

from __future__ import annotations

import collections
import json
import re
import sys

# "--- ingest 10.48550/arXiv.1904.01023v3 ---"
PAPER = re.compile(r"--- ingest (\S+) ---")
# "  extracted=3 stored=1 skipped_hallucination=0"
SUMMARY = re.compile(
    r"\[INFO\]\s+extracted=(\d+) stored=(\d+) skipped_hallucination=(\d+)\s*$"
)
# "LLM extracted 2 records"
PROPOSED = re.compile(r"LLM extracted (\d+) records")
# "dropping Tc for FeSe — no verbatim evidence found in the paper"
DROP = re.compile(r"dropping (\w+) for (.+?) — (.*)$")

EVIDENCE = "no verbatim evidence found"
BINDING = "is not stated for this material"
CALCULATION = "is a calculation"
OTHER = "other"


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "extraction.log"

    # Per paper, the last run wins.
    papers: dict[str, dict[str, object]] = {}
    current: str | None = None
    # The drops of the run in progress, by reason and by field.
    run_reason: collections.Counter = collections.Counter()
    run_field: collections.Counter = collections.Counter()

    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = PAPER.search(line)
            if match:
                current = match.group(1)
                run_reason = collections.Counter()
                run_field = collections.Counter()
                continue
            match = DROP.search(line)
            if match:
                field, _material, reason = match.groups()
                if EVIDENCE in reason:
                    key = EVIDENCE
                elif BINDING in reason:
                    key = BINDING
                elif CALCULATION in reason:
                    key = CALCULATION
                else:
                    key = OTHER
                run_reason[key] += 1
                run_field[f"{field} · {key}"] += 1
                continue
            match = SUMMARY.search(line)
            if match and current:
                extracted, stored, skipped = (int(x) for x in match.groups())
                papers[current] = {
                    "extracted": extracted,
                    "stored": stored,
                    "skipped_hallucination": skipped,
                    "reasons": dict(run_reason),
                    "fields": dict(run_field),
                }
                continue

    totals = {
        "papers": len(papers),
        "records_proposed": sum(int(p["extracted"]) for p in papers.values()),
        "records_stored": sum(int(p["stored"]) for p in papers.values()),
        "skipped_hallucination": sum(
            int(p["skipped_hallucination"]) for p in papers.values()
        ),
    }
    by_reason: collections.Counter = collections.Counter()
    by_field: collections.Counter = collections.Counter()
    for record in papers.values():
        by_reason.update(record["reasons"])  # type: ignore[arg-type]
        by_field.update(record["fields"])  # type: ignore[arg-type]
    totals["fields_rejected"] = sum(by_reason.values())

    print(
        json.dumps(
            {
                "totals": totals,
                "rejected_by_reason": dict(by_reason),
                "rejected_by_field": by_field.most_common(40),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
