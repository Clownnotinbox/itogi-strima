"""FastAPI-приложение: приём ссылок, фоновый воркер, витрина и экспорт."""
import html
import json
import threading
import time
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import deployment
import finalizer
import live_monitor
import summaries as summary_store
from config import BASE_DIR, OLLAMA_MODEL, PUBLIC_DIR, TOP_N, WHISPER_MODEL
from ollama_client import ping
from siteutil import (
    build_selfcontained,
    load_channel,
    publish,
    save_channel,
    stamp_url as _stamp_url,
    tc as _tc,
)
from twitch_support import twitch_channel_login, twitch_channel_url

app = FastAPI(title="Стрим-цитаты")

STATIC_DIR = BASE_DIR / "static"

# ------------------------------------------------------------- фоновый воркер
_worker_started = False


def _worker_loop():
    while True:
        sid = db.next_queued()
        if sid is None:
            time.sleep(1.5)
            continue
        row = db.get_stream(sid)
        if not row:
            continue
        url = row["url"]

        def on_stage(status, message, meta=None):
            fields = {"status": status, "stage_msg": message}
            if status == "error":
                fields["error"] = message
            if meta:
                fields["title"] = meta.get("title")
                fields["source"] = meta.get("source")
                fields["duration"] = meta.get("duration")
                if meta.get("webpage_url"):
                    fields["url"] = meta["webpage_url"]
            db.update_stream(sid, **fields)

        try:
            import pipeline

            _meta, quotes = pipeline.process_stream(sid, url, on_stage)
            db.save_quotes(sid, quotes)
        except Exception as e:  # ошибка уже записана в on_stage, но подстрахуемся
            db.update_stream(sid, status="error", error=str(e), stage_msg="сбой")


def _ensure_worker():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        threading.Thread(target=_worker_loop, daemon=True).start()


@app.on_event("startup")
def _startup():
    db.init_db()
    db.reset_stuck()
    try:
        publish()  # чтобы /live/data.json существовал сразу
    except Exception:
        pass
    live_monitor.repair_stale_watches()
    _ensure_worker()
    live_monitor.start()


# --------------------------------------------------------------------- модели API
class SubmitBody(BaseModel):
    text: str = ""
    urls: list[str] | None = None


def _parse_links(body: SubmitBody) -> list[str]:
    raw = list(body.urls or [])
    raw += [line.strip() for line in body.text.splitlines()]
    links = []
    for item in raw:
        item = item.strip()
        if not item:
            continue
        parsed = urlparse(item)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            links.append(item)
    # уникализируем, сохраняя порядок
    seen, out = set(), []
    for l in links:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out


def _autofill_channel_from_links(links: list[str]):
    for link in links:
        login = twitch_channel_login(link)
        if not login:
            continue

        ch = load_channel()
        current_login = twitch_channel_login(ch.get("platform_url") or "")
        same_channel = current_login and current_login.lower() == login.lower()

        updates = {
            "handle": f"@{login}",
            "platform_url": twitch_channel_url(login),
        }
        if not same_channel:
            updates["name"] = login
            updates["tagline"] = ""
        elif not ch.get("name") or ch.get("name") == "СТРИМЕР":
            updates["name"] = login

        save_channel(updates)
        return


# ------------------------------------------------------------------------- роуты
@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    ok, msg = ping()
    return {
        "ollama_ok": ok,
        "ollama_info": msg,
        "ollama_model": OLLAMA_MODEL,
        "whisper_model": WHISPER_MODEL,
        "top_n": TOP_N,
    }


@app.post("/api/jobs")
def submit(body: SubmitBody):
    links = _parse_links(body)
    if not links:
        raise HTTPException(400, "не найдено ни одной корректной ссылки")
    _autofill_channel_from_links(links)
    watched = []
    queue_links = []
    for url in links:
        if twitch_channel_login(url):
            login = live_monitor.register_watch(url)
            if login:
                watched.append(login)
            continue
        queue_links.append(url)

    ids = [db.add_stream(url) for url in queue_links]
    _ensure_worker()
    live_monitor.start()
    return {"added": len(ids), "ids": ids, "watched": watched}


@app.get("/api/streams")
def streams():
    return db.list_streams()


@app.get("/api/live-watches")
def live_watches():
    return {"channels": live_monitor.list_watches()}


@app.post("/api/finalize-active")
def finalize_active():
    finalized = []
    for watch in live_monitor.list_watches():
        sid = watch.get("active_stream_id")
        if not sid:
            continue
        try:
            sid = int(sid)
            live_monitor.request_stop(sid)
            deadline = time.time() + 180
            while time.time() < deadline:
                row = db.get_stream(sid)
                if not row or row.get("status") not in ("downloading", "transcribing", "filtering"):
                    break
                time.sleep(2)
            deployment.publish_and_maybe_push("Finalize stream summary site")
            finalized.append({"stream_id": sid, "status": (db.get_stream(sid) or {}).get("status")})
        except Exception as e:
            finalized.append({"stream_id": sid, "error": str(e)})
    return {"finalized": finalized}


@app.post("/api/streams/{sid}/stop-recording")
def stop_recording(sid: int):
    row = db.get_stream(sid)
    if not row:
        raise HTTPException(404, "нет такого стрима")
    return live_monitor.request_stop(sid)


@app.post("/api/streams/{sid}/convert-now")
def convert_now(sid: int):
    row = db.get_stream(sid)
    if not row:
        raise HTTPException(404, "нет такого стрима")
    try:
        return live_monitor.convert_recording_now(sid)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/summaries/{sid}")
def list_summaries(sid: int):
    files = sorted(summary_store.SUMMARY_DIR.glob(f"{sid}.summary.*.txt"))
    return {
        "stream_id": sid,
        "summaries": [
            {
                "name": path.name,
                "url": f"/api/summaries/{sid}/{path.name}",
                "updated_at": path.stat().st_mtime,
            }
            for path in files
        ],
    }


@app.post("/api/summaries/{sid}")
def make_summary(sid: int):
    return summary_store.write_interim_summary(sid)


@app.get("/api/summaries/{sid}/{name}", response_class=PlainTextResponse)
def get_summary(sid: int, name: str):
    expected_prefix = f"{sid}.summary."
    if not name.startswith(expected_prefix) or not name.endswith(".txt"):
        raise HTTPException(400, "некорректное имя файла")
    path = summary_store.SUMMARY_DIR / name
    if not path.exists():
        raise HTTPException(404, "нет такого итога")
    return PlainTextResponse(path.read_text("utf-8"))


@app.get("/api/streams/{sid}")
def stream(sid: int):
    row = db.get_stream(sid)
    if not row:
        raise HTTPException(404, "нет такого стрима")
    return row


@app.delete("/api/streams/{sid}")
def remove(sid: int):
    db.delete_stream(sid)
    return {"ok": True}


@app.post("/api/streams/{sid}/retry")
def retry(sid: int):
    row = db.get_stream(sid)
    if not row:
        raise HTTPException(404, "нет такого стрима")
    if row.get("source") == "Twitch live":
        login = live_monitor.register_watch(row["url"])
        if not login:
            raise HTTPException(400, "не удалось возобновить наблюдение Twitch")
        db.update_stream(
            sid,
            stage_msg="live: сохранённая часть завершена; продолжаю запись в новой сессии",
        )
        live_monitor.start()
        return {"ok": True, "mode": "live", "watching": login}
    db.update_stream(sid, status="queued", error=None, stage_msg="повторно в очереди")
    _ensure_worker()
    return {"ok": True}


# ------------------------------------------------------- профиль стримера + публикация
class ChannelBody(BaseModel):
    name: str | None = None
    handle: str | None = None
    tagline: str | None = None
    accent: str | None = None
    accent2: str | None = None
    platform_url: str | None = None


@app.get("/api/channel")
def get_channel():
    return load_channel()


@app.post("/api/channel")
def set_channel(body: ChannelBody):
    return save_channel(body.model_dump(exclude_none=True))


@app.post("/api/publish")
def do_publish():
    return deployment.publish_and_maybe_push("Update stream summary site")


@app.get("/api/site.html", response_class=HTMLResponse)
def site_html():
    return HTMLResponse(
        build_selfcontained(),
        headers={"Content-Disposition": 'attachment; filename="stream-quotes.html"'},
    )


# --------------------------------------------------------------------- экспорт
@app.get("/api/export/{sid}.json")
def export_json(sid: int):
    row = db.get_stream(sid)
    if not row:
        raise HTTPException(404, "нет такого стрима")
    payload = {
        "title": row["title"],
        "url": row["url"],
        "source": row["source"],
        "duration": row["duration"],
        "quotes": [
            {
                "text": q["text"],
                "timecode": _tc(q["t_start"]),
                "t_start": q["t_start"],
                "link": _stamp_url(row["url"], q["t_start"]),
                "score": q["score"],
                "emotion": q["emotion"],
                "reason": q["reason"],
                "tags": q["tags"],
            }
            for q in row["quotes"]
        ],
    }
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="stream-{sid}.json"'},
    )


@app.get("/api/export/all.json")
def export_all():
    return db.list_streams()


@app.get("/api/export/{sid}.html", response_class=HTMLResponse)
def export_html(sid: int):
    row = db.get_stream(sid)
    if not row:
        raise HTTPException(404, "нет такого стрима")
    cards = []
    for q in row["quotes"]:
        link = _stamp_url(row["url"], q["t_start"])
        tags = "".join(f"<span class='tag'>{html.escape(t)}</span>" for t in q["tags"])
        cards.append(
            f"""<figure class="q">
  <blockquote>{html.escape(q['text'])}</blockquote>
  <figcaption>
    <a href="{html.escape(link)}" target="_blank">▶ {_tc(q['t_start'])}</a>
    <span class="em">{html.escape(q['emotion'])}</span>{tags}
    <span class="sc">{q['score']:.0f}</span>
  </figcaption>
</figure>"""
        )
    doc = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(row['title'] or 'Цитаты стрима')}</title>
<style>
:root{{color-scheme:light}}
body{{margin:0;background:#f1efeb;color:#252320;font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;padding:48px 20px}}
.wrap{{max-width:760px;margin:0 auto}}
h1{{font:600 30px/1.2 Georgia,serif;color:#252320}}
.src{{color:#6c6761;margin:4px 0 32px;font-size:14px}}
.q{{margin:0 0 16px;padding:22px 24px;background:#faf9f6;border:1px solid #d0cbc3;border-radius:8px}}
blockquote{{margin:0;font:500 21px/1.35 Georgia,serif;color:#252320}}
figcaption{{margin-top:14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px}}
figcaption a{{color:#7c3742;text-decoration:none;font-weight:600}}
.em{{color:#6c6761}}
.tag{{border:1px solid #d0cbc3;border-radius:4px;padding:2px 7px;color:#6c6761}}
.sc{{margin-left:auto;color:#6c6761}}
footer{{margin-top:40px;color:#6c6761;font-size:12px}}
</style></head><body><div class="wrap">
<h1>{html.escape(row['title'] or 'Цитаты стрима')}</h1>
<div class="src">{html.escape(row['source'] or '')} · <a style="color:#7c3742" href="{html.escape(row['url'])}">источник</a></div>
{''.join(cards) or '<p>Пока пусто.</p>'}
<footer>Стрим-цитаты</footer>
</div></body></html>"""
    return HTMLResponse(doc)


PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/live", StaticFiles(directory=PUBLIC_DIR, html=True), name="live")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
