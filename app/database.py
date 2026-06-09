"""SQLite database layer for mood tracker."""
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mood.db"

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _open_db() -> sqlite3.Connection:
    """Open a fresh connection with sane defaults."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def get_connection():
    conn = _open_db()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema init / migrations (all additive)
# ---------------------------------------------------------------------------

def init_db() -> None:
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_date DATE NOT NULL UNIQUE,
                notes TEXT NOT NULL,
                draft TEXT NOT NULL,
                kept_summary TEXT,
                mode TEXT NOT NULL DEFAULT 'quick',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
        if "mode" not in columns:
            conn.execute("ALTER TABLE entries ADD COLUMN mode TEXT NOT NULL DEFAULT 'quick'")
        # Phase 2: signal columns
        if "energy" not in columns:
            conn.execute("ALTER TABLE entries ADD COLUMN energy TEXT")
        if "sleep_quality" not in columns:
            conn.execute("ALTER TABLE entries ADD COLUMN sleep_quality TEXT")
        if "sensory_load" not in columns:
            conn.execute("ALTER TABLE entries ADD COLUMN sensory_load TEXT")
        if "overwhelm" not in columns:
            conn.execute("ALTER TABLE entries ADD COLUMN overwhelm TEXT")
        # Phase 0: transcription column (raw dictation, never written to disk)
        if "transcription" not in columns:
            conn.execute("ALTER TABLE entries ADD COLUMN transcription TEXT")
        # Phase 3: working notes column (user annotations added after save)
        if "working_notes" not in columns:
            conn.execute("ALTER TABLE entries ADD COLUMN working_notes TEXT")

        # Phase 2: tags table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL
            )
        """)

        # Phase 2: entry_tags junction table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entry_tags (
                entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (entry_id, tag_id)
            )
        """)

        # Phase 3: weekly check-ins table (spoons, meltdown/shutdown log)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weekly_checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start DATE NOT NULL UNIQUE,
                spoons INTEGER DEFAULT 0,
                meltdown_count INTEGER DEFAULT 0,
                shutdown_count INTEGER DEFAULT 0,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()


# ---------------------------------------------------------------------------
# Entry CRUD
# ---------------------------------------------------------------------------

def save_entry(
    entry_date: date,
    notes: str,
    *,
    transcription: Optional[str] = None,
    kept_summary: Optional[str] = None,
    mode: str = "quick",
    energy: Optional[str] = None,
    sleep_quality: Optional[str] = None,
    sensory_load: Optional[str] = None,
    overwhelm: Optional[str] = None,
    working_notes: Optional[str] = None,
) -> int:
    """Insert or update a daily entry. Signal values are optional."""
    valid_signal = {"low", "med", "high"}
    if energy not in valid_signal:
        energy = None
    if sleep_quality not in valid_signal:
        sleep_quality = None
    if sensory_load not in valid_signal:
        sensory_load = None
    if overwhelm not in valid_signal:
        overwhelm = None

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO entries (entry_date, notes, transcription, kept_summary, mode,
                                 energy, sleep_quality, sensory_load, overwhelm, working_notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_date) DO UPDATE SET
                notes = excluded.notes,
                transcription = COALESCE(excluded.transcription, transcription),
                kept_summary = COALESCE(excluded.kept_summary, kept_summary),
                mode = excluded.mode,
                energy = COALESCE(excluded.energy, energy),
                sleep_quality = COALESCE(excluded.sleep_quality, sleep_quality),
                sensory_load = COALESCE(excluded.sensory_load, sensory_load),
                overwhelm = COALESCE(excluded.overwhelm, overwhelm),
                working_notes = COALESCE(excluded.working_notes, working_notes)
            """,
            (str(entry_date), notes, transcription, kept_summary, mode,
             energy, sleep_quality, sensory_load, overwhelm, working_notes),
        )
        conn.commit()
        return cursor.lastrowid


def update_kept_summary(entry_id: int, kept_summary: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE entries SET kept_summary = ? WHERE id = ?",
            (kept_summary, entry_id),
        )
        conn.commit()


def update_working_notes(entry_id: int, working_notes: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE entries SET working_notes = ? WHERE id = ?",
            (working_notes, entry_id),
        )
        conn.commit()


def update_entry_signals(
    entry_id: int,
    *,
    energy: Optional[str] = ...,
    sleep_quality: Optional[str] = ...,
    sensory_load: Optional[str] = ...,
    overwhelm: Optional[str] = ...,
) -> None:
    """
    Update signal columns for an entry.
    Use None to clear a signal; use ... (sentinel) to leave unchanged.
    """
    valid_signal = {"low", "med", "high", None}
    energy = None if energy not in valid_signal else energy
    sleep_quality = None if sleep_quality not in valid_signal else sleep_quality
    sensory_load = None if sensory_load not in valid_signal else sensory_load
    overwhelm = None if overwhelm not in valid_signal else overwhelm

    signals = {
        "energy": energy,
        "sleep_quality": sleep_quality,
        "sensory_load": sensory_load,
        "overwhelm": overwhelm,
    }
    set_clause = ", ".join(f"{k} = ?" for k, v in signals.items() if v is not ...)
    values = [v for v in signals.values() if v is not ...] + [entry_id]

    if not set_clause:
        return

    with get_connection() as conn:
        conn.execute(f"UPDATE entries SET {set_clause} WHERE id = ?", values)
        conn.commit()


def get_recent_summaries(days: int = 7) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT entry_date, kept_summary
            FROM entries
            WHERE kept_summary IS NOT NULL AND kept_summary != ''
            ORDER BY entry_date DESC
            LIMIT ?
            """,
            (days,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_all_entries() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, entry_date, notes, transcription, draft, kept_summary, mode,
                      energy, sleep_quality, sensory_load, overwhelm, working_notes, created_at
               FROM entries ORDER BY entry_date DESC"""
        ).fetchall()
        return [dict(row) for row in rows]


def export_all() -> list[dict]:
    """Export every entry as a plain dict, for JSON export."""
    return get_all_entries()


def get_180day_summaries(days: int = 180) -> list[dict]:
    """Return kept_summary entries from the last N days, newest first."""
    with get_connection() as conn:
        if days == 0:
            rows = conn.execute(
                """
                SELECT entry_date, kept_summary
                FROM entries
                WHERE kept_summary IS NOT NULL
                  AND kept_summary != ''
                ORDER BY entry_date DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT entry_date, kept_summary
                FROM entries
                WHERE kept_summary IS NOT NULL
                  AND kept_summary != ''
                  AND entry_date >= date('now', ? || ' days')
                ORDER BY entry_date DESC
                """,
                (days,),
            ).fetchall()
        return [dict(row) for row in rows]


def get_entries_in_range(start_date: str, end_date: str) -> list[dict]:
    """Return all entries within a date range, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, entry_date, notes, transcription, draft, kept_summary, mode,
                      energy, sleep_quality, sensory_load, overwhelm, working_notes, created_at
               FROM entries
               WHERE entry_date >= ? AND entry_date <= ?
               ORDER BY entry_date DESC""",
            (start_date, end_date),
        ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def get_or_create_tag(name: str, category: str) -> int:
    """Return tag id, creating it if it doesn't exist. name and category are case-insensitive."""
    name = name.strip()
    category = category.strip()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM tags WHERE LOWER(name) = LOWER(?) AND LOWER(category) = LOWER(?)",
            (name, category),
        ).fetchone()
        if row:
            return row[0]
        cursor = conn.execute(
            "INSERT INTO tags (name, category) VALUES (?, ?)",
            (name, category),
        )
        conn.commit()
        return cursor.lastrowid


def save_entry_tags(entry_id: int, tag_ids: list[int]) -> None:
    """Replace all tags for an entry with the given list."""
    with get_connection() as conn:
        conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
        for tag_id in tag_ids:
            conn.execute(
                "INSERT OR IGNORE INTO entry_tags (entry_id, tag_id) VALUES (?, ?)",
                (entry_id, tag_id),
            )
        conn.commit()


def get_entry_tags(entry_id: int) -> list[dict]:
    """Return all tags for an entry."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT t.id, t.name, t.category
               FROM tags t
               JOIN entry_tags et ON et.tag_id = t.id
               WHERE et.entry_id = ?
               ORDER BY t.category, t.name""",
            (entry_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_all_tags() -> list[dict]:
    """Return all tags grouped by category."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, category FROM tags ORDER BY category, name"
        ).fetchall()
        return [dict(row) for row in rows]


def search_tags(query: str, category: Optional[str] = None) -> list[dict]:
    """Search tags by name prefix (case-insensitive). Optionally filter by category."""
    with get_connection() as conn:
        if category:
            rows = conn.execute(
                """SELECT id, name, category FROM tags
                   WHERE LOWER(name) LIKE LOWER(?) AND LOWER(category) = LOWER(?)
                   ORDER BY category, name LIMIT 20""",
                (query.strip() + "%", category),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, name, category FROM tags
                   WHERE LOWER(name) LIKE LOWER(?)
                   ORDER BY category, name LIMIT 20""",
                (query.strip() + "%",),
            ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Weekly check-ins (spoons, meltdown/shutdown log)
# ---------------------------------------------------------------------------

def get_or_create_weekly_checkin(week_start: str) -> dict:
    """Get or create a weekly checkin record for the given week start date (Monday)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM weekly_checkins WHERE week_start = ?",
            (week_start,),
        ).fetchone()
        if row:
            return dict(row)
        conn.execute(
            "INSERT INTO weekly_checkins (week_start) VALUES (?)",
            (week_start,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM weekly_checkins WHERE week_start = ?",
            (week_start,),
        ).fetchone()
        return dict(row)


def update_weekly_checkin(
    week_start: str,
    spoons: int | None = ...,
    meltdown_count: int | None = ...,
    shutdown_count: int | None = ...,
    notes: str | None = ...,
) -> None:
    """Update fields on a weekly checkin. Use ... (sentinel) to skip, None to clear."""
    updates = []
    values = []
    if spoons is not ...:
        updates.append("spoons = ?"); values.append(spoons)
    if meltdown_count is not ...:
        updates.append("meltdown_count = ?"); values.append(meltdown_count)
    if shutdown_count is not ...:
        updates.append("shutdown_count = ?"); values.append(shutdown_count)
    if notes is not ...:
        updates.append("notes = ?"); values.append(notes)
    if not updates:
        return
    values.append(week_start)
    with get_connection() as conn:
        conn.execute(f"UPDATE weekly_checkins SET {', '.join(updates)} WHERE week_start = ?", values)
        conn.commit()


def get_weekly_checkins(weeks: int = 12) -> list[dict]:
    """Return weekly check-in records for the last N weeks, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM weekly_checkins ORDER BY week_start DESC LIMIT ?",
            (weeks,),
        ).fetchall()
        return [dict(row) for row in rows]
