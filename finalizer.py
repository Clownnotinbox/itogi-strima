"""Finalize a stream: export quotes and remove transient transcript text."""
from __future__ import annotations

import json
from datetime import datetime

import db
import summaries
from config import QUOTE_EXPORT_DIR, TRANSCRIPT_DIR


def _tc(seconds) -> str:
    if seconds is None:
        return "--:--"
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def export_quotes(stream_id: int) -> dict:
    stream = db.get_stream(stream_id)
    if not stream:
        raise ValueError(f"stream {stream_id} not found")

    quotes = stream.get("quotes") or []
    payload = {
        "stream_id": stream_id,
        "title": stream.get("title"),
        "url": stream.get("url"),
        "source": stream.get("source"),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "quotes": [
            {
                "rank": q.get("rank"),
                "text": q.get("text"),
                "timecode": _tc(q.get("t_start")),
                "t_start": q.get("t_start"),
                "score": q.get("score"),
                "emotion": q.get("emotion"),
                "reason": q.get("reason"),
                "tags": q.get("tags") or [],
            }
            for q in quotes
        ],
    }

    QUOTE_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    base = QUOTE_EXPORT_DIR / f"{stream_id}.quotes"
    (QUOTE_EXPORT_DIR / f"{stream_id}.quotes.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
    )

    lines = [
        stream.get("title") or stream.get("url") or f"stream {stream_id}",
        stream.get("url") or "",
        "",
    ]
    if quotes:
        for q in quotes:
            lines.append(f"[{_tc(q.get('t_start'))}] {q.get('text')} ({round(q.get('score') or 0)})")
    else:
        lines.append("Цитат нет.")
    (QUOTE_EXPORT_DIR / f"{stream_id}.quotes.txt").write_text("\n".join(lines), "utf-8")
    return payload


def forget_transcripts(stream_id: int) -> list[str]:
    removed = []
    for path in list(TRANSCRIPT_DIR.glob(f"{stream_id}*.txt")) + list(
        TRANSCRIPT_DIR.glob(f"{stream_id}*.json")
    ):
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass
    return removed


def finalize_stream(
    stream_id: int,
    final_status: str | None = None,
    stage_msg: str | None = None,
    clear_error: bool = True,
    forget_transcript_text: bool = True,
) -> dict:
    export = export_quotes(stream_id)
    try:
        summary = summaries.write_interim_summary(stream_id)
    except Exception:
        summary = None
    removed = forget_transcripts(stream_id) if forget_transcript_text else []
    fields = {"error": None} if clear_error else {}
    if final_status:
        fields["status"] = final_status
    if stage_msg:
        fields["stage_msg"] = stage_msg
    db.update_stream(stream_id, **fields)
    return {
        "stream_id": stream_id,
        "quotes": len(export.get("quotes") or []),
        "export_json": str(QUOTE_EXPORT_DIR / f"{stream_id}.quotes.json"),
        "export_txt": str(QUOTE_EXPORT_DIR / f"{stream_id}.quotes.txt"),
        "removed_transcripts": removed,
        "summary": summary,
    }
