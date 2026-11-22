"""Audit the extracted records with a second, independent model.

The database was built with MiniMax-M3.1-Flash.  Auditing it with the
same model would only show the model's opinions back to itself, so the
audit runs on a model of a different family, reached over its own
endpoint, so the two never see each other's work.

For every sampled record the auditor sees what a reader would see: the
formula, the value the database stored, and the sentence it came from
with its surroundings.  It answers three questions -- is this value in
the text, is this text about this material, and is the value a
measurement or a calculation.

The endpoint is any OpenAI-compatible server, so the same script audits
with a local model or with a hosted one.  The key is read from the
environment and never written to disk.

Usage::

    DEEPSEEK_API_KEY=... python tools/audit_with_model.py \\
        --model deepseek-flash --base-url https://api.deepseek.com \\
        --out data/cache/audit_deepseek.jsonl
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PAPERS = BACKEND / "data" / "cache" / "papers"
CACHE = BACKEND / "data" / "cache"

# The two judges and the verdicts they have already written.  A run that
# writes anywhere else cannot resume, and re-judging a record costs
# money and tells us nothing new, so the paths follow the model.
JUDGE_FILES = {
    "flash": "audit_deepseek_full.jsonl",
    "pro": "audit_deepseek_pro_full.jsonl",
}
SAMPLE = CACHE / "audit_sample_500.jsonl"


def output_for(model: str) -> Path:
    """The verdict file a judge writes to and resumes from."""
    name = "pro" if "pro" in model.lower() else "flash"
    return CACHE / JUDGE_FILES[name]

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

# The judge that reads the whole paper.  A window of text around the
# value is not enough to tell whether the material is named elsewhere in
# the paper, and a judge that cannot tell rejects records that are
# right; the paper has to be there for the answer to mean anything.
FULL_PROMPT = """You are checking one record from a superconducting-materials database.

The database holds a material and a transition temperature for it, and the
value was taken from the paper below. Read the paper and answer three
questions about the record:

1. "value"    -- does the paper state this value, in kelvin, for this
                 material?
2. "material" -- is the material this record names the material the value
                 belongs to?
3. "kind"     -- is the value a measured or observed transition temperature,
                 or a computed or predicted one?

Reply with one JSON object and nothing else:
{{"value": "yes|no|unclear", "material": "yes|no|unclear", "kind": "measurement|calculation|unclear"}}

Material: {composition}
Stored value: {tc} {unit}

--- the paper ---
{paper}
"""

JSON_BLOCK = re.compile(r"\{.*\}", re.S)
DECODER = json.JSONDecoder()


def first_object(text: str) -> dict | None:
    """The first complete JSON object in the text.

    The model sometimes answers with the verdict and then repeats it,
    and a greedy match over the whole answer would then hold two
    objects, which is not JSON.  ``raw_decode`` stops at the end of the
    first one.
    """
    for start in (match.start() for match in re.finditer(r"\{", text)):
        try:
            value, _ = DECODER.raw_decode(text[start:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None



class Judge:
    """One auditor, held open for the length of the run."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        effort: str = "",
        max_tokens: int = 8192,
        timeout: int = 300,
    ) -> None:
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key
        self.effort = effort
        self.max_tokens = max_tokens
        self.timeout = timeout

    def ask(self, prompt: str, retries: int = 3) -> dict | None:
        """One prompt, one JSON object back.

        ``None`` means the model did not answer with something that
        could be read; the caller decides what to do about it.
        """
        body: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
        }
        if self.effort:
            body["reasoning_effort"] = self.effort
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        last = ""
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                message = payload["choices"][0]["message"]
                content = message.get("content") or ""
                verdict = first_object(content)
                if verdict is None:
                    # The model sometimes spends the whole answer on
                    # reasoning; the verdict may be in there.
                    reasoning = message.get("reasoning_content") or ""
                    verdict = first_object(reasoning)
                if verdict is None:
                    last = content[:200]
                    time.sleep(2 * (attempt + 1))
                    continue
                return verdict
            except (
                urllib.error.URLError,
                TimeoutError,
                ValueError,
                KeyError,
                OSError,
            ) as exc:
                # The name is in the message too: some of these
                # exceptions carry no text of their own.
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(3 * (attempt + 1))
        self.last_error = last
        return None

    def judge(self, record: dict, retries: int = 3, full_text: bool = False) -> dict:
        """One record's verdict."""
        if full_text:
            name = Path(record.get("markdown_path") or "").name
            paper_path = PAPERS / name if name else None
            if paper_path is None or not paper_path.is_file():
                # Without the paper the judge falls back to the window,
                # and the record is marked as judged that way.
                full_text = False
            else:
                paper = paper_path.read_text(encoding="utf-8")
                prompt = FULL_PROMPT.format(
                    composition=record["composition"],
                    tc=record["tc"],
                    unit=record["tc_unit"] or "K",
                    paper=paper,
                )
        if not full_text:
            prompt = PROMPT.format(
                composition=record["composition"],
                tc=record["tc"],
                unit=record["tc_unit"] or "K",
                context=record["context"]
                or record["evidence"]
                or "(no text available)",
            )
        verdict = self.ask(prompt, retries=retries)
        if verdict is None:
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
                "model": self.model,
                "error": getattr(self, "last_error", "")[:200],
            }
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
            "model": self.model,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        default=str(SAMPLE),
        help="the records to judge; the default is the sample the paper quotes",
    )
    parser.add_argument(
        "--out",
        default="",
        help="where the verdicts are written and resumed from; the default "
        "follows the judge, so that a second run skips what is already judged",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--base-url",
        default="http://localhost:8004",
        help="an OpenAI-compatible server; the /chat/completions path is added",
    )
    parser.add_argument(
        "--api-key-env",
        default="DEEPSEEK_API_KEY",
        help="the environment variable the key is read from",
    )
    parser.add_argument(
        "--effort",
        default="",
        help="reasoning effort for the models that take one (low/high/max)",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--full-text",
        action="store_true",
        help="give the judge the whole paper instead of a window around the value",
    )
    args = parser.parse_args()

    api_key = os.environ.get(args.api_key_env, "").strip()
    # A local server takes no key; a hosted one does.
    if not api_key and args.base_url.startswith("https"):
        print(f"!! 没有密钥：请设置环境变量 {args.api_key_env}")
        return 1

    records = [
        json.loads(line)
        for line in Path(args.sample).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    out = Path(args.out) if args.out else output_for(args.model)
    done: set[str] = set()
    if out.exists():
        kept: list[str] = []
        for line in out.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            # A row that failed is not a verdict: it is tried again.
            if row.get("value") == "error":
                continue
            done.add(row["id"])
            kept.append(line)
        # The file is rewritten only when a failed row is actually in it:
        # a run that has nothing to judge must leave the verdicts alone.
        if len(kept) != len(out.read_text(encoding="utf-8").splitlines()):
            out.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    todo = [record for record in records if record["id"] not in done]
    print(
        f"样本 {len(records)} 条，已判 {len(records) - len(todo)} 条，待判 {len(todo)} 条"
    )

    judge = Judge(
        base_url=args.base_url,
        model=args.model,
        api_key=api_key,
        effort=args.effort,
    )
    lock = threading.Lock()
    count = [0]

    def work(record: dict) -> dict:
        verdict = judge.judge(record, retries=args.retries, full_text=args.full_text)
        with lock:
            count[0] += 1
            if count[0] % 10 == 0:
                print(f"  {count[0]}/{len(todo)}", flush=True)
        return verdict

    # The verdicts are written as they come: a run over the whole
    # corpus takes a while, and a machine that is interrupted should
    # not throw away what it has already read.
    verdicts: list[dict] = []
    with out.open("a", encoding="utf-8") as handle:

        def work_and_write(record: dict) -> dict:
            try:
                verdict = work(record)
            except Exception as error:  # a bad record must not stop the run
                verdict = {
                    "id": record["id"],
                    "composition": record.get("composition"),
                    "value": "error",
                    "material": "error",
                    "kind": "error",
                    "model": args.model,
                    "error": str(error)[:200],
                }
            with lock:
                handle.write(json.dumps(verdict, ensure_ascii=False) + "\n")
                handle.flush()
                verdicts.append(verdict)
            return verdict

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work_and_write, todo))

    # The summary is over the whole sample, not over this run's slice:
    # a run that resumes has judged nothing this time and still has
    # something to say.
    wanted = {record["id"] for record in records}
    judged = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["id"] in wanted
        and json.loads(line).get("value") != "error"
    ]
    print(f"\n判定汇总（样本 {len(judged)}/{len(records)} 条）:")
    for key in ("value", "material", "kind"):
        print(" ", key, dict(collections.Counter(v[key] for v in judged)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
