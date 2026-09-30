"""Generate JSON Schema contracts from Pydantic models.

Usage::

    python -m backend.scripts.generate_contract_schemas            # -> contracts/
    python -m backend.scripts.generate_contract_schemas --out /tmp/contracts/

Reads every Pydantic ``BaseModel`` exported from
:mod:`superconductor_db.schemas` and writes one ``.schema.json`` per model
under ``contracts/``. CI runs the same script and ``diff``s the committed
output against fresh output — mismatches fail the build.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import pkgutil
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


SCHEMAS_PACKAGE = "superconductor_db.schemas"


def discover_models() -> list[type]:
    """Walk the schemas package and return every Pydantic ``BaseModel`` subclass."""
    pkg = importlib.import_module(SCHEMAS_PACKAGE)
    models: list[type] = []
    for mod_info in pkgutil.walk_packages(pkg.__path__, prefix=f"{pkg.__name__}."):
        try:
            mod = importlib.import_module(mod_info.name)
        except Exception as exc:  # pragma: no cover
            log.warning("skipping %s: %s", mod_info.name, exc)
            continue
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if not isinstance(obj, type):
                continue
            if obj.__module__ != mod.__name__:
                continue
            if obj.__name__ in {"BaseModel"}:
                continue
            if issubclass(obj, _BaseModel()):
                models.append(obj)
    # Deduplicate by id() (some are re-exported).
    seen: set[int] = set()
    out: list[type] = []
    for m in models:
        if id(m) in seen:
            continue
        seen.add(id(m))
        out.append(m)
    return out


def _BaseModel():
    from pydantic import BaseModel

    return BaseModel


def generate_one(model: type) -> dict[str, Any]:
    """Return a clean JSON Schema dict for ``model``."""
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    return _strip_pydantic_metadata(schema)


def _strip_pydantic_metadata(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop fields like ``$defs``-style identifiers that don't round-trip well."""
    # Recurse.
    if isinstance(schema, dict):
        return {k: _strip_pydantic_metadata(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_pydantic_metadata(v) for v in schema]
    return schema


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backend.scripts.generate_contract_schemas",
        description="Dump Pydantic models to JSON Schema in contracts/.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("contracts"),
        help="Output directory (default: contracts/).",
    )
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    # Make sure we can import from backend/src.
    project_root = Path(__file__).resolve().parents[2]
    src = project_root / "backend" / "src"
    if src.exists() and str(src) not in sys.path:
        sys.path.insert(0, str(src))

    models = discover_models()
    log.info("discovered %d Pydantic models", len(models))
    written = 0
    for m in models:
        try:
            schema = generate_one(m)
        except Exception as exc:
            log.warning("skipping %s: %s", m.__name__, exc)
            continue
        path = args.out / f"{m.__name__}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written += 1
        log.info("wrote %s", path)
    log.info("done: %d schemas written", written)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    sys.exit(main())
