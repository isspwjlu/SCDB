"""Standalone docling converter -- runs in its own virtualenv.

The application venv cannot host docling: its base interpreter is an
Anaconda Python, and the torch wheels docling pulls refuse to load there
("Error loading c10.dll"). docling therefore lives in a separate virtualenv
(``.venv-docling``, a plain CPython) and this script is the bridge.

The script is a batch worker. It reads one JSON request per line from
stdin::

    {"pdf": "C:/cache/abc.pdf", "out": "C:/cache/abc.md"}

and writes one JSON response per line to stdout::

    {"pdf": "...", "out": "...", "ok": true, "chars": 32015}

or, on failure::

    {"pdf": "...", "ok": false, "error": "..."}

Batching matters: the layout and table models take several seconds to load,
so one process per paper would spend more time loading models than
converting. The caller pipes a whole batch through a single process.

This module must not import anything from the application package -- it runs
in a virtualenv where that package is not installed.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

# The layout and table models are pulled from Hugging Face. On networks
# where huggingface.co is unreachable, the caller sets HF_ENDPOINT to a
# mirror before starting this process.
def _build_converter():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    # The corpus is born-digital publisher PDFs: OCR would only add time.
    options.do_ocr = False
    options.do_table_structure = True
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


def main() -> int:
    converter = _build_converter()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": f"bad request: {exc}"}), file=out, flush=True)
            continue

        pdf = request.get("pdf")
        target = request.get("out")
        response: dict[str, object] = {"pdf": pdf, "out": target}
        try:
            result = converter.convert(str(pdf))
            markdown = result.document.export_to_markdown()
            if target:
                Path(str(target)).parent.mkdir(parents=True, exist_ok=True)
                Path(str(target)).write_text(markdown, encoding="utf-8")
            response["ok"] = True
            response["chars"] = len(markdown)
            response["tables"] = len(result.document.tables)
        except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop the batch
            response["ok"] = False
            response["error"] = f"{type(exc).__name__}: {exc}"
            response["traceback"] = traceback.format_exc(limit=3)
        print(json.dumps(response), file=out, flush=True)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    sys.exit(main())
