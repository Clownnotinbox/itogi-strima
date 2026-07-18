"""Хранилище на SQLite. Один поток-воркер + запросы из FastAPI — используем WAL и
отдельное соединение на операцию, чего для такой нагрузки более чем достаточно."""
import json
import sqlite3
import time
from contextlib import contextmanager

from config import DB_PATH

STATUSES = ("queued", "downloading", "transcribing", "filtering", "done", "error")


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with _conn() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS streams (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT NOT NULL,
                title       TEXT,
                source      TEXT,
                duration    REAL,
                status      TEXT NOT NULL DEFAULT 'queued',
                stage_msg   TEXT,
                error       TEXT,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quotes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id   INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
                text        TEXT NOT NULL,
                t_start     REAL,
                t_end       REAL,
                score       REAL,
                emotion     TEXT,
                reason      TEXT,
                tags        TEXT,
                rank        INTEGER,
                UNIQUE(stream_id, rank)
            );
            """
        )


def add_stream(url: str, status: str = "queued", stage_msg: str | None = None) -> int:
    now = time.time()
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO streams(url, status, stage_msg, created_at, updated_at) VALUES(?,?,?,?,?)",
            (url, status, stage_msg, now, now),
        )
        return cur.lastrowid


def update_stream(stream_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn() as con:
        con.execute(f"UPDATE streams SET {cols} WHERE id=?", (*fields.values(), stream_id))


def set_status(stream_id: int, status: str, stage_msg: str | None = None):
    update_stream(stream_id, status=status, stage_msg=stage_msg)


def save_quotes(stream_id: int, quotes: list[dict]):
    with _conn() as con:
        con.execute("DELETE FROM quotes WHERE stream_id=?", (stream_id,))
        for rank, q in enumerate(quotes, 1):
            con.execute(
                """INSERT INTO quotes(stream_id, text, t_start, t_end, score, emotion, reason, tags, rank)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    stream_id,
                    q["text"],
                    q.get("t_start"),
                    q.get("t_end"),
                    q.get("score"),
                    q.get("emotion"),
                    q.get("reason"),
                    json.dumps(q.get("tags", []), ensure_ascii=False),
                    rank,
                ),
            )


def next_queued() -> int | None:
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM streams WHERE status='queued' AND (source IS NULL OR source!='Twitch live') ORDER BY created_at LIMIT 1"
        ).fetchone()
        return row["id"] if row else None


def get_stream(stream_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
        if not row:
            return None
        stream = dict(row)
        stream["quotes"] = _quotes_for(con, stream_id)
        return stream


def list_streams() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM streams ORDER BY created_at DESC").fetchall()
        out = []
        for row in rows:
            stream = dict(row)
            stream["quotes"] = _quotes_for(con, stream["id"])
            out.append(stream)
        return out


def delete_stream(stream_id: int):
    with _conn() as con:
        con.execute("DELETE FROM streams WHERE id=?", (stream_id,))


def reset_stuck():
    """При старте: всё, что зависло в процессе, вернуть в очередь."""
    with _conn() as con:
        con.execute(
            "UPDATE streams SET status='queued', stage_msg='возвращён в очередь после перезапуска' "
            "WHERE status IN ('downloading','transcribing','filtering') AND (source IS NULL OR source!='Twitch live')"
        )


def _quotes_for(con, stream_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT * FROM quotes WHERE stream_id=? ORDER BY rank", (stream_id,)
    ).fetchall()
    out = []
    for r in rows:
        q = dict(r)
        q["tags"] = json.loads(q["tags"]) if q["tags"] else []
        out.append(q)
    return out
