"""Build the offline sheet the expert audit is filled in on.

The sheet is a single self-contained HTML file: it opens without a
server, it holds the records and their context, and it writes the
verdicts back out as a CSV.  Nothing leaves the machine.

Usage::

    python tools/make_expert_audit.py --n 100
"""

from __future__ import annotations

import argparse
import collections
import html
import json
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
OUT = BACKEND.parent / "paper" / "audit" / "expert_audit.html"

VERDICTS = (
    ("correct", "正确", "值与材料都对"),
    ("wrong_value", "值不对", "值与文中不符"),
    ("wrong_material", "材料不对", "文中说的是别的材料"),
    ("unsupported", "无法判断", "证据不足以判断"),
)

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>SCDB 专家抽检</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font: 15px/1.6 "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
         margin: 0; padding: 32px; background: #faf8f5; color: #14141a; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .lede {{ color: #55555f; font-size: 13px; margin-bottom: 24px; max-width: 70ch; }}
  .bar {{ position: sticky; top: 0; background: #faf8f5; padding: 10px 0 14px;
          border-bottom: 1px solid #d8d3cb; margin-bottom: 18px; }}
  .card {{ background: #fff; border: 1px solid #d8d3cb; border-radius: 4px;
           padding: 16px 18px; margin-bottom: 14px; }}
  .head {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: baseline; }}
  .comp {{ font-size: 17px; font-weight: 700; }}
  .tag {{ font-size: 11px; letter-spacing: .08em; text-transform: uppercase;
          border: 1px solid #d8d3cb; border-radius: 2px; padding: 1px 6px; color: #55555f; }}
  .val {{ font-size: 17px; font-weight: 700; color: #b87333; }}
  .src {{ font-size: 12px; color: #55555f; margin-top: 6px; }}
  .src a {{ color: #b87333; }}
  .ctx {{ margin-top: 10px; padding: 10px 12px; background: #f6f3ee; border-left: 3px solid #b87333;
          font-size: 13px; white-space: pre-wrap; }}
  .ev {{ background: #fff3d6; padding: 0 2px; }}
  .opts {{ margin-top: 12px; display: flex; flex-wrap: wrap; gap: 8px; }}
  .opt {{ border: 1px solid #d8d3cb; border-radius: 3px; padding: 5px 12px; cursor: pointer;
          font-size: 13px; background: #fff; user-select: none; }}
  .opt.on {{ border-color: #b87333; background: #fbeee0; color: #8a5122; font-weight: 600; }}
  .note {{ margin-top: 10px; width: 100%; border: 1px solid #d8d3cb; border-radius: 3px;
           padding: 6px 8px; font: inherit; font-size: 13px; }}
  .done {{ background: #eaf5ea; border-color: #7fae7f; }}
  button {{ font: inherit; font-size: 14px; border: 1px solid #14141a; background: #14141a;
            color: #faf8f5; border-radius: 3px; padding: 9px 18px; cursor: pointer; }}
</style>
</head>
<body>
<h1>SCDB 专家抽检</h1>
<p class="lede">每条记录给出：库里存的材料与 Tc、它来自论文的那一句（高亮）及上下文。
请判断这句话是否真的支持这条记录，然后点一个选项。选项会存在本页面里，点底部「导出 CSV」把结果存成文件交回来。</p>
<div class="bar">
  <span id="progress">0 / {total} 已判</span>
  <button id="export" style="float:right">导出 CSV</button>
  <button id="clear" style="float:right;margin-right:8px;background:#fff;color:#14141a">清空</button>
</div>
<div id="cards"></div>
<script>
const RECORDS = {records};
const VERDICTS = {verdicts};
const KEY = "scdb_expert_audit_v1";
const store = JSON.parse(localStorage.getItem(KEY) || "{{}}");

function draw() {{
  const box = document.getElementById("cards");
  box.innerHTML = "";
  for (const r of RECORDS) {{
    const card = document.createElement("div");
    card.className = "card";
    const saved = store[r.id] || {{}};
    card.innerHTML = `
      <div class="head">
        <span class="comp">${{r.composition}}</span>
        <span class="val">${{r.tc === null ? "—" : r.tc}} ${{r.tc_unit || "K"}}</span>
        <span class="tag">${{r.family}}</span>
        <span class="tag">${{r.band}}</span>
        ${{r.hard_case ? '<span class="tag">难例</span>' : ""}}
      </div>
      <div class="src">${{r.paper_title || ""}}
        ${{r.paper_doi ? '<a href="https://doi.org/' + r.paper_doi + '">' + r.paper_doi + '</a>' : ""}}
        · ${{(r.source_dois || []).length}} 篇来源</div>
      <div class="ctx">${{r.context_marked}}</div>
      <div class="opts">${{VERDICTS.map(([id, label, hint]) =>
        `<span class="opt" data-id="${{id}}" data-rid="${{r.id}}"
           title="${{hint}}">${{label}}</span>`).join("")}}</div>
      <input class="note" data-rid="${{r.id}}" placeholder="备注（可选）"
             value="${{saved.note || ""}}">`;
    box.appendChild(card);
  }}
  for (const el of document.querySelectorAll(".opt")) {{
    el.onclick = () => {{
      const rid = el.dataset.rid;
      const cur = store[rid] || {{}};
      cur.verdict = cur.verdict === el.dataset.id ? undefined : el.dataset.id;
      store[rid] = cur;
      localStorage.setItem(KEY, JSON.stringify(store));
      paint();
    }};
  }}
  for (const el of document.querySelectorAll(".note")) {{
    el.oninput = () => {{
      const rid = el.dataset.rid;
      const cur = store[rid] || {{}};
      cur.note = el.value;
      store[rid] = cur;
      localStorage.setItem(KEY, JSON.stringify(store));
    }};
  }}
  paint();
}}

function paint() {{
  let n = 0;
  for (const el of document.querySelectorAll(".opt")) {{
    const cur = store[el.dataset.rid] || {{}};
    el.classList.toggle("on", cur.verdict === el.dataset.id);
  }}
  for (const card of document.querySelectorAll(".card")) {{
    const id = card.querySelector(".opt").dataset.rid;
    const cur = store[id] || {{}};
    card.classList.toggle("done", Boolean(cur.verdict));
    if (cur.verdict) n += 1;
  }}
  document.getElementById("progress").textContent = n + " / " + RECORDS.length + " 已判";
}}

document.getElementById("export").onclick = () => {{
  const rows = [["id", "composition", "tc", "family", "band", "verdict", "note"]];
  for (const r of RECORDS) {{
    const cur = store[r.id] || {{}};
    rows.push([r.id, r.composition, r.tc, r.family, r.band, cur.verdict || "", cur.note || ""]);
  }}
  const csv = rows.map((row) => row.map((cell) =>
    '"' + String(cell).replace(/"/g, '""') + '"').join(",")).join("\\n");
  const blob = new Blob(["\\ufeff" + csv], {{ type: "text/csv;charset=utf-8" }});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "expert_audit_verdicts.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}};

document.getElementById("clear").onclick = () => {{
  if (confirm("清空所有已填的判定？")) {{
    localStorage.removeItem(KEY);
    store = {{}};
    paint();
  }}
}};

draw();
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument(
        "--sample", default=str(BACKEND / "data" / "cache" / "audit_sample.jsonl")
    )
    args = parser.parse_args()

    records = [
        json.loads(line)
        for line in Path(args.sample).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # The hard cases first, then the rest in the order they were drawn.
    chosen = [r for r in records if r["hard_case"]][: args.n]
    for record in records:
        if len(chosen) >= args.n:
            break
        if record not in chosen:
            chosen.append(record)

    payload = []
    for record in chosen:
        context = record["context"] or record["evidence"]
        marked = html.escape(context)
        evidence = html.escape(record["evidence"])
        if evidence and evidence in marked:
            marked = marked.replace(evidence, f'<span class="ev">{evidence}</span>')
        payload.append(
            {
                "id": record["id"],
                "composition": record["composition"],
                "tc": record["tc"],
                "tc_unit": record["tc_unit"],
                "family": record["family"],
                "band": record["band"],
                "hard_case": record["hard_case"],
                "paper_title": record["paper_title"],
                "paper_doi": record["paper_doi"],
                "source_dois": record["source_dois"],
                "context_marked": marked,
            }
        )

    page = PAGE.format(
        records=json.dumps(payload, ensure_ascii=False),
        verdicts=json.dumps(
            [[i, l, h] for i, l, h in VERDICTS],
            ensure_ascii=False,
        ),
        total=len(payload),
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    print(f"抽检卷 → {OUT}（{len(payload)} 条）")
    print("按家族:", dict(collections.Counter(r["family"] for r in payload)))
    print("难例:", sum(1 for r in payload if r["hard_case"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
