"""SQLite database layer for mood tracker.

Supports both plaintext sqlite3 (locked / legacy) and sqlcipher3 (unlocked / encrypted).
When the vault is active, database functions use the vault's open SQLCipher connection
instead of opening their own sqlite3 connections.
"""
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mood.db"

# Vault shim: if the vault installs a connection factory, all DB operations
# use it instead of opening sqlite3 directly.
# This lets the vault inject a sqlcipher3 connection while keeping
# database.py logic unchanged.
_vault_connection_factory: Optional[callable] = None


class VaultLockedError(Exception):
    """Raised when database access is attempted while the vault is locked."""
    pass


def install_vault_connection_factory(factory: Optional[callable]) -> None:
    """Install a vault-provided connection factory.

    factory() -> an open DB connection (must have row_factory=sqlite3.Row set).
    Once installed, all get_connection() calls go through the factory.
    To uninstall: call install_vault_connection_factory(None).
    """
    global _vault_connection_factory
    _vault_connection_factory = factory


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
    """Yield an open DB connection.

    If the vault has installed a connection factory (i.e. the DB is encrypted
    and the vault is unlocked), use that connection instead of opening sqlite3.
    Raises VaultLockedError if the vault is set up but not unlocked.
    """
    if _vault_connection_factory is not None:
        conn = _vault_connection_factory()
        try:
            yield conn
        finally:
            pass  # vault owns the connection lifecycle
    else:
        # Check if vault is set up but locked — if so, raise VaultLockedError
        # instead of trying to open the encrypted DB with sqlite3.
        try:
            from app import vault as _v
            if _v.is_vault_setup() and not _v._vault.is_unlocked():
                raise VaultLockedError("Vault is locked. Unlock with /vault-unlock first.")
        except ImportError:
            pass  # vault not available
        except Exception:
            pass
        conn = _open_db()
        try:
            yield conn
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Schema init / migrations (all additive)
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Initialize or verify the database schema.

    For plaintext (unencrypted) databases, runs all additive migrations.
    For encrypted databases, the vault must be unlocked first — this function
    detects an encrypted DB by probing with sqlite3 (which can't open SQLCipher)
    and returns early. The vault handles the encrypted DB.
    """
    if _vault_connection_factory is not None:
        return  # encrypted DB: vault handles schema

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Detect if the DB file is a SQLCipher encrypted database.
    # sqlite3.connect() raises DatabaseError on encrypted files.
    try:
        _probe_conn = sqlite3.connect(str(DB_PATH), timeout=1.0)
        _probe_conn.execute("SELECT 1").fetchone()
        _probe_conn.close()
    except sqlite3.DatabaseError:
        # Not a valid sqlite3 file — likely encrypted. Skip migrations.
        return

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
        # Phase 2: async reflection status column
        if "reflection_status" not in columns:
            conn.execute("ALTER TABLE entries ADD COLUMN reflection_status TEXT DEFAULT 'ready'")
            # Backfill existing rows (legacy entries already have kept_summary)
            conn.execute("UPDATE entries SET reflection_status = 'ready' WHERE kept_summary IS NOT NULL AND kept_summary != ''")

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

        # Phase A-C: embeddings sidecar table (optional — populated only if provider supports /embeddings)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entry_embeddings (
                entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                embedding BLOB NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (entry_id)
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
    reflection_status: str = "ready",
) -> int:
    """Insert or update a daily entry. Signal values are optional.

    reflection_status: 'ready' (default), 'pending' (async generation in progress),
    or 'error' (generation failed). Treat NULL as 'ready' for legacy rows.
    """
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
            INSERT INTO entries (entry_date, notes, draft, transcription, kept_summary, mode,
                                 energy, sleep_quality, sensory_load, overwhelm,
                                 working_notes, reflection_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_date) DO UPDATE SET
                notes = excluded.notes,
                transcription = COALESCE(excluded.transcription, transcription),
                kept_summary = excluded.kept_summary,
                mode = excluded.mode,
                energy = COALESCE(excluded.energy, energy),
                sleep_quality = COALESCE(excluded.sleep_quality, sleep_quality),
                sensory_load = COALESCE(excluded.sensory_load, sensory_load),
                overwhelm = COALESCE(excluded.overwhelm, overwhelm),
                working_notes = COALESCE(excluded.working_notes, working_notes),
                reflection_status = excluded.reflection_status
            """,
            (str(entry_date), notes, '', transcription, kept_summary, mode,
             energy, sleep_quality, sensory_load, overwhelm, working_notes, reflection_status),
        )
        conn.commit()
        return cursor.lastrowid


def update_reflection_status(
    entry_id: int,
    status: str,
    kept_summary: Optional[str] = None,
    error_note: Optional[str] = None,
) -> None:
    """Update reflection status and optionally the kept_summary or error note.

    Called by background tasks. status: 'ready', 'error'.
    """
    with get_connection() as conn:
        if kept_summary is not None:
            conn.execute(
                "UPDATE entries SET reflection_status = ?, kept_summary = ? WHERE id = ?",
                (status, kept_summary, entry_id),
            )
        else:
            conn.execute(
                "UPDATE entries SET reflection_status = ? WHERE id = ?",
                (status, entry_id),
            )
        conn.commit()


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
                      energy, sleep_quality, sensory_load, overwhelm, working_notes,
                      reflection_status, created_at
               FROM entries ORDER BY entry_date DESC"""
        ).fetchall()
        return [dict(row) for row in rows]


def export_all() -> dict:
    """Export all entries with their tags as a plain dict, for JSON backup."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, entry_date, notes, transcription, draft, kept_summary, mode,
                      energy, sleep_quality, sensory_load, overwhelm, working_notes,
                      reflection_status, created_at
               FROM entries ORDER BY entry_date DESC"""
        ).fetchall()

        entries = [dict(row) for row in rows]

        if entries:
            ids = [e["id"] for e in entries]
            placeholders = ",".join("?" * len(ids))
            tag_rows = conn.execute(
                f"""
                SELECT et.entry_id, t.name, t.category
                FROM entry_tags et
                JOIN tags t ON t.id = et.tag_id
                WHERE et.entry_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
            tags_by_entry: dict[int, list[dict]] = {eid: [] for eid in ids}
            for tr in tag_rows:
                tags_by_entry[tr["entry_id"]].append({"name": tr["name"], "category": tr["category"]})
            for e in entries:
                e["tags"] = tags_by_entry.get(e["id"], [])

            # Embeddings
            import json as _json
            emb_rows = conn.execute(
                f"SELECT entry_id, embedding, model FROM entry_embeddings WHERE entry_id IN ({placeholders})",
                ids,
            ).fetchall()
            for er in emb_rows:
                for e in entries:
                    if e["id"] == er["entry_id"]:
                        e["_embedding"] = _json.loads(er["embedding"])
                        e["_embedding_model"] = er["model"]
                        break

        weekly_checkins = conn.execute(
            "SELECT week_start, spoons, meltdown_count, shutdown_count, notes, created_at FROM weekly_checkins ORDER BY week_start DESC"
        ).fetchall()

        # Export all tags — both linked to entries and orphaned
        all_tags = conn.execute(
            "SELECT id, name, category FROM tags ORDER BY category, name"
        ).fetchall()

        return {
            "entries": entries,
            "tags": [dict(t) for t in all_tags],
            "weekly_checkins": [dict(wc) for wc in weekly_checkins],
        }


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
                      energy, sleep_quality, sensory_load, overwhelm, working_notes,
                      reflection_status, created_at
               FROM entries
               WHERE entry_date >= ? AND entry_date <= ?
               ORDER BY entry_date DESC""",
            (start_date, end_date),
        ).fetchall()
        return [dict(row) for row in rows]


def get_entry_status(entry_id: int) -> dict:
    """Return reflection status and kept_summary for a single entry.

    Returns: { status: 'ready'|'pending'|'error', kept_summary: str|null }
    Treats NULL reflection_status as 'ready' for legacy rows.
    """
    with get_connection() as conn:
        row = conn.execute(
            """SELECT reflection_status, kept_summary FROM entries WHERE id = ?""",
            (entry_id,),
        ).fetchone()
        if not row:
            return {"status": "error", "kept_summary": None}
        status = row["reflection_status"]
        if status is None or status == "":
            status = "ready"
        return {"status": status, "kept_summary": row["kept_summary"]}


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


def get_weekly_checkins_in_range(start_date: str, end_date: str) -> list[dict]:
    """Return weekly check-ins that overlap with the given date range."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM weekly_checkins
            WHERE week_start <= ?
            ORDER BY week_start DESC
            """,
            (end_date,),
        ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Entry embeddings (Phase C — optional semantic search)
# ---------------------------------------------------------------------------
def save_entry_embedding(entry_id: int, embedding: list[float], model: str) -> None:
    """Insert or replace the embedding for an entry."""
    import json as _json
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO entry_embeddings (entry_id, embedding, model, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(entry_id) DO UPDATE SET
                embedding = excluded.embedding,
                model = excluded.model,
                updated_at = CURRENT_TIMESTAMP
            """,
            (entry_id, _json.dumps(embedding), model),
        )
        conn.commit()


def get_entry_embedding(entry_id: int) -> dict | None:
    """Return {embedding, model} for an entry, or None if not stored."""
    import json as _json
    with get_connection() as conn:
        row = conn.execute(
            "SELECT embedding, model FROM entry_embeddings WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        if not row:
            return None
        return {"embedding": _json.loads(row["embedding"]), "model": row["model"]}


def get_all_embeddings() -> dict[int, list[float]]:
    """Return all stored embeddings as {entry_id: embedding}."""
    import json as _json
    with get_connection() as conn:
        rows = conn.execute("SELECT entry_id, embedding FROM entry_embeddings").fetchall()
        return {row["entry_id"]: _json.loads(row["embedding"]) for row in rows}


# ---------------------------------------------------------------------------
# Ask-journal retrieval (Phase C)
# ---------------------------------------------------------------------------

def search_entries_for_qa(
    keywords: list[str],
    date_hints: list[str] | None = None,
    tag_names: list[str] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search entries for Q&A context using keyword + recency ranking.

    Returns entries with their full structured data. Prefer kept_summary
    over raw notes in the calling code. Results are ordered by:
      1. Number of keyword matches (higher first)
      2. Recency as a tiebreaker (newer first)
    """
    with get_connection() as conn:
        # Fetch all entries with their tags and signals (most recent 200 as a cap)
        rows = conn.execute(
            """
            SELECT e.id, e.entry_date, e.notes, e.kept_summary,
                   e.energy, e.sleep_quality, e.sensory_load, e.overwhelm
            FROM entries e
            ORDER BY e.entry_date DESC
            LIMIT 200
            """
        ).fetchall()

        entries = [dict(row) for row in rows]

        # Fetch tags for these entries
        if entries:
            ids = [e["id"] for e in entries]
            placeholders = ",".join("?" * len(ids))
            tag_rows = conn.execute(
                f"""
                SELECT et.entry_id, t.name, t.category
                FROM entry_tags et
                JOIN tags t ON t.id = et.tag_id
                WHERE et.entry_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
            tags_by_entry: dict[int, list[dict]] = {eid: [] for eid in ids}
            for tr in tag_rows:
                tags_by_entry[tr["entry_id"]].append({"name": tr["name"], "category": tr["category"]})
            for e in entries:
                e["_tags"] = tags_by_entry.get(e["id"], [])
        else:
            tags_by_entry = {}

        # Score entries by keyword matches
        scored = []
        for e in entries:
            score = 0
            text = (
                (e.get("kept_summary") or "") + " " +
                (e.get("notes") or "") + " " +
                " ".join(t["name"] for t in e.get("_tags", []))
            ).lower()

            for kw in keywords:
                if kw.lower() in text:
                    score += 1

            # Date hints: exact match boosts
            if date_hints:
                for dh in date_hints:
                    if dh in e.get("entry_date", ""):
                        score += 2

            # Tag name hints
            if tag_names:
                entry_tag_names = [t["name"].lower() for t in e.get("_tags", [])]
                for tn in tag_names:
                    if tn.lower() in entry_tag_names:
                        score += 1

            if score > 0 or not keywords:
                scored.append((score, e.get("entry_date", ""), e))

        # Sort: highest score first, recency as tiebreaker
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [e for _, _, e in scored[:limit]]


# ---------------------------------------------------------------------------
# Correlation analytics (Phase D — deterministic, computed in DB)
# ---------------------------------------------------------------------------

def compute_correlations(days: int = 180) -> dict:
    """Compute tag×signal co-occurrence and lead/lag statistics.

    All numbers are computed directly from the DB — no model inference.
    Returns a structured dict of counts; the LLM narrates them.
    """
    from datetime import date, timedelta

    cutoff = str(date.today() - timedelta(days=days))

    with get_connection() as conn:
        # ---- Tag × signal co-occurrence ----
        # For each tag, count how many entries tagged with it had each signal level
        tag_signal_rows = conn.execute(
            """
            SELECT
                t.name AS tag_name,
                t.category AS tag_category,
                e.energy,
                e.sleep_quality,
                e.sensory_load,
                e.overwhelm
            FROM entries e
            JOIN entry_tags et ON et.entry_id = e.id
            JOIN tags t ON t.id = et.tag_id
            WHERE e.entry_date >= ?
              AND t.category IN ('activities', 'triggers')
              AND (e.energy IS NOT NULL OR e.sleep_quality IS NOT NULL
                   OR e.sensory_load IS NOT NULL OR e.overwhelm IS NOT NULL)
            ORDER BY t.name
            """,
            (cutoff,),
        ).fetchall()

        # Baseline: total entries with at least one signal in the window
        total_with_signals = conn.execute(
            """
            SELECT COUNT(*) FROM entries
            WHERE entry_date >= ?
              AND (energy IS NOT NULL OR sleep_quality IS NOT NULL
                   OR sensory_load IS NOT NULL OR overwhelm IS NOT NULL)
            """,
            (cutoff,),
        ).fetchone()[0]

        # Aggregate: tag → {tag_name, tag_category, total_entries,
        #                   energy_counts: {low, med, high},
        #                   sleep_counts, sensory_counts, overwhelm_counts}
        tag_stats: dict = {}
        for row in tag_signal_rows:
            key = (row["tag_name"], row["tag_category"])
            if key not in tag_stats:
                tag_stats[key] = {
                    "tag_name": row["tag_name"],
                    "tag_category": row["tag_category"],
                    "total": 0,
                    "energy": {"low": 0, "med": 0, "high": 0, "null": 0},
                    "sleep_quality": {"low": 0, "med": 0, "high": 0, "null": 0},
                    "sensory_load": {"low": 0, "med": 0, "high": 0, "null": 0},
                    "overwhelm": {"low": 0, "med": 0, "high": 0, "null": 0},
                }
            s = tag_stats[key]
            s["total"] += 1
            for sig_key in ("energy", "sleep_quality", "sensory_load", "overwhelm"):
                v = row[sig_key]
                bucket = v if v in ("low", "med", "high") else "null"
                s[sig_key][bucket] += 1

        # Baseline signal distribution across all entries
        baseline_signals: dict = {}
        baseline_rows = conn.execute(
            """
            SELECT
                SUM(CASE WHEN energy = 'low' THEN 1 ELSE 0 END) AS e_low,
                SUM(CASE WHEN energy = 'med' THEN 1 ELSE 0 END) AS e_med,
                SUM(CASE WHEN energy = 'high' THEN 1 ELSE 0 END) AS e_high,
                SUM(CASE WHEN sleep_quality = 'low' THEN 1 ELSE 0 END) AS s_low,
                SUM(CASE WHEN sleep_quality = 'med' THEN 1 ELSE 0 END) AS s_med,
                SUM(CASE WHEN sleep_quality = 'high' THEN 1 ELSE 0 END) AS s_high,
                SUM(CASE WHEN sensory_load = 'low' THEN 1 ELSE 0 END) AS x_low,
                SUM(CASE WHEN sensory_load = 'med' THEN 1 ELSE 0 END) AS x_med,
                SUM(CASE WHEN sensory_load = 'high' THEN 1 ELSE 0 END) AS x_high,
                SUM(CASE WHEN overwhelm = 'low' THEN 1 ELSE 0 END) AS o_low,
                SUM(CASE WHEN overwhelm = 'med' THEN 1 ELSE 0 END) AS o_med,
                SUM(CASE WHEN overwhelm = 'high' THEN 1 ELSE 0 END) AS o_high
            FROM entries
            WHERE entry_date >= ?
              AND (energy IS NOT NULL OR sleep_quality IS NOT NULL
                   OR sensory_load IS NOT NULL OR overwhelm IS NOT NULL)
            """,
            (cutoff,),
        ).fetchone()

        baseline = {
            "energy": {
                "low": baseline_rows["e_low"] or 0,
                "med": baseline_rows["e_med"] or 0,
                "high": baseline_rows["e_high"] or 0,
            },
            "sleep_quality": {
                "low": baseline_rows["s_low"] or 0,
                "med": baseline_rows["s_med"] or 0,
                "high": baseline_rows["s_high"] or 0,
            },
            "sensory_load": {
                "low": baseline_rows["x_low"] or 0,
                "med": baseline_rows["x_med"] or 0,
                "high": baseline_rows["x_high"] or 0,
            },
            "overwhelm": {
                "low": baseline_rows["o_low"] or 0,
                "med": baseline_rows["o_med"] or 0,
                "high": baseline_rows["o_high"] or 0,
            },
        }

        # ---- Lead/lag: does poor sleep predict next-day overwhelm? ----
        # For each pair of consecutive days, check if sleep was low and next-day overwhelm was high
        lead_lag_rows = conn.execute(
            """
            SELECT
                a.entry_date AS sleep_date,
                a.sleep_quality AS sleep_val,
                b.entry_date AS overwhelm_date,
                b.overwhelm AS overwhelm_val
            FROM entries a
            JOIN entries b
              ON date(b.entry_date) = date(a.entry_date, '+1 day')
            WHERE a.entry_date >= ?
              AND a.sleep_quality IS NOT NULL
              AND b.overwhelm IS NOT NULL
            ORDER BY a.entry_date
            """,
            (cutoff,),
        ).fetchall()

        lead_lag = []
        for row in lead_lag_rows:
            lead_lag.append({
                "sleep_date": row["sleep_date"],
                "sleep": row["sleep_val"],
                "overwhelm_date": row["overwhelm_date"],
                "overwhelm": row["overwhelm_val"],
            })

        # Aggregate: poor sleep → next-day high overwhelm
        poor_sleep_days = [r for r in lead_lag if r["sleep"] == "low"]
        after_poor_sleep_high_overwhelm = sum(1 for r in poor_sleep_days if r["overwhelm"] == "high")
        poor_sleep_high_overwhelm_pct = (
            round(after_poor_sleep_high_overwhelm / len(poor_sleep_days) * 100)
            if poor_sleep_days else None
        )

        # Also check: high energy days → next-day overwhelm
        high_energy_rows = conn.execute(
            """
            SELECT
                a.entry_date AS energy_date,
                a.energy AS energy_val,
                b.entry_date AS overwhelm_date,
                b.overwhelm AS overwhelm_val
            FROM entries a
            JOIN entries b
              ON date(b.entry_date) = date(a.entry_date, '+1 day')
            WHERE a.entry_date >= ?
              AND a.energy IS NOT NULL
              AND b.overwhelm IS NOT NULL
            ORDER BY a.entry_date
            """,
            (cutoff,),
        ).fetchall()

        high_energy = [r for r in high_energy_rows if r["energy_val"] == "high"]
        after_high_energy_high_overwhelm = sum(1 for r in high_energy if r["overwhelm"] == "high")
        high_energy_high_overwhelm_pct = (
            round(after_high_energy_high_overwhelm / len(high_energy) * 100)
            if high_energy else None
        )

        return {
            "tag_signal_stats": list(tag_stats.values()),
            "baseline": baseline,
            "total_entries_with_signals": total_with_signals,
            "lead_lag": {
                "poor_sleep_preceding_overwhelm": {
                    "poor_sleep_days": len(poor_sleep_days),
                    "followed_by_high_overwhelm": after_poor_sleep_high_overwhelm,
                    "pct": poor_sleep_high_overwhelm_pct,
                },
                "high_energy_preceding_overwhelm": {
                    "high_energy_days": len(high_energy),
                    "followed_by_high_overwhelm": after_high_energy_high_overwhelm,
                    "pct": high_energy_high_overwhelm_pct,
                },
            },
            "date_range": f"last {days} days",
        }


# ---------------------------------------------------------------------------
# Backup / Restore (Phase S1)
# ---------------------------------------------------------------------------

def import_data(
    data: dict,
    *,
    on_conflict: str = "skip",  # "skip" | "overwrite"
) -> dict:
    """Restore entries, tags, and weekly check-ins from an export payload.

    on_conflict:
      "skip"   — leave existing entries/check-ins as-is (default)
      "overwrite" — replace existing entries/check-ins with imported data

    Returns a summary of what was done. Never deletes existing rows in
    "skip" mode. Tags are merged (existing tags are reused, new ones added).
    """
    import json as _json

    entries: list[dict] = data.get("entries", [])
    tags_data: list[dict] = data.get("tags", [])
    weekly_checkins: list[dict] = data.get("weekly_checkins", [])
    entry_embeddings: list[dict] = data.get("entry_embeddings", [])

    stats = {
        "entries_imported": 0,
        "entries_skipped": 0,
        "entries_overwritten": 0,
        "tags_created": 0,
        "tags_reused": 0,
        "checkins_imported": 0,
        "checkins_skipped": 0,
        "checkins_overwritten": 0,
        "embeddings_imported": 0,
        "errors": [],
    }

    with get_connection() as conn:
        # --- Entries ---
        for entry in entries:
            try:
                ed = entry.get("entry_date")
                if not ed:
                    stats["errors"].append(f"Entry missing entry_date, skipped: {entry}")
                    continue

                # Check if entry already exists
                existing = conn.execute(
                    "SELECT id FROM entries WHERE entry_date = ?", (ed,)
                ).fetchone()

                if existing:
                    if on_conflict == "skip":
                        stats["entries_skipped"] += 1
                        continue
                    # overwrite: update existing row
                    conn.execute(
                        """
                        UPDATE entries SET
                            notes = ?,
                            transcription = ?,
                            kept_summary = ?,
                            mode = ?,
                            energy = ?,
                            sleep_quality = ?,
                            sensory_load = ?,
                            overwhelm = ?,
                            working_notes = ?,
                            reflection_status = ?
                        WHERE entry_date = ?
                        """,
                        (
                            entry.get("notes", ""),
                            entry.get("transcription"),
                            entry.get("kept_summary"),
                            entry.get("mode", "quick"),
                            entry.get("energy"),
                            entry.get("sleep_quality"),
                            entry.get("sensory_load"),
                            entry.get("overwhelm"),
                            entry.get("working_notes"),
                            entry.get("reflection_status", "ready"),
                            ed,
                        ),
                    )
                    stats["entries_overwritten"] += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO entries
                            (entry_date, notes, draft, transcription, kept_summary, mode,
                             energy, sleep_quality, sensory_load, overwhelm,
                             working_notes, reflection_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ed,
                            entry.get("notes", ""),
                            entry.get("draft", ""),
                            entry.get("transcription"),
                            entry.get("kept_summary"),
                            entry.get("mode", "quick"),
                            entry.get("energy"),
                            entry.get("sleep_quality"),
                            entry.get("sensory_load"),
                            entry.get("overwhelm"),
                            entry.get("working_notes"),
                            entry.get("reflection_status", "ready"),
                        ),
                    )
                    stats["entries_imported"] += 1

                # --- Tags for this entry ---
                entry_id_for_tags = conn.execute(
                    "SELECT id FROM entries WHERE entry_date = ?", (ed,)
                ).fetchone()[0]

                # Collect tags from the entry
                tag_list = entry.get("tags", [])
                if isinstance(tag_list, list):
                    for tag_def in tag_list:
                        if isinstance(tag_def, dict):
                            tag_name = str(tag_def.get("name", "")).strip()
                            tag_cat = str(tag_def.get("category", "")).strip()
                        elif isinstance(tag_def, str):
                            tag_name = tag_def.strip()
                            tag_cat = ""
                        else:
                            continue
                        if not tag_name:
                            continue
                        # Upsert tag
                        existing_tag = conn.execute(
                            "SELECT id FROM tags WHERE LOWER(name) = LOWER(?) AND LOWER(category) = LOWER(?)",
                            (tag_name, tag_cat),
                        ).fetchone()
                        if existing_tag:
                            tag_id = existing_tag[0]
                            stats["tags_reused"] += 1
                        else:
                            cur = conn.execute(
                                "INSERT INTO tags (name, category) VALUES (?, ?)",
                                (tag_name, tag_cat),
                            )
                            tag_id = cur.lastrowid
                            stats["tags_created"] += 1
                        # Link entry to tag (ignore if already linked)
                        conn.execute(
                            "INSERT OR IGNORE INTO entry_tags (entry_id, tag_id) VALUES (?, ?)",
                            (entry_id_for_tags, tag_id),
                        )

                # --- Embeddings for this entry ---
                emb_data = entry.get("_embedding") or entry.get("embedding")
                emb_model = entry.get("_embedding_model") or entry.get("embedding_model", "")
                if emb_data and isinstance(emb_data, list):
                    conn.execute(
                        """
                        INSERT INTO entry_embeddings (entry_id, embedding, model, updated_at)
                        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(entry_id) DO UPDATE SET
                            embedding = excluded.embedding,
                            model = excluded.model,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (entry_id_for_tags, _json.dumps(emb_data), emb_model),
                    )
                    stats["embeddings_imported"] += 1

            except Exception as exc:
                stats["errors"].append(f"Error processing entry {entry.get('entry_date', '?')}: {exc}")

        # --- Orphaned tags (from top-level "tags" key, not tied to entries) ---
        for tag_def in tags_data:
            try:
                tag_name = str(tag_def.get("name", "")).strip()
                tag_cat = str(tag_def.get("category", "")).strip()
                if not tag_name:
                    continue
                existing_tag = conn.execute(
                    "SELECT id FROM tags WHERE LOWER(name) = LOWER(?) AND LOWER(category) = LOWER(?)",
                    (tag_name, tag_cat),
                ).fetchone()
                if existing_tag:
                    stats["tags_reused"] += 1
                else:
                    conn.execute(
                        "INSERT INTO tags (name, category) VALUES (?, ?)",
                        (tag_name, tag_cat),
                    )
                    stats["tags_created"] += 1
            except Exception as exc:
                stats["errors"].append(f"Error processing tag {tag_def}: {exc}")

        # --- Weekly check-ins ---
        for wc in weekly_checkins:
            try:
                ws = wc.get("week_start")
                if not ws:
                    continue

                existing_wc = conn.execute(
                    "SELECT id FROM weekly_checkins WHERE week_start = ?", (ws,)
                ).fetchone()

                if existing_wc:
                    if on_conflict == "skip":
                        stats["checkins_skipped"] += 1
                        continue
                    conn.execute(
                        """
                        UPDATE weekly_checkins SET
                            spoons = ?, meltdown_count = ?, shutdown_count = ?, notes = ?
                        WHERE week_start = ?
                        """,
                        (
                            wc.get("spoons", 0),
                            wc.get("meltdown_count", 0),
                            wc.get("shutdown_count", 0),
                            wc.get("notes"),
                            ws,
                        ),
                    )
                    stats["checkins_overwritten"] += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO weekly_checkins (week_start, spoons, meltdown_count, shutdown_count, notes)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            ws,
                            wc.get("spoons", 0),
                            wc.get("meltdown_count", 0),
                            wc.get("shutdown_count", 0),
                            wc.get("notes"),
                        ),
                    )
                    stats["checkins_imported"] += 1
            except Exception as exc:
                stats["errors"].append(f"Error processing weekly check-in {ws}: {exc}")

        conn.commit()

    return stats
