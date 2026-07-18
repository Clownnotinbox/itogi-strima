"""Interim live summaries saved to disk."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import db
from config import SUMMARY_DIR, TRANSCRIPT_DIR


STOPWORDS = {
    "это",
    "как",
    "что",
    "вот",
    "там",
    "тут",
    "она",
    "они",
    "оно",
    "меня",
    "тебя",
    "мне",
    "тебе",
    "его",
    "еще",
    "ещё",
    "уже",
    "для",
    "или",
    "если",
    "просто",
    "типа",
    "блин",
    "блядь",
    "нахуй",
    "когда",
    "почему",
    "потому",
    "очень",
    "только",
    "вообще",
}


def _tokens(text: str) -> list[str]:
    return [
        t
        for t in re.findall(r"[а-яёa-z0-9]{4,}", text.lower())
        if t not in STOPWORDS and not t.isdigit()
    ]


def _read_transcripts(stream_id: int) -> tuple[str, list[str]]:
    paths = sorted(TRANSCRIPT_DIR.glob(f"{stream_id}.live.*.txt"))
    if not paths:
        paths = sorted(TRANSCRIPT_DIR.glob(f"{stream_id}*.txt"))
    chunks = []
    for path in paths:
        try:
            text = path.read_text("utf-8").strip()
        except Exception:
            continue
        if text:
            chunks.append(text)
    return "\n".join(chunks), [p.name for p in paths]


def existing_summary_count(stream_id: int) -> int:
    return len(list(SUMMARY_DIR.glob(f"{stream_id}.summary.*.json")))


def write_interim_summary(stream_id: int, elapsed_seconds: int | None = None) -> dict:
    stream = db.get_stream(stream_id)
    if not stream:
        raise ValueError(f"stream {stream_id} not found")

    text, transcript_files = _read_transcripts(stream_id)
    words = Counter(_tokens(text))
    topics = [w for w, _count in words.most_common(12)]
    quotes = stream.get("quotes") or []
    n = existing_summary_count(stream_id) + 1
    elapsed = elapsed_seconds if elapsed_seconds is not None else None

    payload = {
        "stream_id": stream_id,
        "summary_index": n,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "title": stream.get("title"),
        "url": stream.get("url"),
        "status": stream.get("status"),
        "elapsed_seconds": elapsed,
        "transcript_files": transcript_files,
        "transcript_chars": len(text),
        "quote_count": len(quotes),
        "topics": topics,
        "quotes": [
            {
                "rank": q.get("rank"),
                "text": q.get("text"),
                "timecode": q.get("t_start"),
                "score": q.get("score"),
            }
            for q in quotes[:10]
        ],
    }

    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    base = SUMMARY_DIR / f"{stream_id}.summary.{n:04d}"
    Path(f"{base}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
    )

    elapsed_label = "неизвестно"
    if elapsed is not None:
        h, rem = divmod(int(elapsed), 3600)
        m = rem // 60
        elapsed_label = f"{h}ч {m:02d}м"

    lines = [
        f"# Промежуточный итог #{n}",
        "",
        f"Стрим: {stream.get('title') or stream.get('url')}",
        f"Время обработки: {elapsed_label}",
        f"Цитат сохранено: {len(quotes)}",
        f"Расшифровок: {len(transcript_files)} файлов",
        "",
        "## Темы",
        ", ".join(topics) if topics else "Пока мало текста для тем.",
        "",
        "## Топ цитат",
    ]
    if quotes:
        for q in quotes[:10]:
            t = q.get("t_start")
            tc = "--:--" if t is None else f"{int(t)//60:02d}:{int(t)%60:02d}"
            lines.append(f"- [{tc}] {q.get('text')} ({round(q.get('score') or 0)})")
    else:
        lines.append("Пока цитат нет.")
    lines.append("")
    lines.append("## Файлы расшифровки")
    lines.extend(f"- {name}" for name in transcript_files[-12:])
    Path(f"{base}.txt").write_text("\n".join(lines), "utf-8")
    return payload
