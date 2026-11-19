"""Audit the extracted records with a second, independent model.

The database was built with MiniMax-M3.1-Flash.  Auditing it with the
same model would only show the model's opinions back to itself, so the
audit runs on **qwen3.8-27b** -- a different family, served from the
master -- and the two never see each other's work.

For every sampled record the auditor sees what a reader would see: the
formula, the value the database stored, and the sentence it came from
with its surroundings.  It answers three questions -- is this value in
the text, is this text about this material, and is the value a
measurement or a calculation.

Usage::

    python tools/audit_with_qwen.py --sample data/cache/audit_sample.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ENDPOINT = "http://llm-host.invalid:8004/v1/chat/completions"
MODEL = "qwen3.8-27b"

PROMPT = """You are checking one record from a superconducting-materials database.

The database extracted a value from a sentence of a published paper. You are
given the chemical formula, the extracted value, and the text the value was
taken from (the sentence is marked between >>> and <<<).

Answer three questions about that record:
1. "value"    -- does the text state this value, in kelvin, for this material?
2. "material" -- does the text discuss this material, or a formula that
                 normalises to it?
3. "kind"     -- is the value a measured or observed transition temperature,
                 or a computed or predicted one?

Reply with one JSON object and nothing else:
{{"value": "yes|no|unclear", "material": "yes|no|unclear", "kind": "measurement|calculation|unclear"}}

Formula: {composition}
Stored value: {tc} {unit}
Text:
>>>
{context}
<<<"""

JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def judge(record: dict, retries: int = 3) -> dict:
    """One record's verdict."""
    prompt = PROMPT.format(
        composition=record["composition"],
        tc=record["tc"],
        unit=record["tc_unit"] or "K",
        context=record["context"] or record["evidence"] or "(no text available)",
    )
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 3072,
        "temperature": 0.0,
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    last = ""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = json.loads(response.read().decode("utf-8"))
            message = payload["choices"][0]["message"]
            content = message.get("content") or ""
            match = JSON_BLOCK.search(content)
            if not match:
                # The model sometimes spends the whole answer on
                # reasoning; the verdict may be in there.
                reasoning = message.get("reasoning_content") or ""
                match = JSON_BLOCK.search(reasoning)
            if not match:
                last = content[:200]
                continue
            verdict = json.loads(match.group(0))
            return {
                "id": record["id"],
                "composition": record["composition"],
                "tc": record["tc"],
                "family": record["family"],
                "band": record["band"],
                "hard_case": record["hard_case"],
                "value": str(verdict.get("value", "unclear")).lower(),
                "material": str(verdict.get("material", "unclear")).lower(),
                "kind": str(verdict.get("kind", "unclear")).lower(),
            }
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
            last = str(exc)
            time.sleep(3 * (attempt + 1))
    return {
        "id": record["id"],
        "composition": record["composition"],
        "tc": record["tc"],
        "family": record["family"],
        "band": record["band"],
        "hard_case": record["hard_case"],
        "value": "error",
        "material": "error",
        "kind": "error",
        "error": last[:200],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample", default=str(BACKEND / "data" / "cache" / "audit_sample.jsonl")
    )
    parser.add_argument(
        "--out", default=str(BACKEND / "data" / "cache" / "audit_qwen.jsonl")
    )
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    records = [
        json.loads(line)
        for line in Path(args.sample).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    out = Path(args.out)
    done: set[str] = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["id"])
    todo = [record for record in records if record["id"] not in done]
    print(f"样本 {len(records)} 条，已判 {len(done)} 条，待判 {len(todo)} 条")

    lock = threading.Lock()
    count = [0]

    def work(record: dict) -> dict:
        verdict = judge(record)
        with lock:
            count[0] += 1
            if count[0] % 10 == 0:
                print(f"  {count[0]}/{len(todo)}", flush=True)
        return verdict

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        verdicts = list(pool.map(work, todo))

    with out.open("a", encoding="utf-8") as handle:
        for verdict in verdicts:
            handle.write(json.dumps(verdict, ensure_ascii=False) + "\n")

    import collections

    print("\n判定汇总:")
    for key in ("value", "material", "kind"):
        print(" ", key, dict(collections.Counter(v[key] for v in verdicts)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
