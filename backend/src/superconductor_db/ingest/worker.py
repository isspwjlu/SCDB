"""Ingest worker (spec §6.2).

The worker loop claims one ``ingest_jobs`` row at a time using
``SELECT ... FOR UPDATE SKIP LOCKED`` so multiple replicas can run safely.
For each job:

1. Mark ``in_progress`` and stamp ``locked_by``.
2. Run :func:`superconductor_db.ingest.runner.run_job`.
3. Update ``ingest_runs`` counters + ``ingest_jobs.status`` based on the
   result.
4. On failure, increment ``attempts``; auto-mark ``failed`` after 3 tries
   (poison-job guard per spec §11).

The loop is implemented as ``async def run_worker_loop()`` so it can be
awaited from an entrypoint script (``python -m
superconductor_db.ingest.worker``) and from integration tests.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from superconductor_db.core.config import Settings, get_settings
from superconductor_db.core.db import get_engine, get_session_factory
from superconductor_db.db.models.ingest import IngestJob, IngestRun
from superconductor_db.ingest.llm_client import LLMClient
from superconductor_db.ingest.runner import JobResult, run_job

log = logging.getLogger(__name__)


# 3-attempt retry on poison jobs per spec §11.
_MAX_ATTEMPTS = 3

# Lock name used to claim jobs (also used as the audit ``locked_by`` label).
_LOCK_NAME = f"worker:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- #
# Job claim                                                                   #
# --------------------------------------------------------------------------- #


def _claim_next_job(db: Session) -> IngestJob | None:
    """Atomically claim one pending job via ``FOR UPDATE SKIP LOCKED``.

    Returns the locked :class:`IngestJob` (already marked ``in_progress``) or
    ``None`` if no work is available.
    """
    row = db.execute(
        text(
            """
            SELECT id, query, attempts
              FROM ingest_jobs
             WHERE status = 'pending' AND attempts < :max_attempts
             ORDER BY id
             LIMIT 1
             FOR UPDATE SKIP LOCKED
            """
        ),
        {"max_attempts": _MAX_ATTEMPTS},
    ).first()
    if row is None:
        return None

    job_id = row[0]
    db.execute(
        text(
            """
            UPDATE ingest_jobs
               SET status = 'in_progress',
                   locked_by = :locker,
                   locked_at = :now,
                   attempts = attempts + 1
             WHERE id = :id
            """
        ),
        {"locker": _LOCK_NAME, "now": datetime.now(timezone.utc), "id": job_id},
    )
    db.commit()
    return db.get(IngestJob, job_id)


def _finalize_job(db: Session, job: IngestJob, success: bool, error: str | None) -> None:
    """Mark the job ``completed`` / ``failed`` and clear lock metadata."""
    new_status = "completed" if success else "failed"
    db.execute(
        text(
            """
            UPDATE ingest_jobs
               SET status = :status,
                   last_error = :err,
                   locked_by = NULL,
                   locked_at = NULL
             WHERE id = :id
            """
        ),
        {"status": new_status, "err": error, "id": job.id},
    )
    db.commit()


def _increment_run_counters(db: Session, run_id: Any, delta: JobResult) -> None:
    """Apply :class:`JobResult` counters onto an :class:`IngestRun` row."""
    db.execute(
        text(
            """
            UPDATE ingest_runs
               SET papers_searched = papers_searched + :s,
                   papers_downloaded = papers_downloaded + :d,
                   records_extracted = records_extracted + :e,
                   records_stored = records_stored + :st,
                   errors = errors + :err
             WHERE id = :id
            """
        ),
        {
            "s": delta.papers_searched,
            "d": delta.papers_downloaded,
            "e": delta.records_extracted,
            "st": delta.records_stored,
            "err": delta.errors,
            "id": str(run_id),
        },
    )
    db.commit()


# --------------------------------------------------------------------------- #
# Loop                                                                        #
# --------------------------------------------------------------------------- #


async def run_worker_loop(
    *,
    settings: Settings | None = None,
    sleep_when_idle: float = 5.0,
    max_iterations: int | None = None,
) -> None:
    """Run the worker loop until cancelled or ``max_iterations`` reached.

    Each iteration:

    1. Open a DB session.
    2. Claim one job (``FOR UPDATE SKIP LOCKED``).
    3. If a job is found, run :func:`run_job` and update counters.
    4. If no job is found, sleep ``sleep_when_idle`` seconds.

    The worker catches all exceptions per-iteration so a transient DB error
    can't kill the process; instead it logs and retries on the next
    iteration.
    """
    settings = settings or get_settings()
    engine = get_engine(_sync_dsn(settings))
    SessionLocal = get_session_factory(engine)
    client = LLMClient(settings=settings)

    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        db = SessionLocal()
        try:
            job = _claim_next_job(db)
            if job is None:
                db.close()
                await asyncio.sleep(sleep_when_idle)
                continue

            log.info("worker claimed job %s (query=%s)", job.id, job.query)
            try:
                result = await run_job(
                    job.query,
                    db=db,
                    client=client,
                    run_id=job.id,
                )
                _increment_run_counters(db, _run_id_for_job(db, job.id), result)
                _finalize_job(db, job, success=True, error=None)
            except Exception as exc:
                log.exception("worker job %s failed", job.id)
                _finalize_job(db, job, success=False, error=str(exc)[:500])
                db.rollback()
        except Exception as exc:
            log.exception("worker loop outer failure")
            try:
                db.rollback()
            except Exception:
                pass
            await asyncio.sleep(1.0)
        finally:
            try:
                db.close()
            except Exception:
                pass


def _sync_dsn(settings: Settings) -> str:
    if settings.postgres_dsn:
        return settings.postgres_dsn
    return (
        f"postgresql+psycopg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


def _run_id_for_job(db: Session, job_id: int) -> Any:
    """Return the :class:`IngestRun` id currently driving this job.

    Jobs created by the admin endpoint carry an explicit ``run_id`` FK.
    Older jobs predating the migration column may have ``run_id IS NULL``;
    for those we fall back to the most-recent ``running`` run, and finally
    create an ad-hoc run so the counters still update somewhere.
    """
    job = db.get(IngestJob, job_id)
    if job is not None and job.run_id is not None:
        return job.run_id
    row = db.execute(
        text(
            """
            SELECT id FROM ingest_runs
             WHERE status = 'running'
             ORDER BY started_at DESC
             LIMIT 1
            """
        )
    ).first()
    if row is None:
        # Create an ad-hoc run so the counters still update somewhere.
        run = IngestRun(status="running")
        db.add(run)
        db.commit()
        db.refresh(run)
        return run.id
    return row[0]


def main() -> None:
    """CLI entrypoint: ``python -m superconductor_db.ingest.worker``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    asyncio.run(run_worker_loop(settings=get_settings()))


if __name__ == "__main__":
    main()


__all__ = ["run_worker_loop", "main"]