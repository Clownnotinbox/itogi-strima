"""Local Twitch live watcher.

When a watched channel goes live, the monitor records short audio chunks,
transcribes them, extracts quotes, and keeps the stream row updated.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import db
import deployment
import finalizer
import pipeline
import summaries
import twitch_chat
from config import (
    AUDIO_DIR,
    LIVE_CHUNK_SECONDS,
    LIVE_DYNAMIC_KEEP_THRESHOLD,
    LIVE_FIRST_UPDATE_DELAY_SECONDS,
    LIVE_MAX_SECONDS,
    LIVE_MIN_QUOTES,
    LIVE_POLL_SECONDS,
    LIVE_QUOTE_INTERVAL_SECONDS,
    LIVE_QUOTE_LIMIT,
    LIVE_RECONNECT_DELAY_SECONDS,
    LIVE_RECONNECT_GRACE_SECONDS,
    LIVE_QUOTES_PER_HOUR,
    LIVE_SITE_PUBLISH_SECONDS,
    LIVE_SUMMARY_SECONDS,
    LIVE_USE_LLM,
    TRANSCRIPT_DIR,
    WATCHES_PATH,
)
from twitch_support import twitch_channel_login, twitch_channel_url


_thread: threading.Thread | None = None
_lock = threading.Lock()
_active: set[str] = set()
_stop_requested: set[int] = set()
_recorders: dict[int, subprocess.Popen] = {}


class StopLive(Exception):
    pass


def _load() -> dict:
    if not WATCHES_PATH.exists():
        return {"channels": {}}
    try:
        data = json.loads(WATCHES_PATH.read_text("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("channels"), dict):
            return data
    except Exception:
        pass
    return {"channels": {}}


def _save(data: dict):
    WATCHES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def register_watch(url: str) -> str | None:
    login = twitch_channel_login(url)
    if not login:
        return None
    login_key = login.lower()
    with _lock:
        data = _load()
        channels = data.setdefault("channels", {})
        old = channels.get(login_key, {})
        channels[login_key] = {
            "login": login,
            "url": twitch_channel_url(login),
            "last_live_id": None,
            "last_notice_id": old.get("last_notice_id"),
            "active_stream_id": None,
            "created_at": old.get("created_at") or time.time(),
            "updated_at": time.time(),
        }
        _save(data)
    return login


def list_watches() -> list[dict]:
    data = _load()
    return list(data.get("channels", {}).values())


def _update_watch(login: str, **fields):
    with _lock:
        data = _load()
        row = data.setdefault("channels", {}).setdefault(login.lower(), {"login": login})
        row.update(fields)
        row["updated_at"] = time.time()
        _save(data)


def _watch_for(login: str) -> dict:
    data = _load()
    return data.get("channels", {}).get(login.lower(), {})


def repair_stale_watches():
    """Drop stale live state after DB cleanup or interrupted live processing."""
    with _lock:
        data = _load()
        changed = False
        streams_exist = bool(db.list_streams())
        for row in data.get("channels", {}).values():
            row_changed = False
            sid = row.get("active_stream_id")
            if sid:
                try:
                    stream_exists = db.get_stream(int(sid)) is not None
                except (TypeError, ValueError):
                    stream_exists = False
                if not stream_exists:
                    row["active_stream_id"] = None
                    row_changed = True
                    if not streams_exist and row.get("last_live_id"):
                        row["last_live_id"] = None
            elif row.get("last_live_id") and not streams_exist:
                row["last_live_id"] = None
                row_changed = True
            if row_changed:
                row["updated_at"] = time.time()
                changed = True
        if changed:
            _save(data)


def _existing_live_chunks(stream_id: int) -> int:
    return len(list(TRANSCRIPT_DIR.glob(f"{stream_id}.live.*.txt")))


def _probe_live(login: str) -> dict:
    import yt_dlp

    url = twitch_channel_url(login)
    with yt_dlp.YoutubeDL(pipeline._ytdlp_opts(url, {"skip_download": True})) as ydl:
        info = ydl.extract_info(url, download=False)
    live_started_at = info.get("timestamp")
    try:
        live_started_at = float(live_started_at) if live_started_at else None
    except (TypeError, ValueError):
        live_started_at = None
    live_age = max(0.0, time.time() - live_started_at) if live_started_at else None
    return {
        "id": str(info.get("id") or ""),
        "title": info.get("title") or f"{login} live",
        "is_live": bool(info.get("is_live")),
        "url": info.get("webpage_url") or url,
        "source": info.get("extractor_key") or info.get("extractor") or "Twitch",
        "live_started_at": live_started_at,
        "live_age": live_age,
    }


def _live_media_url(login: str) -> str:
    import yt_dlp

    url = twitch_channel_url(login)
    # Do not let an unavailable Twitch endpoint block the stop button forever.
    # The recorder retries transient failures, so a short socket timeout is safe.
    with yt_dlp.YoutubeDL(
        pipeline._ytdlp_opts(url, {"skip_download": True, "socket_timeout": 10})
    ) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = info.get("formats") or []
    audioish = [f for f in formats if f.get("url") and f.get("acodec") != "none"]
    if not audioish:
        raise RuntimeError("Twitch не отдал аудиопоток live")
    audioish.sort(key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0))
    return audioish[0]["url"]


def _record_chunk(login: str, stream_id: int, chunk_index: int) -> str:
    media_url = _live_media_url(login)
    wav = str(AUDIO_DIR / f"{stream_id}.live.{chunk_index:04d}.wav")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        media_url,
        "-t",
        str(LIVE_CHUNK_SECONDS),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        wav,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with _lock:
        _recorders[stream_id] = proc
    deadline = time.time() + LIVE_CHUNK_SECONDS + 90
    try:
        while proc.poll() is None:
            with _lock:
                should_stop = stream_id in _stop_requested
            if should_stop:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise StopLive()
            if time.time() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, LIVE_CHUNK_SECONDS + 90)
            time.sleep(0.5)
        if proc.returncode != 0:
            with _lock:
                should_stop = stream_id in _stop_requested
            if should_stop:
                raise StopLive()
            raise subprocess.CalledProcessError(proc.returncode, cmd)
    finally:
        with _lock:
            _recorders.pop(stream_id, None)
    return wav


def request_stop(stream_id: int) -> dict:
    with _lock:
        _stop_requested.add(stream_id)
        proc = _recorders.get(stream_id)
    if proc and proc.poll() is None:
        proc.terminate()
    stream = db.get_stream(stream_id)
    if stream and stream.get("status") not in ("downloading", "transcribing", "filtering"):
        result = finalizer.finalize_stream(
            stream_id,
            final_status="done",
            stage_msg="остановлено вручную, цитаты сохранены",
        )
        _clear_active_stream(stream_id)
        return {"stopping": False, "finalized": result}
    if stream:
        db.update_stream(
            stream_id,
            stage_msg="live: останавливаю запись; жду завершения текущего запроса Twitch",
            error=None,
        )
    return {"stopping": True, "stream_id": stream_id}


def _clear_active_stream(stream_id: int):
    with _lock:
        data = _load()
        changed = False
        for row in data.get("channels", {}).values():
            if row.get("active_stream_id") == stream_id:
                row["active_stream_id"] = None
                row["updated_at"] = time.time()
                changed = True
        if changed:
            _save(data)


def _apply_chat_reaction(q: dict, tracker: twitch_chat.ChatReactionTracker | None) -> dict:
    if not tracker:
        return q
    reaction = tracker.score(q.get("t_start"), q.get("t_end"))
    if reaction <= 0:
        return q
    q = dict(q)
    q["score"] = round(min(100.0, (q.get("score") or 0) + reaction * 8), 1)
    tags = list(q.get("tags") or [])
    if reaction >= 0.35 and "реакция чата" not in tags:
        tags.append("реакция чата")
    q["tags"] = tags[:5]
    if reaction >= 0.35:
        reason = q.get("reason") or ""
        if "чат" not in reason.lower():
            q["reason"] = (reason + ", чат реагировал").strip(", ")
    return q


def _dynamic_quote_target(elapsed_seconds: float | None) -> int:
    if not elapsed_seconds:
        return LIVE_MIN_QUOTES
    hours = max(0.25, float(elapsed_seconds) / 3600)
    return max(LIVE_MIN_QUOTES, round(hours * LIVE_QUOTES_PER_HOUR + 1))


def _select_live_quotes(quotes: list[dict], elapsed_seconds: float | None, limit: int | None = None) -> list[dict]:
    if not quotes:
        return []
    quotes.sort(key=lambda x: x.get("score") or 0, reverse=True)
    if limit is not None:
        return quotes[: max(1, limit)]

    target = _dynamic_quote_target(elapsed_seconds)
    hard_cap = min(LIVE_QUOTE_LIMIT, max(target, target + 2))
    good = [q for q in quotes if (q.get("score") or 0) >= LIVE_DYNAMIC_KEEP_THRESHOLD]
    if len(good) >= target:
        return good[:hard_cap]

    keep_count = max(min(LIVE_MIN_QUOTES, len(quotes)), len(good))
    return quotes[: min(hard_cap, keep_count)]


def _rebuild_live_quotes(
    stream_id: int,
    tracker: twitch_chat.ChatReactionTracker | None = None,
    limit: int | None = None,
) -> tuple[list[dict], bool]:
    quotes: list[dict] = []
    seen: set[str] = set()
    by_key: dict[str, dict] = {}
    found_transcript = False
    elapsed_seconds = None
    existing_stream = db.get_stream(stream_id)
    for q in (existing_stream or {}).get("quotes") or []:
        key = " ".join((q.get("text") or "").lower().split())
        if not key:
            continue
        seen.add(key)
        saved = dict(q)
        quotes.append(saved)
        by_key[key] = saved
        for value in (q.get("t_end"), q.get("t_start")):
            if value is not None:
                elapsed_seconds = max(elapsed_seconds or 0, float(value))
                break
    for path in sorted(TRANSCRIPT_DIR.glob(f"{stream_id}.live.*.json")):
        try:
            payload = json.loads(path.read_text("utf-8"))
        except Exception:
            continue
        found_transcript = True
        raw_loudness = payload.get("loudness") or []
        loudness = []
        for point in raw_loudness:
            if isinstance(point, dict):
                loudness.append((point.get("start"), point.get("level", 0)))
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                loudness.append((point[0], point[1]))
        fresh = pipeline.generate_quote_candidates(payload.get("segments") or [], loudness, limit=16)
        for q in fresh:
            key = " ".join(q["text"].lower().split())
            if not key:
                continue
            if key in seen:
                if q.get("context") and key in by_key:
                    by_key[key]["context"] = q["context"]
                continue
            seen.add(key)
            added = _apply_chat_reaction(q, tracker)
            quotes.append(added)
            by_key[key] = added
            for value in (q.get("t_end"), q.get("t_start")):
                if value is not None:
                    elapsed_seconds = max(elapsed_seconds or 0, float(value))
                    break

    try:
        quotes = pipeline.finalize_quote_candidates(
            quotes,
            use_llm=LIVE_USE_LLM,
            limit=None,
        )
    except Exception as e:
        quotes = pipeline.finalize_quote_candidates(quotes, use_llm=False, limit=None)
        stream = db.get_stream(stream_id)
        msg = f"live: LLM-rerank не сработал ({e}); оставил быстрый отбор"
        if stream and stream.get("status") != "done":
            db.update_stream(stream_id, stage_msg=msg)

    stream = db.get_stream(stream_id)
    elapsed_seconds = max(elapsed_seconds or 0, float((stream or existing_stream or {}).get("duration") or 0)) or None
    return _select_live_quotes(quotes, elapsed_seconds, limit), found_transcript


def convert_recording_now(stream_id: int) -> dict:
    stream = db.get_stream(stream_id)
    if not stream:
        raise ValueError(f"stream {stream_id} not found")

    quotes, found_transcript = _rebuild_live_quotes(stream_id)
    if not found_transcript and not (stream.get("quotes") or []):
        export = finalizer.export_quotes(stream_id)
        return {
            "stream_id": stream_id,
            "quotes": len(stream.get("quotes") or []),
            "export_txt": str(finalizer.QUOTE_EXPORT_DIR / f"{stream_id}.quotes.txt"),
            "export_json": str(finalizer.QUOTE_EXPORT_DIR / f"{stream_id}.quotes.json"),
            "export": export,
            "message": "transcripts already removed; no saved candidates to rerank",
        }

    db.save_quotes(stream_id, quotes)
    export = finalizer.export_quotes(stream_id)
    deployment.publish_and_maybe_push("Update stream summary site")
    return {
        "stream_id": stream_id,
        "quotes": len(quotes),
        "export_txt": str(finalizer.QUOTE_EXPORT_DIR / f"{stream_id}.quotes.txt"),
        "export_json": str(finalizer.QUOTE_EXPORT_DIR / f"{stream_id}.quotes.json"),
        "export": export,
        "message": (
            "quotes rebuilt from saved transcripts"
            if found_transcript
            else "saved candidates reranked with the current algorithm"
        ),
    }


def _process_live(login: str, live: dict, stream_id: int | None = None):
    live_id = live["id"] or str(int(time.time()))
    if stream_id is None:
        stream_id = db.add_stream(
            live["url"],
            status="downloading",
            stage_msg="live: стартую непрерывную запись",
        )
        stage_msg = "live: стартую непрерывную запись"
    else:
        stage_msg = "live: продолжаю запись после перезапуска"
    db.update_stream(
        stream_id,
        status="downloading",
        title=live["title"],
        source="Twitch live",
        duration=live.get("live_age"),
        stage_msg=stage_msg,
        error=None,
    )
    _update_watch(login, last_live_id=live_id, active_stream_id=stream_id)

    if _watch_for(login).get("last_notice_id") != live_id:
        ok, msg = twitch_chat.send_notice(login)
        if ok:
            db.update_stream(stream_id, stage_msg="live: написал в чат, слушаю эфир")
            _update_watch(login, last_notice_id=live_id)
        else:
            db.update_stream(stream_id, stage_msg=f"live: слушаю эфир, чат не тронут ({msg})")

    chat_tracker = twitch_chat.ChatReactionTracker(login, live.get("live_age") or 0)
    chat_tracker.start()

    chunks: queue.Queue[dict] = queue.Queue()
    record_done = threading.Event()
    stop_reason = {"kind": "ended", "message": "live завершён"}
    chunk_index = _existing_live_chunks(stream_id)
    started = time.time()
    summary_count = summaries.existing_summary_count(stream_id)
    existing_recorded_seconds = chunk_index * LIVE_CHUNK_SECONDS

    def first_schedule_at(interval_seconds: int) -> float:
        if interval_seconds <= 0:
            return float("inf")
        if LIVE_FIRST_UPDATE_DELAY_SECONDS > 0:
            return existing_recorded_seconds + LIVE_FIRST_UPDATE_DELAY_SECONDS
        return max(1, summary_count + 1) * interval_seconds

    next_quote_at = (
        first_schedule_at(LIVE_QUOTE_INTERVAL_SECONDS)
        if LIVE_QUOTE_INTERVAL_SECONDS > 0
        else float("inf")
    )
    next_summary_at = (
        first_schedule_at(LIVE_SUMMARY_SECONDS)
        if LIVE_SUMMARY_SECONDS > 0
        else float("inf")
    )
    next_publish_at = (
        first_schedule_at(LIVE_SITE_PUBLISH_SECONDS)
        if LIVE_SITE_PUBLISH_SECONDS > 0
        else float("inf")
    )

    def rebuild_and_publish(recorded_seconds: int, message: str):
        quotes, found = _rebuild_live_quotes(stream_id, chat_tracker)
        if found:
            db.save_quotes(stream_id, quotes)
            finalizer.export_quotes(stream_id)
        if LIVE_SUMMARY_SECONDS > 0 and recorded_seconds >= next_summary_at:
            summaries.write_interim_summary(stream_id, elapsed_seconds=recorded_seconds)
        deployment.publish_and_maybe_push(message)
        return len(quotes)

    def recorder():
        nonlocal chunk_index
        offline_since: float | None = None
        try:
            while True:
                with _lock:
                    if stream_id in _stop_requested:
                        stop_reason.update({"kind": "manual", "message": "остановлено вручную"})
                        break
                elapsed_recorded = chunk_index * LIVE_CHUNK_SECONDS + max(0, time.time() - started)
                if LIVE_MAX_SECONDS and elapsed_recorded >= LIVE_MAX_SECONDS:
                    stop_reason.update({"kind": "limit", "message": "live: остановлено по лимиту времени"})
                    break

                probe_error = None
                try:
                    live_now = _probe_live(login)
                except Exception as e:
                    probe_error = str(e)
                    live_now = {"is_live": False}
                # A successful Twitch probe that says offline means the stream has
                # ended.  Do not keep the row in "downloading" for the reconnect
                # grace period: that period is only for an inconclusive probe
                # (network/Twitch error) or a broken media connection below.
                if not live_now.get("is_live") and not probe_error:
                    stop_reason.update(
                        {
                            "kind": "ended",
                            "message": "live завершён",
                        }
                    )
                    break
                if not live_now.get("is_live"):
                    offline_since = offline_since or time.time()
                    offline_for = time.time() - offline_since
                    remaining = LIVE_RECONNECT_GRACE_SECONDS - offline_for
                    if LIVE_RECONNECT_GRACE_SECONDS <= 0 or remaining <= 0:
                        stop_reason.update(
                            {
                                "kind": "ended",
                                "message": "live недоступен после ожидания переподключения",
                            }
                        )
                        break
                    reason = f"ошибка Twitch: {probe_error}" if probe_error else "эфир временно offline"
                    db.update_stream(
                        stream_id,
                        status="downloading",
                        stage_msg=(
                            f"live: {reason}; жду восстановление до "
                            f"{max(1, round(remaining / 60))} мин"
                        ),
                        error=None,
                    )
                    time.sleep(min(LIVE_RECONNECT_DELAY_SECONDS, max(1, remaining)))
                    continue

                next_chunk_index = chunk_index + 1
                fallback_offset = (next_chunk_index - 1) * LIVE_CHUNK_SECONDS
                offset = float(live_now.get("live_age") or fallback_offset)
                db.update_stream(
                    stream_id,
                    status="downloading",
                    stage_msg=f"live: пишу аудио-фрагмент {next_chunk_index}",
                    duration=offset,
                    error=None,
                )
                try:
                    wav = _record_chunk(login, stream_id, next_chunk_index)
                except StopLive:
                    raise
                except Exception as e:
                    offline_since = offline_since or time.time()
                    offline_for = time.time() - offline_since
                    remaining = LIVE_RECONNECT_GRACE_SECONDS - offline_for
                    if LIVE_RECONNECT_GRACE_SECONDS <= 0 or remaining <= 0:
                        stop_reason.update({"kind": "error", "message": str(e)})
                        break
                    db.update_stream(
                        stream_id,
                        status="downloading",
                        stage_msg=(
                            "live: аудиопоток оборвался; переподключаюсь, "
                            f"запас ожидания {max(1, round(remaining / 60))} мин"
                        ),
                        error=None,
                    )
                    time.sleep(min(LIVE_RECONNECT_DELAY_SECONDS, max(1, remaining)))
                    continue
                offline_since = None
                chunk_index = next_chunk_index
                chunks.put(
                    {
                        "index": chunk_index,
                        "offset": offset,
                        "live": live_now,
                        "wav": wav,
                    }
                )
        except StopLive:
            stop_reason.update({"kind": "manual", "message": "остановлено вручную"})
        except Exception as e:
            stop_reason.update({"kind": "error", "message": str(e)})
        finally:
            record_done.set()

    threading.Thread(target=recorder, daemon=True).start()

    processed = _existing_live_chunks(stream_id)
    try:
        while not record_done.is_set() or not chunks.empty():
            try:
                item = chunks.get(timeout=2)
            except queue.Empty:
                continue

            idx = item["index"]
            wav = item["wav"]
            offset = item["offset"]
            live_now = item["live"]
            db.update_stream(
                stream_id,
                status="transcribing",
                stage_msg=f"live: расшифровка фрагмента {idx}; итоги каждые 2 часа",
                error=None,
            )
            loudness = pipeline.compute_loudness(wav)
            segments, words = pipeline.transcribe(wav)
            pipeline.save_transcript(
                stream_id,
                segments,
                words,
                live_now,
                chunk_index=idx,
                offset=offset,
                loudness=loudness,
            )
            pipeline._free_model()
            processed = max(processed, idx)
            recorded_seconds = processed * LIVE_CHUNK_SECONDS
            db.update_stream(stream_id, duration=offset + LIVE_CHUNK_SECONDS, error=None)

            if recorded_seconds >= next_quote_at:
                count = rebuild_and_publish(recorded_seconds, "Update 2-hour stream summary")
                db.update_stream(
                    stream_id,
                    status="downloading",
                    stage_msg=f"live: 2-часовой итог опубликован, цитат: {count}",
                    error=None,
                )
                while LIVE_QUOTE_INTERVAL_SECONDS > 0 and recorded_seconds >= next_quote_at:
                    next_quote_at += LIVE_QUOTE_INTERVAL_SECONDS
                while LIVE_SUMMARY_SECONDS > 0 and recorded_seconds >= next_summary_at:
                    next_summary_at += LIVE_SUMMARY_SECONDS
                while LIVE_SITE_PUBLISH_SECONDS > 0 and recorded_seconds >= next_publish_at:
                    next_publish_at += LIVE_SITE_PUBLISH_SECONDS

            try:
                os.remove(wav)
            except OSError:
                pass

        final_count = rebuild_and_publish(
            processed * LIVE_CHUNK_SECONDS,
            "Finalize stream summary site",
        )
        if stop_reason["kind"] == "error":
            db.update_stream(stream_id, status="error", error=stop_reason["message"], stage_msg="live: сбой")
            finalizer.finalize_stream(
                stream_id,
                stage_msg="live: сбой, найденные цитаты сохранены; повторю запись автоматически",
                clear_error=False,
                forget_transcript_text=False,
            )
        else:
            finalizer.finalize_stream(
                stream_id,
                final_status="done",
                stage_msg=f"{stop_reason['message']}: {final_count} цитат сохранено",
                forget_transcript_text=False,
            )
    finally:
        chat_tracker.stop()

    with _lock:
        _stop_requested.discard(stream_id)
    if stop_reason["kind"] in ("error", "ended"):
        _update_watch(login, active_stream_id=None, last_live_id=None)
    else:
        _update_watch(login, active_stream_id=None)


def _loop():
    while True:
        for watch in list_watches():
            login = watch.get("login")
            if not login:
                continue
            login_key = login.lower()
            if login_key in _active:
                continue
            try:
                live = _probe_live(login)
                if not live.get("is_live"):
                    continue
                live_id = live["id"] or ""
                active_stream_id = watch.get("active_stream_id")
                previous_live_id = watch.get("last_live_id") or ""
                if active_stream_id and previous_live_id and live_id and live_id != previous_live_id:
                    # The channel started a different broadcast while the old
                    # recording was still marked active (for example after the
                    # app was restarted).  Finalize it and start this broadcast
                    # at chunk 1 instead of appending to its transcripts.
                    finalizer.finalize_stream(
                        int(active_stream_id),
                        final_status="done",
                        stage_msg="live: завершён предыдущий эфир; начат новый",
                        forget_transcript_text=False,
                    )
                    _update_watch(login, active_stream_id=None, last_live_id=None)
                    active_stream_id = None
                if live_id and live_id == watch.get("last_live_id") and not active_stream_id:
                    continue

                _active.add(login_key)

                def run_one(login=login, live=live, active_stream_id=active_stream_id):
                    try:
                        _process_live(login, live, active_stream_id)
                    except Exception as e:
                        sid = _watch_for(login).get("active_stream_id") or active_stream_id
                        if sid:
                            db.update_stream(
                                int(sid),
                                status="error",
                                error=str(e),
                                stage_msg="live: сбой, повторю запись автоматически",
                            )
                        _update_watch(login, active_stream_id=None, last_live_id=None)
                    finally:
                        _active.discard(login.lower())

                threading.Thread(target=run_one, daemon=True).start()
            except Exception:
                pass
        time.sleep(max(15, LIVE_POLL_SECONDS))


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
