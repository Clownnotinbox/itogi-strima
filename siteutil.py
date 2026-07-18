"""Идентичность стримера (channel.json) и сборка данных для публичной страницы."""
import json
import time
from collections import defaultdict
from datetime import datetime, timezone

import db
from config import CHANNEL_PATH, PUBLIC_DIR

DEFAULT_CHANNEL = {
    "name": "Итоги стрима",
    "handle": "",
    "tagline": "",
    "accent": "#e8b04b",
    "accent2": "#7bdcff",
    "platform_url": "",
}


def load_channel() -> dict:
    ch = dict(DEFAULT_CHANNEL)
    if CHANNEL_PATH.exists():
        try:
            ch.update(json.loads(CHANNEL_PATH.read_text("utf-8")))
        except Exception:
            pass
    return ch


def save_channel(data: dict) -> dict:
    ch = load_channel()
    for k in DEFAULT_CHANNEL:
        if k in data and data[k] is not None:
            ch[k] = data[k]
    CHANNEL_PATH.write_text(json.dumps(ch, ensure_ascii=False, indent=2), "utf-8")
    return ch


def tc(seconds) -> str:
    if seconds is None:
        return "--:--"
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def stamp_url(base_url: str, seconds) -> str:
    if seconds is None or not base_url:
        return base_url or ""
    s = int(seconds)
    if "youtube.com" in base_url or "youtu.be" in base_url:
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}t={s}s"
    if "twitch.tv" in base_url:
        h, m, sec = s // 3600, (s % 3600) // 60, s % 60
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}t={h}h{m}m{sec}s"
    return base_url


def _stream_day(stream: dict) -> str:
    dt = datetime.fromtimestamp(stream.get("created_at") or stream.get("updated_at") or time.time())
    return dt.date().isoformat()


def _stream_day_label(day: str) -> str:
    try:
        return datetime.fromisoformat(day).strftime("%d.%m.%Y")
    except ValueError:
        return day


def build_payload() -> dict:
    """Собирает то, что уедет на публичную страницу: только готовые фразы одного стримера."""
    ch = load_channel()
    streams = [s for s in db.list_streams() if s["status"] != "error" and s["quotes"]]
    out_streams, phrases = [], []
    for s in streams:
        day = _stream_day(s)
        qs = []
        for q in s["quotes"]:
            item = {
                "text": q["text"],
                "t_start": q["t_start"],
                "timecode": tc(q["t_start"]),
                "link": stamp_url(s["url"], q["t_start"]),
                "score": q["score"],
                "emotion": q["emotion"],
                "reason": q["reason"],
                "tags": q["tags"],
            }
            qs.append(item)
            phrases.append({**item, "stream_id": s["id"], "stream_title": s["title"], "stream_day": day})
        out_streams.append(
            {
                "id": s["id"],
                "title": s["title"],
                "url": s["url"],
                "source": s["source"],
                "duration": s["duration"],
                "day": day,
                "day_label": _stream_day_label(day),
                "quotes": qs,
            }
        )
    phrases.sort(key=lambda x: x["score"] or 0, reverse=True)
    day_map = defaultdict(list)
    day_stream_ids = defaultdict(set)
    for p in phrases:
        day_map[p["stream_day"]].append(p)
        day_stream_ids[p["stream_day"]].add(p["stream_id"])
    days = [
        {
            "day": day,
            "label": _stream_day_label(day),
            "phrases": items,
            "stats": {"streams": len(day_stream_ids[day]), "phrases": len(items)},
        }
        for day, items in sorted(day_map.items(), reverse=True)
    ]
    current = days[0] if days else {"day": None, "label": "", "phrases": [], "stats": {"streams": 0, "phrases": 0}}
    return {
        "channel": ch,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {"streams": len(out_streams), "phrases": len(phrases)},
        "current_day": current["day"],
        "current_day_label": current["label"],
        "current_stats": current["stats"],
        "days": days,
        "streams": out_streams,
        "phrases": current["phrases"],
        "all_phrases": phrases,
    }


def publish() -> dict:
    """Пишет data.json рядом с публичной страницей."""
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_payload()
    (PUBLIC_DIR / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
    )
    return payload


def build_selfcontained() -> str:
    """Одна самодостаточная HTML-страница (css+js+данные внутри) — заливай куда угодно."""
    payload = build_payload()
    html = (PUBLIC_DIR / "index.html").read_text("utf-8")
    css = (PUBLIC_DIR / "live.css").read_text("utf-8")
    js = (PUBLIC_DIR / "live.js").read_text("utf-8")
    data_js = "window.__DATA__ = " + json.dumps(payload, ensure_ascii=False) + ";"
    html = html.replace('<link rel="stylesheet" href="live.css" />', f"<style>{css}</style>")
    html = html.replace(
        '<script src="live.js"></script>',
        f"<script>{data_js}</script>\n<script>{js}</script>",
    )
    return html
