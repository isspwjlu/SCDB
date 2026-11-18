"""Collapse each material's reported Tc values to one defensible number.

A compound reported by several papers carries several values, and the binding
gate cannot catch a measurement that is genuinely the paper's own but is
nevertheless wrong for that material — ``Nb = 4.0 K`` came from a paper that
really does discuss niobium, just not its bulk transition. Those accumulate
over time, so the corpus needs a consolidation pass.

The rule: keep the value the measurements agree on, discard the outliers.
Values within :data:`CLUSTER_TOL` kelvin count as agreeing. When no cluster
dominates — two candidates far apart, as with ``Sr2RuO4`` at 1.42 K and
3.5 K — the tie is broken by how well the row is documented, because the
higher value is not automatically the better one: 3.5 K is Sr2RuO4's
pressure-induced transition while 1.42 K is its ambient one.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from typing import Any, Iterable, NamedTuple

# Two values within this many kelvin are taken as the same measurement across
# papers. Wide enough for sample-to-sample spread in one compound, far below
# any real gap between distinct phases of one material.
CLUSTER_TOL = 1.0

_ALIAS_STRIP_RE = re.compile(r"[\s()\[\]{}_,\-/]")


def canonical_formula(formula: str) -> str:
    """Normalise a formula for grouping: phase prefixes and stacks removed.

    ``2H-NbSe2``, ``NbSe2 (bilayer)`` and ``graphene/NbSe2`` describe the same
    family, and their values should be reconciled against each other.
    """
    return _ALIAS_STRIP_RE.sub("", formula).casefold()


def cluster(values: Iterable[float], tol: float = CLUSTER_TOL) -> list[list[float]]:
    """Group values into ascending runs separated by more than ``tol``."""
    groups: list[list[float]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


def _median(values: list[float]) -> float:
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def documentation_score(row: tuple) -> int:
    """How well a row is documented: evidence sentence, then a pressure series."""
    notes = row[3] if len(row) > 3 else None
    pressure = row[4] if len(row) > 4 else None
    return (1 if notes else 0) + (1 if pressure and pressure != "null" else 0)


class Verdict(NamedTuple):
    keep: list[tuple]
    drop: list[tuple]
    reason: str


def decide(items: list[tuple], tol: float = CLUSTER_TOL) -> Verdict | None:
    """Choose which rows of one material survive, or ``None`` if all agree."""
    values = [i[2] for i in items]
    if len(values) < 2:
        return None
    groups = cluster(values, tol)
    if len(groups) == 1:
        # Every value agrees. They may be distinct pressure runs, so keep them.
        return None

    best = max(groups, key=len)
    if len(best) == 1:
        winner = max(items, key=documentation_score)
        reason = f"no agreement; kept the best-documented row ({winner[2]} K)"
    else:
        target = _median(best)
        winner = min(items, key=lambda i: abs(i[2] - target))
        reason = f"majority {best} -> {winner[2]} K"
    drop = [i for i in items if i[0] != winner[0]]
    return Verdict(keep=[winner], drop=drop, reason=reason)


def consolidate(db_path: str, *, apply: bool = False) -> dict[str, Any]:
    """Report (and optionally apply) the consensus reduction for every material."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "select id, chemical_composition, tc_value, notes, tc_under_pressure "
        "from materials where tc_value is not null"
    ).fetchall()
    grouped: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        grouped[canonical_formula(row[1])].append(row)

    removed = 0
    for _key, items in sorted(grouped.items()):
        verdict = decide(items)
        if verdict is None:
            continue
        print(f"  {items[0][1]:24} {sorted(i[2] for i in items)}")
        print(f"     {verdict.reason}; dropping {[d[2] for d in verdict.drop]}")
        if apply:
            conn.executemany(
                "delete from materials where id=?", [(d[0],) for d in verdict.drop]
            )
            conn.execute(
                "update materials set notes=coalesce(notes,'')||? where id=?",
                (
                    f" | consensus of {sorted(i[2] for i in items)}"
                    f" -> {verdict.keep[0][2]} K",
                    verdict.keep[0][0],
                ),
            )
            removed += len(verdict.drop)
    if apply:
        conn.commit()
        remaining = conn.execute("select count(*) from materials").fetchone()[0]
        conn.close()
        return {"removed": removed, "remaining": remaining}
    conn.close()
    return {"removed": 0, "remaining": len(rows)}
