"""SQLite-backed history of scraping runs.

One row per run in `runs`, one row per scraped profile in `profiles`. Lives next
to the app's working directory so each install has its own history.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable

DB_FILENAME = "scraper_history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT,
    search_url    TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    verified_only INTEGER NOT NULL DEFAULT 0,
    total_pages   INTEGER,
    profile_count INTEGER NOT NULL DEFAULT 0,
    csv_filename  TEXT,
    error         TEXT,
    target_role   TEXT
);

CREATE TABLE IF NOT EXISTS profiles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url                 TEXT NOT NULL,
    verified            INTEGER NOT NULL DEFAULT 0,
    scraped_at          TEXT NOT NULL,
    name                TEXT,
    headline            TEXT,
    location            TEXT,
    ai_match            TEXT,
    ai_confidence       INTEGER,
    ai_reason           TEXT,
    ai_seniority        TEXT,
    ai_currently_in_role TEXT,
    ai_red_flags        TEXT,
    ai_signals          TEXT
);

CREATE INDEX IF NOT EXISTS idx_profiles_run ON profiles(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
"""

# Columns that may be missing on databases created by older app versions.
# We add them with ALTER TABLE so existing histories aren't wiped.
_MIGRATIONS = [
    ("runs", "target_role", "TEXT"),
    ("profiles", "name", "TEXT"),
    ("profiles", "headline", "TEXT"),
    ("profiles", "location", "TEXT"),
    ("profiles", "ai_match", "TEXT"),
    ("profiles", "ai_confidence", "INTEGER"),
    ("profiles", "ai_reason", "TEXT"),
    # Option A — multi-field AI analysis:
    ("profiles", "ai_seniority", "TEXT"),
    ("profiles", "ai_currently_in_role", "TEXT"),
    ("profiles", "ai_red_flags", "TEXT"),  # JSON array
    ("profiles", "ai_signals", "TEXT"),    # JSON array
]


def _db_path() -> Path:
    return Path.cwd() / DB_FILENAME


def init_db() -> None:
    with sqlite3.connect(_db_path()) as conn:
        conn.executescript(SCHEMA)
        # Bring older databases up to current schema without losing data.
        for table, column, coltype in _MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            except sqlite3.OperationalError:
                # Column already exists -> ignore.
                pass
        conn.commit()


@contextmanager
def _conn():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- runs ----------

def create_run(label: str | None, search_url: str, verified_only: bool,
               target_role: str | None = None) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO runs (label, search_url, started_at, verified_only, "
            "status, target_role) VALUES (?, ?, ?, ?, 'running', ?)",
            (label or None, search_url, _now(), int(bool(verified_only)),
             (target_role or None)),
        )
        return int(cur.lastrowid)


def update_run_progress(run_id: int, *, total_pages: int | None = None,
                        profile_count: int | None = None) -> None:
    sets, vals = [], []
    if total_pages is not None:
        sets.append("total_pages = ?")
        vals.append(int(total_pages))
    if profile_count is not None:
        sets.append("profile_count = ?")
        vals.append(int(profile_count))
    if not sets:
        return
    vals.append(run_id)
    with _conn() as c:
        c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", vals)


def finish_run(run_id: int, *, status: str, csv_filename: str | None = None,
               error: str | None = None, profile_count: int | None = None) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE runs SET status=?, finished_at=?, csv_filename=?, error=?, "
            "profile_count=COALESCE(?, profile_count) WHERE id=?",
            (status, _now(), csv_filename, error, profile_count, run_id),
        )


def add_profiles(run_id: int, profiles: Iterable[dict]) -> list[int]:
    """Insert profiles. Each dict may contain: url, verified, name, headline,
    location. Returns the inserted row ids (in input order) so AI results can
    be linked back later."""
    now = _now()
    ids: list[int] = []
    with _conn() as c:
        # Sanity check: the run must exist before we link profiles to it. If it
        # doesn't, FK enforcement will fail on every INSERT and we want a clear
        # message instead of "FOREIGN KEY constraint failed".
        row = c.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RuntimeError(
                f"Cannot add profiles: run_id={run_id} does not exist in the runs "
                f"table. The history database may be corrupt. To reset, close the "
                f"app and delete scraper_history.db, then re-run."
            )
        for i, p in enumerate(profiles):
            url = (p.get("url") or "").strip()
            if not url:
                # Skip blank URLs rather than failing the whole batch.
                continue
            try:
                cur = c.execute(
                    "INSERT INTO profiles (run_id, url, verified, scraped_at, "
                    "name, headline, location) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (int(run_id), url, int(bool(p.get("verified"))), now,
                     p.get("name") or None,
                     p.get("headline") or None,
                     p.get("location") or None),
                )
                ids.append(int(cur.lastrowid))
            except sqlite3.IntegrityError as e:
                raise RuntimeError(
                    f"Failed to insert profile #{i} (url={url!r}) for "
                    f"run_id={run_id}: {e}"
                ) from e
    return ids


def update_profile_ai(profile_id: int, result: dict) -> None:
    """Persist a full multi-field AI analysis result for one profile.
    No-ops if the profile row no longer exists (e.g. run deleted mid-scrape)."""
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM profiles WHERE id = ?",
                           (profile_id,)).fetchone()
        if not exists:
            return
        c.execute(
            "UPDATE profiles SET "
            " ai_match=?, ai_confidence=?, ai_reason=?, "
            " ai_seniority=?, ai_currently_in_role=?, "
            " ai_red_flags=?, ai_signals=? "
            "WHERE id=?",
            (
                result.get("match") or "unknown",
                int(result.get("confidence") or 0),
                result.get("reason") or "",
                result.get("seniority") or "unknown",
                result.get("currently_in_role") or "unknown",
                json.dumps(result.get("red_flags") or [], ensure_ascii=False),
                json.dumps(result.get("signals") or [], ensure_ascii=False),
                profile_id,
            ),
        )


# ---------- queries ----------

def list_runs(limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT r.id, r.label, r.search_url, r.started_at, r.finished_at, "
            "r.status, r.verified_only, r.total_pages, r.profile_count, "
            "r.csv_filename, r.target_role, "
            "(SELECT COUNT(*) FROM profiles WHERE run_id=r.id AND ai_match='yes') AS ai_yes, "
            "(SELECT COUNT(*) FROM profiles WHERE run_id=r.id AND ai_match='maybe') AS ai_maybe, "
            "(SELECT COUNT(*) FROM profiles WHERE run_id=r.id AND ai_match='no') AS ai_no "
            "FROM runs r ORDER BY r.started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_run(run_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def get_run_profiles(run_id: int) -> list[dict]:
    """Return this run's profiles ordered yes -> maybe -> error -> no -> unknown,
    with the highest-confidence rows first within each bucket."""
    import json as _json
    with _conn() as c:
        rows = c.execute(
            "SELECT id, url, verified, name, headline, location, "
            "ai_match, ai_confidence, ai_reason, "
            "ai_seniority, ai_currently_in_role, ai_red_flags, ai_signals "
            "FROM profiles WHERE run_id = ? ORDER BY "
            "CASE ai_match "
            "  WHEN 'yes'   THEN 0 "
            "  WHEN 'maybe' THEN 1 "
            "  WHEN 'error' THEN 2 "
            "  WHEN 'no'    THEN 3 "
            "  ELSE 4 END, "
            "ai_confidence DESC, id",
            (run_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # JSON-decode the tag list columns so the UI can render them.
            for k in ("ai_red_flags", "ai_signals"):
                try:
                    d[k] = _json.loads(d.get(k) or "[]")
                except (json.JSONDecodeError, TypeError):
                    d[k] = []
            out.append(d)
        return out


def delete_run(run_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM runs WHERE id = ?", (run_id,))
