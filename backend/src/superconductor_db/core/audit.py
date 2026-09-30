"""Audit log helper (spec §11).

Provides an async-context-manager ``audit(...)`` that wraps a DB-mutating
endpoint and writes an ``audit_log`` row on success or failure. Designed to
compose with the FastAPI session lifecycle so callers can do::

    @router.patch("/admin/users/{id}/role")
    def update_role(..., db: Session = Depends(get_db)):
        with audit("admin.update_role", db, target_table="users", target_id=str(uid)):
            ...

The context manager handles:

- Successful completion -> insert ``AuditLog`` row with ``details`` dict.
- Exception propagation -> still writes a row with ``status="failure"`` and
  the exception class name in ``details["error"]``; then re-raises.

Implementation note: ``audit_log.id`` is BigSerial autoincrement, so we add
the row, then commit at context exit.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from sqlalchemy.orm import Session

from superconductor_db.db.models.audit import AuditLog

log = logging.getLogger(__name__)


@asynccontextmanager
async def audit(
    action: str,
    db: Session,
    *,
    target_table: str | None = None,
    target_id: str | uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
):
    """Async audit context manager.

    Yields ``None``. On exit:

    - success: writes AuditLog row with ``status="success"``.
    - exception: writes AuditLog row with ``status="failure"`` and re-raises.

    Note: ``db.commit()`` is called on exit to persist the row. Callers should
    not commit again after the block.
    """
    target_id_str: str | None = None
    if target_id is not None:
        target_id_str = str(target_id)
    payload: dict[str, Any] = dict(details or {})
    try:
        yield
    except Exception as exc:
        payload["error"] = exc.__class__.__name__
        payload["message"] = str(exc)
        try:
            _write_row(
                db,
                action=action,
                user_id=user_id,
                target_table=target_table,
                target_id=target_id_str,
                details={"status": "failure", **payload},
            )
            db.commit()
        except Exception:
            log.exception("audit: failed to write failure row")
            db.rollback()
        raise
    else:
        try:
            _write_row(
                db,
                action=action,
                user_id=user_id,
                target_table=target_table,
                target_id=target_id_str,
                details={"status": "success", **payload},
            )
            db.commit()
        except Exception:
            log.exception("audit: failed to write success row")
            db.rollback()


def _write_row(
    db: Session,
    *,
    action: str,
    user_id: uuid.UUID | None,
    target_table: str | None,
    target_id: str | None,
    details: dict[str, Any],
) -> None:
    """Add an AuditLog row. Caller commits."""
    row = AuditLog(
        user_id=user_id,
        action=action,
        target_table=target_table,
        target_id=target_id,
        details=details,
    )
    db.add(row)


# Sync variant for use inside synchronous route handlers that don't await.
# Many routes are sync (FastAPI thread-pool). Same semantics as the async one.
@contextmanager
def audit_sync(
    action: str,
    db: Session,
    *,
    target_table: str | None = None,
    target_id: str | uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
):
    target_id_str: str | None = None
    if target_id is not None:
        target_id_str = str(target_id)
    payload: dict[str, Any] = dict(details or {})
    try:
        yield
    except Exception as exc:
        payload["error"] = exc.__class__.__name__
        payload["message"] = str(exc)
        try:
            _write_row(
                db,
                action=action,
                user_id=user_id,
                target_table=target_table,
                target_id=target_id_str,
                details={"status": "failure", **payload},
            )
            db.commit()
        except Exception:
            log.exception("audit_sync: failed to write failure row")
            db.rollback()
        raise
    else:
        try:
            _write_row(
                db,
                action=action,
                user_id=user_id,
                target_table=target_table,
                target_id=target_id_str,
                details={"status": "success", **payload},
            )
            db.commit()
        except Exception:
            log.exception("audit_sync: failed to write success row")
            db.rollback()
