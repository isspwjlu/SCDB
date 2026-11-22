"""Recompute the pipeline statistics from the extraction log.

The extraction log grows as the corpus grows, so the numbers the paper
quotes have to be computed at a fixed moment rather than recounted
whenever the script runs.  The moment is the release: the script is
given the time of the corpus snapshot, and only the log lines up to
that time are counted.  Everything the log holds after the release
belongs to the next one.

A paper that was extracted more than once is counted once -- the later
run wins, because it is the one whose result is in the database.

The result is written to ``backend/data/cache/pipeline_stats.json``;
``tools/freeze_audit.py`` copies it into the paper's own directory.

Usage::

    python tools/master_usage.py --log extraction.log --db _dev.db
    python tools/master_usage.py --log extraction.log --db _dev.db \\
        --until 2026-11-19T20:22:41
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
OUT = BACKEND / "data" / "cache" / "pipeline_stats.json"

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
# "LLM usage: 8 calls, prompt 1200 (cached 300), completion 400 (reasoning 120)"
USAGE = re.compile(
    r"LLM usage: (\d+) calls, prompt (\d+) \(cached (\d+)\), "
    r"completion (\d+) \(reasoning (\d+)\)"
)
# "2026-11-19 18:02:41, INFO ..."
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),")

EVIDENCE = "no verbatim evidence found"
BINDING = "is not stated for this material"
CALCULATION = "is a calculation"
OTHER = "other"

FIELDS = ("calls", "prompt", "cached", "completion", "reasoning")


def read_log(path: str, until: str) -> tuple[dict, dict, str]:
    """Read the extraction log and return what it says about the run."""
    papers: dict[str, dict] = {}
    run_reason: dict[str, int] = {}
    run_field: dict[str, int] = {}
    run_proposed = 0
    current: str | None = None
    last_seen = ""

    usage_total = dict.fromkeys(FIELDS, 0)
    usage_lines = 0
    seg_prev: dict[str, int] | None = None

    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stamp = STAMP.match(line)
            if stamp:
                last_seen = stamp.group(1)
                if until and last_seen > until.replace("T", " "):
                    break
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

    usage_total["usage_lines"] = usage_lines
    usage_total["total_tokens"] = usage_total["prompt"] + usage_total["completion"]
    return papers, usage_total, last_seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        required=True,
        help="the extraction log the run wrote",
    )
    parser.add_argument(
        "--db",
        required=True,
        help="the database the run wrote to; read read-only, for the counts",
    )
    parser.add_argument(
        "--until",
        default="",
        help="the release, as YYYY-MM-DDTHH:MM:SS; log lines after it are "
        "not counted, so the numbers do not move under the paper",
    )
    parser.add_argument(
        "--out",
        default=str(OUT),
        help="where the statistics are written",
    )
    args = parser.parse_args()

    papers, usage_total, last_seen = read_log(args.log, args.until)

    by_reason: collections.Counter[str] = collections.Counter()
    by_field: collections.Counter[str] = collections.Counter()
    for record in papers.values():
        by_reason.update(record["reasons"])
        by_field.update(record["fields"])

    result = {
        "until": args.until or last_seen,
        "papers": len(papers),
        "records_proposed": sum(r["proposed"] for r in papers.values()),
        "records_extracted": sum(r["extracted"] for r in papers.values()),
        "records_stored": sum(r["stored"] for r in papers.values()),
        "skipped_hallucination": sum(
            r["skipped_hallucination"] for r in papers.values()
        ),
        "fields_rejected": sum(by_reason.values()),
        "rejected_by_reason": dict(by_reason),
        "rejected_by_field": sorted(by_field.items(), key=lambda kv: -kv[1]),
        "llm_usage": usage_total,
    }

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    result["corpus"] = {
        "papers": db.execute("select count(*) from papers").fetchone()[0],
        "materials_raw": db.execute("select count(*) from materials").fetchone()[0],
        "mtime": os.path.getmtime(args.db),
    }
    db.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    print(f"  截止 {result['until']}")
    for key in (
        "papers",
        "records_proposed",
        "records_extracted",
        "records_stored",
        "skipped_hallucination",
        "fields_rejected",
    ):
        print(f"  {key:24} {result[key]}")
    print("  rejected_by_reason:", result["rejected_by_reason"])
    print("  llm_usage:", result["llm_usage"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
