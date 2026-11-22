"""Capacity-triggered retention — prune only when disk pressure is real.

Policy (founder decision): NEVER prune while usage is below 70% of a store's
cap; at 70% log a warning for visibility; at 80% drop the OLDEST 10% of that
store. No age-based pruning at all — data lives as long as the free-tier disk
allows. Under steady load this gives a sawtooth between ~70% and ~80%, and the
SQLite file's high-water mark is what it is (pruned pages are freed for reuse;
with auto_vacuum=INCREMENTAL set at first creation they are also given back to
the OS).

Stores and their "capacity" metric:
- ai_calls.input_image — bytes (the heavy store: compressed photo per extract)
- user_messages, activity_log — row counts
(The guardrail window table is in-memory and capped separately — see
guardrails.py; it follows the same 80%-trigger / 10%-evict rule.)

Safe to call from the engine tick once a day: prunes are single statements, the
meta watermark prevents repeated runs, and golden-set export should happen
BEFORE images are pruned (they never come back).
"""
import logging
import os

from . import db
from .config import settings

log = logging.getLogger("remed.retention")

WARN_FRACTION = 0.7     # log a warning
TRIGGER_FRACTION = 0.8  # prune
PRUNE_FRACTION = 0.1    # of a store, oldest first


def _cutoff_created_at(conn, table: str, n: int) -> str | None:
    """created_at of the n-th oldest row (zero-indexed) — everything at or
    before it is the oldest ~10% we drop."""
    row = conn.execute(
        f"SELECT created_at FROM {table} ORDER BY created_at LIMIT 1 OFFSET ?", (n,)
    ).fetchone()
    return row["created_at"] if row else None


def _prune_ai_images(conn) -> str | None:
    cap_bytes = settings.ai_image_budget_kb * 1024
    if not cap_bytes:
        return None
    used = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(input_image)),0) AS b FROM ai_calls WHERE input_image IS NOT NULL"
    ).fetchone()["b"]
    if used < WARN_FRACTION * cap_bytes:
        return None
    if used < TRIGGER_FRACTION * cap_bytes:
        log.warning("ai_calls images at %s%% of %sKB budget — monitor, no prune yet",
                    round(100 * used / cap_bytes), settings.ai_image_budget_kb)
        return None
    rows = conn.execute("SELECT COUNT(*) AS c FROM ai_calls WHERE input_image IS NOT NULL").fetchone()["c"]
    n = max(1, int(rows * PRUNE_FRACTION))
    cutoff = _cutoff_created_at(conn, "ai_calls", n - 1)
    if cutoff is None:
        return None
    conn.execute(
        "UPDATE ai_calls SET input_image=NULL"
        " WHERE input_image IS NOT NULL AND created_at <= ?", (cutoff,))
    log.warning("ai_calls images hit %.0f%% of budget (%s bytes) — unlinked oldest %d rows",
                100 * used / cap_bytes, used, n)
    return f"unlinked {n} oldest images"


def _prune_log(conn, table: str) -> str | None:
    cap = settings.logs_max_rows
    if not cap:
        return None
    count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    if count < WARN_FRACTION * cap:
        return None
    if count < TRIGGER_FRACTION * cap:
        log.warning("%s at %.0f%% of %d-row cap — monitor, no prune yet",
                    table, 100 * count / cap, cap)
        return None
    n = max(1, int(count * PRUNE_FRACTION))
    cutoff = _cutoff_created_at(conn, table, n - 1)
    if cutoff is None:
        return None
    conn.execute(f"DELETE FROM {table} WHERE created_at <= ?", (cutoff,))
    log.warning("%s at %.0f%% of %d-row cap — pruned oldest %d rows",
                table, 100 * count / cap, cap, n)
    return f"{table}: pruned {n} oldest rows"


def maintenance(now=None) -> dict:
    """Run all capacity checks; prune where triggered. Called at most once per
    UTC day from the engine tick (watermark in meta). Returns a report dict."""
    report: dict = {}
    with db.tx() as conn:
        report["ai_images"] = _prune_ai_images(conn)
        report["user_messages"] = _prune_log(conn, "user_messages")
        report["activity_log"] = _prune_log(conn, "activity_log")
    # give freed pages back to the OS (no-op unless auto_vacuum=INCREMENTAL)
    try:
        db.get_conn().execute("PRAGMA incremental_vacuum(2048)")
    except Exception:
        pass
    # daily ops visibility: file size + core row counts
    try:
        size = os.path.getsize(settings.db_path) + \
            (os.path.getsize(settings.db_path + "-wal") if os.path.exists(settings.db_path + "-wal") else 0)
        counts = {t: db.query_one(f"SELECT COUNT(*) AS c FROM {t}")["c"]
                  for t in ("users", "prescriptions", "dose_events", "reminder_queue")}
        log.info("db size %.1f MB; %s", size / 1e6, counts)
        report["db_size_mb"] = round(size / 1e6, 1)
    except Exception:
        pass
    return report


def maintenance_if_due(now) -> None:
    """Once per UTC day (meta watermark). Call from the engine tick."""
    day = now.strftime("%Y-%m-%d")
    if db.meta_get("last_maintenance") != day:
        db.meta_set("last_maintenance", day)
        try:
            maintenance(now)
        except Exception:
            log.exception("retention maintenance failed")
