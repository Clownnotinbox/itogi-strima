"""Пайплайн: ссылка -> аудио (yt-dlp+ffmpeg) -> STT (faster-whisper) ->
просодия (громкость) -> воронка фильтрации (эвристики + 2 прохода Ollama) -> топ-N фраз.
"""
import gc
import glob
import json
import math
import os
import re
import subprocess
import wave
from difflib import SequenceMatcher
from pathlib import Path

import filtering as F
from config import (
    AUDIO_DIR,
    LANGUAGE,
    LIVE_MAX_WORDS,
    PRESELECT_RATIO,
    SCORE_KEEP_THRESHOLD,
    TOP_N,
    TRANSCRIPT_DIR,
    WHISPER_COMPUTE_CPU,
    WHISPER_COMPUTE_CUDA,
    WHISPER_DEVICE,
    WHISPER_MODEL,
    YTDLP_FORMAT,
)
from twitch_support import twitch_channel_login


# ------------------------------------------------------------- CUDA DLL (Windows)
def _add_cuda_dll_dirs():
    """faster-whisper/ctranslate2 ищут cublas/cudnn в PATH. Подкладываем dll из pip-колёс."""
    try:
        import nvidia  # noqa: F401
        roots = [Path(p) for p in nvidia.__path__]
    except Exception:
        return
    for root in roots:
        for sub in ("cublas", "cudnn"):
            for binpath in glob.glob(str(root / sub / "*")):
                if os.path.isdir(binpath) and (Path(binpath).name in ("bin", "lib")):
                    try:
                        os.add_dll_directory(binpath)
                    except Exception:
                        pass


_add_cuda_dll_dirs()

_MODEL = None
_MODEL_DEVICE = None


# ----------------------------------------------------------------------- скачивание
def _cookies_from_browser_spec(value: str) -> tuple:
    """Accept `chrome` or `chrome:Default` / `chrome:Profile 2` from env."""
    value = (value or "").strip()
    if not value:
        return ()
    if ":" not in value:
        return (value,)
    browser, profile = value.split(":", 1)
    browser = browser.strip()
    profile = profile.strip()
    return (browser, profile) if profile else (browser,)


def _ytdlp_opts(url: str, extra: dict | None = None) -> dict:
    from config import COOKIES_FILE, COOKIES_FROM_BROWSER

    # для Twitch тянем именно аудио-дорожку (audio_only) — меньше трафика, «там аудио»
    fmt = YTDLP_FORMAT
    if "twitch.tv" in url:
        fmt = "audio_only/bestaudio/best"
    opts = {
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "retries": 3,
    }
    if COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = _cookies_from_browser_spec(COOKIES_FROM_BROWSER)
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    if extra:
        opts.update(extra)
    return opts


def _latest_twitch_vod_url(login: str, on_status=None) -> str:
    import yt_dlp

    videos_url = f"https://www.twitch.tv/{login}/videos?filter=archives&sort=time"
    if on_status:
        on_status(f"ищу последнюю запись Twitch: {login}")

    opts = _ytdlp_opts(
        videos_url,
        {
            "skip_download": True,
            "extract_flat": "in_playlist",
            "playlistend": 1,
        },
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        playlist = ydl.extract_info(videos_url, download=False)

    entries = [e for e in (playlist.get("entries") or []) if e]
    if not entries:
        raise RuntimeError(
            f"у {login} нет доступных записей Twitch: дай ссылку на VOD или включи сохранение VOD на Twitch"
        )

    first = entries[0]
    url = first.get("url") or ""
    if url.startswith("http"):
        return url

    vod_id = str(first.get("id") or "")
    if vod_id.startswith("v"):
        vod_id = vod_id[1:]
    if vod_id.isdigit():
        return f"https://www.twitch.tv/videos/{vod_id}"

    raise RuntimeError("не удалось определить ссылку на последнюю запись Twitch")


def resolve_media_url(url: str, on_status=None) -> str:
    login = twitch_channel_login(url)
    if login:
        return _latest_twitch_vod_url(login, on_status=on_status)
    return url


def download_audio(url: str, stream_id: int, on_status=None) -> dict:
    """Качаем аудиодорожку и приводим к 16k mono wav (нужно и STT, и просодии).
    Для Twitch берём audio_only; поддержаны куки для защищённого контента; live отсекаем."""
    import yt_dlp

    # сначала лёгкий проб: узнать название/длительность и не влететь в бесконечный live
    with yt_dlp.YoutubeDL(_ytdlp_opts(url, {"skip_download": True})) as ydl:
        probe = ydl.extract_info(url, download=False)
    if probe.get("is_live"):
        raise RuntimeError("это идущий сейчас live-эфир — дождись VOD/записи и дай ссылку на неё")

    out_tmpl = str(AUDIO_DIR / f"{stream_id}.%(ext)s")
    with yt_dlp.YoutubeDL(_ytdlp_opts(url, {"outtmpl": out_tmpl})) as ydl:
        info = ydl.extract_info(url, download=True)

    src = None
    dl = (info.get("requested_downloads") or [{}])[0]
    src = dl.get("filepath")
    if not src or not os.path.exists(src):
        hits = glob.glob(str(AUDIO_DIR / f"{stream_id}.*"))
        src = hits[0] if hits else None
    if not src:
        raise RuntimeError("не удалось найти скачанный аудиофайл")

    wav = str(AUDIO_DIR / f"{stream_id}.wav")
    if on_status:
        on_status("перекодирую аудио в 16кГц mono")
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "16000", "-vn", wav],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if src != wav and os.path.exists(src):
        try:
            os.remove(src)
        except OSError:
            pass

    return {
        "wav": wav,
        "title": info.get("title") or url,
        "duration": info.get("duration"),
        "source": info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url") or url,
    }


# ----------------------------------------------------------------------------- STT
def _load_model():
    global _MODEL, _MODEL_DEVICE
    if _MODEL is not None:
        return _MODEL, _MODEL_DEVICE
    from faster_whisper import WhisperModel

    tries = []
    if WHISPER_DEVICE in ("auto", "cuda"):
        tries.append(("cuda", WHISPER_COMPUTE_CUDA))
    tries.append(("cpu", WHISPER_COMPUTE_CPU))

    last_err = None
    for device, compute in tries:
        try:
            _MODEL = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute)
            _MODEL_DEVICE = device
            return _MODEL, device
        except Exception as e:  # cuda не завелась -> откат на cpu
            last_err = e
    raise RuntimeError(f"не удалось загрузить STT-модель: {last_err}")


def _free_model():
    global _MODEL, _MODEL_DEVICE
    _MODEL = None
    _MODEL_DEVICE = None
    gc.collect()


def transcribe(wav_path: str, on_status=None):
    model, device = _load_model()
    if on_status:
        on_status(f"расшифровка (Whisper {WHISPER_MODEL}, {device})")
    segments_gen, info = model.transcribe(
        wav_path,
        language=LANGUAGE,
        task="transcribe",
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=True,
        condition_on_previous_text=True,
    )
    segments, words = [], []
    for seg in segments_gen:
        segments.append({"text": seg.text, "start": seg.start, "end": seg.end})
        for w in seg.words or []:
            words.append({"word": w.word, "start": w.start, "end": w.end})
    return segments, words


def _shift_timing(items: list[dict], offset: float) -> list[dict]:
    out = []
    for item in items:
        row = dict(item)
        for key in ("start", "end"):
            if row.get(key) is not None:
                row[key] = row[key] + offset
        out.append(row)
    return out


def save_transcript(
    stream_id: int,
    segments: list[dict],
    words: list[dict] | None = None,
    meta: dict | None = None,
    chunk_index: int | None = None,
    offset: float = 0.0,
    loudness: list[tuple[float, float]] | None = None,
) -> dict:
    """Сохраняем STT-результат, чтобы речь не жила только в памяти процесса."""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f".live.{chunk_index:04d}" if chunk_index is not None else ""
    base = TRANSCRIPT_DIR / f"{stream_id}{suffix}"
    abs_segments = _shift_timing(segments, offset)
    abs_words = _shift_timing(words or [], offset)
    text = "\n".join(s.get("text", "").strip() for s in abs_segments if s.get("text", "").strip())
    payload = {
        "stream_id": stream_id,
        "chunk_index": chunk_index,
        "offset": offset,
        "meta": meta or {},
        "text": text,
        "segments": abs_segments,
        "words": abs_words,
        "loudness": _shift_timing(
            [{"start": t, "end": t, "level": lv} for t, lv in (loudness or [])],
            offset,
        ),
    }
    Path(f"{base}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    Path(f"{base}.txt").write_text(text, "utf-8")
    return payload


# ------------------------------------------------------------------------- просодия
def compute_loudness(wav_path: str, hz: float = 10.0):
    """RMS-огибающая громкости, нормированная по 95-му перцентилю (0..1) — маркер эмоций."""
    try:
        import numpy as np
    except Exception:
        return []
    with wave.open(wav_path, "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    if not raw:
        return []
    import numpy as np

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    frame = max(1, int(sr / hz))
    trimmed = audio[: len(audio) - len(audio) % frame].reshape(-1, frame)
    rms = np.sqrt((trimmed ** 2).mean(axis=1) + 1e-9)
    p95 = np.percentile(rms, 95) or 1.0
    level = np.clip(rms / p95, 0.0, 1.0)
    return [(i / hz, float(level[i])) for i in range(len(level))]


# ----------------------------------------------------------- воронка фильтрации
def filter_funnel(segments, words, loudness, meta, on_status=None) -> list[dict]:
    import ollama_client as O

    windows = F.build_windows(segments, loudness)
    # предотсев: до LLM доходит только PRESELECT_RATIO лучших окон
    if 0 < PRESELECT_RATIO < 1 and len(windows) > 4:
        keep = max(4, math.ceil(len(windows) * PRESELECT_RATIO))
        windows = sorted(windows, key=lambda w: w["prior"], reverse=True)[:keep]

    raw_candidates: list[dict] = []
    seen = set()
    for idx, w in enumerate(windows, 1):
        if on_status and idx % 5 == 0:
            on_status(f"фильтрация: окно {idx}/{len(windows)}, кандидатов {len(raw_candidates)}")
        for c in O.extract_candidates(w["text"]):
            key = " ".join(F.norm_tokens(c["text"]))
            if len(key) < 4 or key in seen:
                continue
            seen.add(key)
            raw_candidates.append(c)

    if not raw_candidates:
        return []

    # проход 2 — жюри (батчами, чтобы не плодить вызовы)
    if on_status:
        on_status(f"оценка кандидатов ({len(raw_candidates)})")
    axes_all: list[dict] = []
    B = 8
    for i in range(0, len(raw_candidates), B):
        batch = raw_candidates[i : i + B]
        axes_all.extend(O.judge_candidates([c["text"] for c in batch]))

    scored = []
    for c, axes in zip(raw_candidates, axes_all):
        t0, t1 = F.locate(c["text"], words, segments)
        sig = F.signals(c["text"])
        pros = F.prosody(t0, t1, loudness)
        score = F.final_score(axes, sig, pros)
        if score < SCORE_KEEP_THRESHOLD:
            continue
        tags = []
        if sig["marker"] > 0:
            tags.append("обобщение")
        if pros > 0.6:
            tags.append("на эмоциях")
        if sig["n_words"] <= 6:
            tags.append("лаконично")
        scored.append(
            {
                "text": c["text"].strip(" .,-—"),
                "t_start": t0,
                "t_end": t1,
                "score": round(score, 1),
                "emotion": c.get("emotion") or "",
                "reason": c.get("reason") or "",
                "tags": tags,
            }
        )

    scored = F.dedupe(scored)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:TOP_N]


_STRONG_WORDS = {
    "бля",
    "блять",
    "блядь",
    "сука",
    "пиздец",
    "пизда",
    "хуй",
    "хуйню",
    "хуйня",
    "ебать",
    "ебаный",
    "ебнутый",
    "охуеть",
    "нахуй",
    "жесть",
    "заебало",
    "нихуя",
}
_JOY_WORDS = {"ахаха", "хаха", "смешно", "кайф", "кайфово", "имба", "класс", "топ"}
_SAD_WORDS = {"грустно", "жалко", "больно", "тяжело", "устал", "устала", "страшно", "одиноко"}
_IRONY_WORDS = {"ирония", "сарказм", "конечно", "смешно", "кринж", "логика", "понятно", "видимо", "ну да", "как будто"}
_HOOK_WORDS = {"запомните", "главное", "залетаем"}
_WEAK_HOOK_WORDS = {"реально", "сейчас", "давайте", "ребят", "смотрите", "смотри", "интересно"}
_UNIVERSAL_WORDS = {"никто", "всегда", "никогда", "каждый", "любого"}
_BAD_STARTS = {
    "и",
    "а",
    "но",
    "вот",
    "это",
    "там",
    "тут",
    "к",
    "в",
    "на",
    "у",
    "с",
    "то",
    "ой",
    "ну",
    "просто",
    "если",
    "чтобы",
    "только",
    "может",
    "при",
}
_BAD_ENDS = {
    "и",
    "а",
    "но",
    "в",
    "на",
    "у",
    "с",
    "к",
    "по",
    "за",
    "что",
    "как",
    "не",
    "т",
    "чем",
    "если",
    "тоже",
    "вот",
    "там",
    "тут",
    "же",
}
_SELF_WORDS = {"я", "мне", "меня", "мой", "моя", "моё", "мое", "мы", "нам", "нас", "себя"}
_CONTEXT_WORDS = {
    "это",
    "этот",
    "эта",
    "эти",
    "там",
    "тут",
    "здесь",
    "сейчас",
    "сегодня",
    "их",
    "его",
    "ее",
    "её",
    "они",
    "он",
    "она",
    "оно",
    "этом",
    "этим",
    "этого",
    "этой",
    "так",
    "такой",
    "такая",
}
_LIVE_FILLERS = set(F.FILLERS) | {
    "бля",
    "блять",
    "блядь",
    "нахуй",
    "сука",
    "типа",
    "реально",
    "конечно",
    "то",
    "есть",
    "же",
}
_FEMALE_SPEAKER_MASC = {
    "делал",
    "сделал",
    "сказал",
    "думал",
    "понял",
    "забыл",
    "видел",
    "хотел",
    "пошел",
    "пошёл",
}
_WEAK_PHRASES = {
    "ну то есть",
    "так секунду",
    "сейчас посмотрю",
    "сейчас открою",
}
_PROFANITY_RE = re.compile(r"(бля|бляд|блять|хуй|хуйн|нахуй|пизд|еба|ёба|сука|заеб|охуе|нихуя)")
_WEIRD_RE = re.compile(r"\b(npc|iq|бан|запретк|скуф)\b", re.IGNORECASE)

_LIVE_DROP_PATTERNS = (
    r"\b(?:так,?\s*)?(?:секунду|минуту),?\s+(?:сейчас|щас)\b",
    r"\b(?:включите|выключите|врубайте|вырубайте)\b.{0,40}\b(?:музык|видео|запис|микрофон)\w*\b",
)

_LIVE_LOW_PRIORITY_PATTERNS = (
    r"\bу меня\b.{0,30}\b(?:не )?(?:открывается|загружается|работает)\b",
    r"\b(?:открою|закрою|перезапущу)\b.{0,25}\b(?:настройк|браузер|программ)\w*\b",
)

_LIVE_HIGH_PRIORITY_PATTERNS = (
    r"\b(?:за|после)\s+кажд\w*\b.{0,80}\b(?:буду|стану|начну)\b",
    r"\bесли бы\b.{0,100}\bя бы\b",
    r"\bпочему\b.{0,100}\bмне интересно\b",
)

_LIVE_REPEATABLE_WORDS = {
    "я", "ты", "мы", "вы", "он", "она", "они", "не", "ни", "и", "а", "но", "как",
}

_LIVE_TOKEN_REPLACEMENTS = {}


def _normalize_live_candidate_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = re.sub(r"^(?:в основном,?\s*)?то есть\s+", "", cleaned, flags=re.IGNORECASE)
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _safe_live_fix(original: str, proposed: str) -> str:
    """Accept only small, transcript-like edits; reject creative LLM rewrites."""
    fixed = _normalize_live_candidate_text(proposed)
    if not fixed or len(fixed) > 180:
        return ""

    original_tokens = F.norm_tokens(original)
    fixed_tokens = F.norm_tokens(fixed)
    if not (4 <= len(fixed_tokens) <= LIVE_MAX_WORDS):
        return ""
    if abs(len(original_tokens) - len(fixed_tokens)) > 3:
        return ""

    matcher = SequenceMatcher(None, original_tokens, fixed_tokens, autojunk=False)
    edits = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            edits += max(i2 - i1, j2 - j1)
    if edits > 3 or matcher.ratio() < 0.72:
        return ""
    return fixed


def _live_verbatim_tokens(text: str) -> list[str]:
    return [_LIVE_TOKEN_REPLACEMENTS.get(token, token) for token in F.norm_tokens(text)]


def _contains_token_span(haystack: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(haystack[i : i + len(needle)] == needle for i in range(len(haystack) - len(needle) + 1))


def _safe_live_context_quote(original: str, proposed: str, context: str) -> str:
    """Accept a longer quote only when it is a continuous verbatim span of saved context."""
    proposed = re.sub(r"\s+", " ", proposed).strip()
    if not proposed or len(proposed) > 260:
        return ""
    original_tokens = _live_verbatim_tokens(original)
    proposed_tokens = _live_verbatim_tokens(proposed)
    context_tokens = _live_verbatim_tokens(context)
    if not (4 <= len(proposed_tokens) <= LIVE_MAX_WORDS):
        return ""
    if not _contains_token_span(proposed_tokens, original_tokens):
        return ""
    if not _contains_token_span(context_tokens, proposed_tokens):
        return ""
    return _normalize_live_candidate_text(proposed)


def _dedupe_live_for_rerank(candidates: list[dict]) -> list[dict]:
    """Prefer a complete context-backed superset over an older clipped quote."""
    kept: list[dict] = []
    for raw in candidates:
        candidate = dict(raw)
        candidate["text"] = _normalize_live_candidate_text(candidate.get("text") or "")
        tokens = _live_verbatim_tokens(candidate["text"])
        if not tokens:
            continue
        replaced = False
        duplicate = False
        for index, existing in enumerate(kept):
            existing_tokens = _live_verbatim_tokens(existing["text"])
            candidate_extends = _contains_token_span(tokens, existing_tokens) and len(tokens) > len(existing_tokens)
            existing_extends = _contains_token_span(existing_tokens, tokens) and len(existing_tokens) > len(tokens)
            union = set(tokens) | set(existing_tokens)
            similar = bool(union) and (
                len(set(tokens) & set(existing_tokens)) / len(union) >= 0.6
                or candidate_extends
                or existing_extends
            )
            if not similar:
                continue
            duplicate = True
            if candidate_extends:
                kept[index] = candidate
            elif not existing_extends and (candidate.get("score") or 0) > (existing.get("score") or 0):
                kept[index] = candidate
            replaced = True
            break
        if not duplicate and not replaced:
            kept.append(candidate)
    return kept


def _live_quote_passes_final_gate(text: str) -> bool:
    sig = F.signals(text)
    features = _live_features(text, sig, 0.0)
    tokens = F.norm_tokens(text)
    if sig["n_words"] < 5 or sig["n_words"] > LIVE_MAX_WORDS:
        return False
    if features["weak_phrase"] or features["suspicious_repeat"] or features["merged_thoughts"]:
        return False
    if features["dangling"] or features["trailing_open"]:
        return False
    if tokens and tokens[-1] in (_BAD_ENDS | {"это", "этот", "эта", "эти", "то", "но", "если", "чтобы"}):
        return False
    if features["asr_garble"]:
        return False
    if sig["n_words"] <= 6 and features["strong"] and not (
        features["contrast"] or features["weird"] or features["playful_promise"]
    ):
        return False
    if features["profane_only"] and ". " not in text:
        return False
    if features["needs_context"] and not (
        features["contrast"] or features["weird"] or features["playful_promise"] or ". " in text
    ):
        return False
    if features["fragment"] and not (
        features["contrast"]
        or features["weird"]
        or features["playful_promise"]
        or features["sharp_question"]
    ):
        return False
    return True


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _has_word(text: str, words: set[str]) -> bool:
    low = text.lower()
    toks = set(F.norm_tokens(low))
    return bool(toks & words) or any(w in low for w in words if " " in w)


def _live_features(text: str, sig: dict, pros: float) -> dict:
    low = text.lower()
    toks = F.norm_tokens(low)
    first = toks[0] if toks else ""
    last = toks[-1] if toks else ""
    strong = sum(1 for t in toks if t in _STRONG_WORDS or _PROFANITY_RE.search(t))
    profanity_density = strong / max(1, len(toks))
    has_if_then = bool(re.search(r"\bесли\b.+(?:^|[\s,])то(?:[\s,]|$)", low))
    contrast = int(
        bool(
            re.search(r"\bне\b.+\bа\b|\bно\b|\bзато\b|\bхотя\b", low)
            or re.search(r"\bне\b.+,\s*(?:а\s+)?(?:это|ты|я|мы)\b", low)
        )
        or has_if_then
    )
    definition = int(bool(re.search(r"\bэто\b.+\b(когда|то|способ|значит)\b", low)))
    absolute = int(any(w in toks for w in _UNIVERSAL_WORDS))
    hook = int(_has_word(low, _HOOK_WORDS))
    weak_hook = int(_has_word(low, _WEAK_HOOK_WORDS))
    punch = int(strong > 0 or contrast or definition or (absolute and hook))
    question = int("?" in text or any(w in toks for w in ("почему", "зачем", "как", "когда")))
    sharp_question = int(
        question
        and sig["n_words"] >= 6
        and bool(strong or hook or weak_hook or (first in {"почему", "зачем"} and "?" in text))
    )
    long_quote = int(sig["n_words"] > 24)
    drop_pattern = int(_matches_any(low, _LIVE_DROP_PATTERNS))
    low_priority = int(_matches_any(low, _LIVE_LOW_PRIORITY_PATTERNS))
    high_priority = int(_matches_any(low, _LIVE_HIGH_PRIORITY_PATTERNS))
    playful_promise = int(
        bool(
            re.search(
                r"\b(?:за|после)\s+(?:кажд(?:ый|ую|ое|ого)|люб(?:ой|ую|ое))\b.{0,80}"
                r"\b(?:буду|стану|начну)\b",
                low,
            )
            or re.search(r"\b(?:буду|стану|начну)\b.{0,60}\b(?:за|после|кажд)", low)
        )
    )
    weak_phrase = int(any(p in low for p in _WEAK_PHRASES) or drop_pattern)
    context_verb = int(any(w in toks for w in ("включу", "включила", "закончите", "стрим", "цены", "вагон")))
    female_mismatch = int(any(w in toks for w in _FEMALE_SPEAKER_MASC))
    dangling = int(
        bool(
            re.search(
                r"\b(чтобы|который|которая|которые|которых|которого|которой|если|когда)\b",
                low,
            )
        )
        and not (definition or contrast or question)
    )
    trailing_open = int(text.rstrip().endswith((",", "—", "-")))
    filler_hits = sum(1 for t in toks if t in _LIVE_FILLERS)
    context_hits = sum(1 for t in toks if t in _CONTEXT_WORDS)
    self_hits = sum(1 for t in toks if t in _SELF_WORDS)
    content_words = [
        t for t in toks if t not in _LIVE_FILLERS and t not in _CONTEXT_WORDS and t not in _SELF_WORDS
    ]
    content_ratio = len(content_words) / max(1, len(toks))
    repeated_ratio = (max((toks.count(t) for t in set(toks)), default=0) / max(1, len(toks)))
    suspicious_repeat = int(
        len(toks) <= 12
        and any(toks.count(t) >= 2 for t in set(toks) if t not in _LIVE_REPEATABLE_WORDS)
        and not (playful_promise or (question and ". " in text))
    )
    merged_thoughts = int(
        bool(re.search(r"\bя\s+сам(?:ая|ый)\b.{0,70}\bкак\b.{0,35}\bкоманд", low))
    )
    digit_tokens = sum(1 for t in toks if t.isdigit())
    live_marker = int(any(t in (F.MARKERS - {"все", "всё", "ничего"}) for t in toks))
    weird = int(
        bool(_WEIRD_RE.search(low))
        or bool(re.search(r"\bкого\b.+\bудив", low))
        or bool(re.search(r"\bвырубай(те)?\b", low))
    )
    shape = int(
        bool(
            contrast
            or definition
            or (absolute and not weak_phrase)
            or hook
            or weird
            or high_priority
            or playful_promise
            or sharp_question
        )
    )
    conditional_fragment = int(
        ("если" in toks or "чтобы" in toks)
        and not question
        and (not contrast or last in {"тоже", "если", "то"})
    )
    soft_fragment = int(bool(re.search(r"\bно вот\b|\bа так\b", low)))
    profanity_lead_fragment = int(first in _STRONG_WORDS and sig["n_words"] > 8 and not weird)
    fragment = int(
        first in _BAD_STARTS
        or last in _BAD_ENDS
        or (len(last) <= 1 and len(toks) <= 8)
        or trailing_open
        or weak_phrase
        or conditional_fragment
        or soft_fragment
        or profanity_lead_fragment
        or (context_verb and not (contrast or definition or weird))
        or dangling
    )
    needs_context = int(
        (context_hits + self_hits >= 2 or (self_hits and not (question or shape)))
        and not (definition or contrast or playful_promise or sharp_question)
    )
    profane_only = int(strong > 0 and not shape and not question)
    weak_question = int(question and needs_context and not shape)
    asr_garble = int(
        (content_ratio < 0.42 and not shape)
        or (repeated_ratio >= 0.24 and not shape)
        or suspicious_repeat
        or merged_thoughts
        or (digit_tokens >= 2 and not weird)
        or (sig["n_words"] >= 9 and not shape and not re.search(r"[.!?\u2026]$", text.strip()))
    )
    intensity = min(1.0, min(strong, 2) * 0.18 + pros * 0.50 + hook * 0.22 + weak_hook * 0.08 + question * 0.10)
    long_context = int(long_quote and not weak_phrase and not dangling and not female_mismatch and (punch or contrast or definition or hook or pros > 0.58))
    zavoz = min(
        1.0,
        0.30 * intensity
        + 0.18 * shape
        + 0.16 * contrast
        + 0.14 * hook
        + 0.10 * weird
        + 0.10 * definition
        + 0.08 * absolute
        + 0.16 * playful_promise
        + 0.12 * sharp_question,
    )
    return {
        "strong": strong,
        "contrast": contrast,
        "definition": definition,
        "absolute": absolute,
        "hook": hook,
        "weak_hook": weak_hook,
        "punch": punch,
        "question": question,
        "sharp_question": sharp_question,
        "long_quote": long_quote,
        "long_context": long_context,
        "fragment": fragment,
        "weak_phrase": weak_phrase,
        "drop_pattern": drop_pattern,
        "low_priority": low_priority,
        "high_priority": high_priority,
        "playful_promise": playful_promise,
        "context_verb": context_verb,
        "female_mismatch": female_mismatch,
        "dangling": dangling,
        "trailing_open": trailing_open,
        "filler_hits": filler_hits,
        "context_hits": context_hits,
        "self_hits": self_hits,
        "content_ratio": content_ratio,
        "repeated_ratio": repeated_ratio,
        "suspicious_repeat": suspicious_repeat,
        "merged_thoughts": merged_thoughts,
        "digit_tokens": digit_tokens,
        "live_marker": live_marker,
        "weird": weird,
        "shape": shape,
        "conditional_fragment": conditional_fragment,
        "soft_fragment": soft_fragment,
        "profanity_lead_fragment": profanity_lead_fragment,
        "needs_context": needs_context,
        "profane_only": profane_only,
        "weak_question": weak_question,
        "asr_garble": asr_garble,
        "profanity_density": profanity_density,
        "intensity": intensity,
        "zavoz": zavoz,
    }


def _live_emotion(text: str, features: dict, pros: float) -> str:
    low = text.lower()
    if _has_word(low, _SAD_WORDS):
        return "грусть"
    if _has_word(low, _JOY_WORDS):
        return "радость"
    if features["contrast"] or features["weird"] or _has_word(low, _IRONY_WORDS):
        return "ирония"
    if features["strong"] and any(w in low for w in ("бан", "хейт", "хуйн", "нахуй", "сука", "пизд")):
        return "гнев"
    if features["zavoz"] >= 0.42 or features["hook"] or features["absolute"]:
        return "азарт"
    if features["intensity"] > 0.35 or pros > 0.55:
        return "азарт"
    return "спокойствие"


def _live_tags(sig: dict, features: dict, pros: float) -> list[str]:
    tags: list[str] = []
    if features["zavoz"] >= 0.34:
        tags.append("завоз")
    if features["shape"] and not features["needs_context"]:
        tags.append("панч")
    if features["weird"]:
        tags.append("странно")
    if features["strong"] or pros > 0.55:
        tags.append("на эмоциях")
    if features["contrast"]:
        tags.append("контраст")
    if features["hook"]:
        tags.append("цепляет")
    if features["definition"] or features["live_marker"] > 0:
        tags.append("обобщение")
    if features.get("playful_promise"):
        tags.append("обещание")
    if features.get("sharp_question"):
        tags.append("вопрос")
    if features.get("long_context"):
        tags.append("контекст")
    if sig["n_words"] <= 8:
        tags.append("лаконично")
    return tags[:4]


def _live_reason(features: dict, tags: list[str]) -> str:
    if features.get("playful_promise"):
        return "смешное игровое обещание"
    if features.get("sharp_question"):
        return "резкий риторический вопрос"
    if features.get("long_context"):
        return "цельная длинная мысль"
    if features["weird"] and features["contrast"]:
        return "странный контраст, клипово"
    if features["weird"]:
        return "необычная формулировка"
    if features["contrast"] and features["hook"]:
        return "контрастный завоз"
    if features["definition"]:
        return "готовая формула"
    if features["hook"] and features["absolute"]:
        return "цепкий заход, обобщение"
    if features["hook"]:
        return "крючок для клипа"
    if features["question"] and not features["needs_context"]:
        return "цепкий вопрос"
    if features["strong"] >= 2 and features["shape"]:
        return "жесткая эмоция с мыслью"
    if features["strong"] and features["shape"]:
        return "эмоциональный удар"
    if features["absolute"]:
        return "обобщение без контекста"
    if features["punch"]:
        return "панчлайн"
    if "лаконично" in tags:
        return "коротко и выносится"
    return "самостоятельная формулировка"


def _live_candidate_parts(text: str) -> list[str]:
    parts = [p.strip(" \t\r\n\"'\u00ab\u00bb") for p in re.split(r"[.!?\u2026]+", text) if p.strip()]
    candidates = list(parts)
    for i in range(len(parts) - 1):
        merged = f"{parts[i]}. {parts[i + 1]}".strip()
        n_words = len(F.norm_tokens(merged))
        if 10 <= n_words <= LIVE_MAX_WORDS:
            candidates.append(merged)

    clauses = [p.strip(" \t\r\n\"'\u00ab\u00bb") for p in re.split(r"[,;:\u2014]+", text) if p.strip()]
    for i in range(len(clauses)):
        for width in (2, 3, 4):
            window = clauses[i : i + width]
            if len(window) != width:
                continue
            merged = ", ".join(window)
            n_words = len(F.norm_tokens(merged))
            if 5 <= n_words <= LIVE_MAX_WORDS:
                candidates.append(merged)
    return candidates


def _live_segment_variants(
    segments: list[dict], seg_index: int
) -> list[tuple[str, float | None, float | None]]:
    """Verbatim candidates from one segment and up to two nearby segments."""
    seg = segments[seg_index]
    text = (seg.get("text") or "").strip()
    variants = [(part, seg.get("start"), seg.get("end")) for part in _live_candidate_parts(text)]

    merged = text
    previous_end = seg.get("end")
    for next_index in range(seg_index + 1, min(len(segments), seg_index + 4)):
        nxt = segments[next_index]
        nxt_text = (nxt.get("text") or "").strip()
        if not nxt_text:
            continue
        try:
            gap = float(nxt.get("start")) - float(previous_end)
        except (TypeError, ValueError):
            gap = 0.0
        if gap > 2.8:
            break
        candidate = f"{merged.rstrip('.!?…')}. {nxt_text}"
        n_words = len(F.norm_tokens(candidate))
        if n_words > LIVE_MAX_WORDS:
            break
        if n_words >= 5 and not nxt_text.rstrip().endswith((",", "—", "-")):
            variants.append((candidate, seg.get("start"), nxt.get("end")))
        merged = candidate
        previous_end = nxt.get("end")

    # A good spoken punch is often split as: question -> weak aside -> "although/but" answer.
    # Build that contrast without hard-coding topics or vocabulary from a particular stream.
    is_question = "?" in text or bool(re.search(r"^\s*(?:почему|зачем|как|когда)\b", text, re.IGNORECASE))
    if is_question:
        for contrast_index in range(seg_index + 1, min(len(segments), seg_index + 6)):
            contrast_text = (segments[contrast_index].get("text") or "").strip()
            if not re.match(r"^(?:хотя|но|зато|а\s+если|при\s+этом)\b", contrast_text, re.IGNORECASE):
                continue
            contrast_parts = [contrast_text]
            contrast_end = segments[contrast_index].get("end")
            if contrast_index + 1 < len(segments):
                continuation = (segments[contrast_index + 1].get("text") or "").strip()
                combined = f"{text.rstrip('.!?…')}? {contrast_text} {continuation}".strip()
                if continuation and len(F.norm_tokens(combined)) <= LIVE_MAX_WORDS:
                    contrast_parts.append(continuation)
                    contrast_end = segments[contrast_index + 1].get("end")
            candidate = f"{text.rstrip('.!?…')}? {' '.join(contrast_parts)}"
            if 5 <= len(F.norm_tokens(candidate)) <= LIVE_MAX_WORDS:
                variants.append((candidate, seg.get("start"), contrast_end))
            break

    unique = []
    seen: set[str] = set()
    for candidate, start, end in variants:
        key = " ".join(F.norm_tokens(candidate))
        if key and key not in seen:
            seen.add(key)
            unique.append((candidate, start, end))
    return unique


def score_live_quote_text(text: str, pros: float = 0.0) -> dict | None:
    """Score one complete quote with the same generic signals used by the live fast pass."""
    text = _normalize_live_candidate_text(text)
    sig = F.signals(text)
    if sig["n_words"] < 4 or sig["n_words"] > LIVE_MAX_WORDS:
        return None
    features = _live_features(text, sig, pros)
    if features["weak_phrase"] or features["suspicious_repeat"] or features["merged_thoughts"]:
        return None
    if features["dangling"] and not (
        features["high_priority"] or features["playful_promise"] or features["long_context"]
    ):
        return None
    if features["profane_only"] and not ((pros > 0.72 and sig["n_words"] <= 8) or ". " in text):
        return None

    quality = (
        30
        + 18 * sig["length"]
        + 9 * sig["diversity"]
        + 7 * features["live_marker"]
        - 18 * sig["filler"]
        - 8 * sig["deixis"]
        - 18 * features["fragment"]
        - 16 * features["female_mismatch"]
        - 12 * features["dangling"]
        - 8 * features["long_quote"]
        - 15 * features["needs_context"]
        - 18 * features["low_priority"]
        - 22 * max(0.0, features["profanity_density"] - 0.18)
        - 8 * max(0, features["filler_hits"] - 2)
    )
    bonus = (
        20 * features["zavoz"]
        + 8 * features["intensity"]
        + 10 * features["shape"]
        + 6 * features["hook"]
        + 3 * features["weak_hook"]
        + 5 * features["contrast"]
        + 8 * features["weird"]
        + 4 * pros
        + 8 * features["long_context"]
        + 14 * features["playful_promise"]
        + 12 * features["sharp_question"]
        + 6 * min(2, text.count(". "))
    )
    score = quality + bonus
    if features["profane_only"]:
        score = min(score, 56)
    if features["needs_context"] and not features["weird"]:
        score = min(score, 58)
    if features["fragment"]:
        score = min(score, 68 if features["weird"] else 60)
    if features["low_priority"]:
        score = min(score, 66)
    score = round(max(0.0, min(100.0, score)), 1)
    tags = _live_tags(sig, features, pros)
    return {
        "text": text,
        "score": score,
        "emotion": _live_emotion(text, features, pros),
        "reason": _live_reason(features, tags),
        "tags": tags,
    }


def quick_live_quotes(segments, loudness=None, limit: int = 3) -> list[dict]:
    """Fast no-LLM quote candidates for live mode, ranked for bright stream moments."""
    candidates = []
    for seg_index, seg in enumerate(segments):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        context_start = max(0, seg_index - 2)
        context_end = min(len(segments), seg_index + 3)
        context = " ".join(
            (item.get("text") or "").strip()
            for item in segments[context_start:context_end]
            if (item.get("text") or "").strip()
        )
        context = re.sub(r"\s+", " ", context).strip()[:700]
        variants = _live_segment_variants(segments, seg_index)
        for part, part_start, part_end in variants:
            part = _normalize_live_candidate_text(part)
            sig = F.signals(part)
            if sig["n_words"] < 4 or sig["n_words"] > LIVE_MAX_WORDS:
                continue
            pros = F.prosody(seg.get("start"), seg.get("end"), loudness)
            features = _live_features(part, sig, pros)
            if features["long_quote"] and not features["long_context"]:
                continue
            if features["weak_phrase"]:
                continue
            if features["suspicious_repeat"] or features["merged_thoughts"]:
                continue
            if features["dangling"] and not (
                features["high_priority"] or features["playful_promise"] or features["long_context"]
            ):
                continue
            if features["profane_only"] and not (
                (pros > 0.72 and sig["n_words"] <= 8) or ". " in part
            ):
                continue
            quality = (
                30
                + 18 * sig["length"]
                + 9 * sig["diversity"]
                + 7 * features["live_marker"]
                - 18 * sig["filler"]
                - 8 * sig["deixis"]
                - 18 * features["fragment"]
                - 16 * features["female_mismatch"]
                - 12 * features["dangling"]
                - 8 * features["long_quote"]
                - 15 * features["needs_context"]
                - 18 * features["low_priority"]
                - 22 * max(0.0, features["profanity_density"] - 0.18)
                - 8 * max(0, features["filler_hits"] - 2)
            )
            zavoz_bonus = (
                20 * features["zavoz"]
                + 8 * features["intensity"]
                + 10 * features["shape"]
                + 6 * features["hook"]
                + 3 * features["weak_hook"]
                + 5 * features["contrast"]
                + 8 * features["weird"]
                + 4 * pros
                + 8 * features["long_context"]
                + 14 * features["playful_promise"]
                + 12 * features["sharp_question"]
                + 14 * int(part_end != seg.get("end"))
                + 6 * min(2, part.count(". "))
            )
            score = quality + zavoz_bonus
            if features["profane_only"]:
                score = min(score, 56)
            if features["needs_context"] and not features["weird"]:
                score = min(score, 58)
            if features["fragment"]:
                score = min(score, 68 if features["weird"] else 60)
            if features["low_priority"]:
                score = min(score, 66)
            score = max(0, min(100, score))
            # Recall-first live pass: the LLM below is the precision gate. A low threshold here
            # prevents Whisper segment boundaries from hiding a good completed thought.
            if score < (48 if features["long_quote"] else 42):
                continue
            tags = _live_tags(sig, features, pros)
            candidates.append(
                {
                    "text": part,
                    "t_start": part_start,
                    "t_end": part_end,
                    "score": round(score, 1),
                    "emotion": _live_emotion(part, features, pros),
                    "reason": _live_reason(features, tags),
                    "tags": tags,
                    "context": context,
                }
            )

    candidates = F.dedupe(candidates)
    candidates.sort(key=lambda x: (x["score"], "завоз" in x.get("tags", [])), reverse=True)
    return candidates[:limit]


def dedupe_live_moments(candidates: list[dict], gap_seconds: float = 18.0) -> list[dict]:
    """Keep one best quote from a short live burst so one joke does not fill the whole top."""
    kept: list[dict] = []
    for c in sorted(candidates, key=lambda x: x.get("score") or 0, reverse=True):
        t = c.get("t_start")
        if t is None:
            kept.append(c)
            continue
        try:
            t_float = float(t)
        except (TypeError, ValueError):
            kept.append(c)
            continue
        duplicate_moment = False
        for k in kept:
            kt = k.get("t_start")
            if kt is None:
                continue
            try:
                if abs(t_float - float(kt)) < gap_seconds:
                    duplicate_moment = True
                    break
            except (TypeError, ValueError):
                continue
        if not duplicate_moment:
            kept.append(c)
    return kept


def _diverse_live_pool(pool: list[dict], limit: int, buckets: int = 12) -> list[dict]:
    if len(pool) <= limit:
        return sorted(pool, key=lambda q: q.get("score") or 0, reverse=True)
    timed = [q for q in pool if q.get("t_start") is not None]
    if not timed:
        return sorted(pool, key=lambda q: q.get("score") or 0, reverse=True)[:limit]
    times = [float(q["t_start"]) for q in timed]
    low, high = min(times), max(times)
    width = max(1.0, (high - low + 1.0) / buckets)
    groups: list[list[dict]] = [[] for _ in range(buckets)]
    for q in pool:
        try:
            index = min(buckets - 1, max(0, int((float(q.get("t_start")) - low) / width)))
        except (TypeError, ValueError):
            index = 0
        groups[index].append(q)
    for group in groups:
        group.sort(key=lambda q: q.get("score") or 0, reverse=True)
    selected: list[dict] = []
    depth = 0
    while len(selected) < limit and any(depth < len(group) for group in groups):
        for group in groups:
            if depth < len(group):
                selected.append(group[depth])
                if len(selected) == limit:
                    break
        depth += 1
    return selected


def rerank_live_quotes_with_llm(candidates: list[dict], max_candidates: int = 36) -> list[dict]:
    """Second-pass live rerank: keep clip-worthy quotes, reject STT mush and profanity-only lines."""
    if not candidates:
        return []
    import ollama_client as O

    pool = []
    for candidate in _dedupe_live_for_rerank(candidates):
        q = dict(candidate)
        q["text"] = _normalize_live_candidate_text(q.get("text") or "")
        sig = F.signals(q["text"])
        features = _live_features(q["text"], sig, 0.0)
        if sig["n_words"] < 4 or sig["n_words"] > LIVE_MAX_WORDS:
            continue
        if features["weak_phrase"] or features["suspicious_repeat"] or features["merged_thoughts"]:
            continue
        pool.append(q)
    pool = _diverse_live_pool(pool, max_candidates)

    judgments: list[dict] = []
    batch_size = 4
    for i in range(0, len(pool), batch_size):
        batch = pool[i : i + batch_size]
        try:
            judgments.extend(
                O.judge_live_candidates(
                    [q["text"] for q in batch],
                    [q.get("context") or "" for q in batch],
                )
            )
        except Exception:
            judgments.extend(
                {
                    "celnost": 0,
                    "clip": 0,
                    "zavoz": 0,
                    "surprise": 0,
                    "context": 0,
                    "verdict": "drop",
                    "reason": "",
                    "quote": "",
                    "fix": "",
                }
                for _ in batch
            )

    reranked: list[dict] = []
    for q, j in zip(pool, judgments):
        llm_score = (
            0.32 * j.get("celnost", 0)
            + 0.31 * j.get("clip", 0)
            + 0.10 * j.get("zavoz", 0)
            + 0.19 * j.get("surprise", 0)
            + 0.08 * j.get("context", 0)
        )
        if j.get("verdict") != "keep":
            continue
        if j.get("celnost", 0) < 60 or j.get("clip", 0) < 50 or j.get("context", 0) < 45:
            continue

        q2 = dict(q)
        expanded = _safe_live_context_quote(
            q2["text"],
            str(j.get("quote") or "").strip(),
            q2.get("context") or "",
        )
        if expanded:
            q2["text"] = expanded
        else:
            fix = _safe_live_fix(q2["text"], str(j.get("fix") or "").strip())
            if fix:
                q2["text"] = fix
        if not _live_quote_passes_final_gate(q2["text"]):
            continue
        fast_score = float(q.get("score") or 0)
        q2["score"] = round(max(0.0, min(100.0, fast_score * 0.35 + llm_score * 0.65)), 1)
        reason = (j.get("reason") or "").strip()
        if reason:
            q2["reason"] = reason
        reranked.append(q2)

    reranked = F.dedupe(reranked)
    reranked.sort(key=lambda x: x.get("score") or 0, reverse=True)
    return reranked


# ------------------------------------------------------------------- оркестрация
def process_stream(stream_id: int, url: str, on_stage):
    """on_stage(status, message) — колбэк для обновления БД/UI."""
    try:
        on_stage("downloading", "скачиваю аудио")
        media_url = resolve_media_url(url, on_status=lambda m: on_stage("downloading", m))
        meta = download_audio(media_url, stream_id, on_status=lambda m: on_stage("downloading", m))

        on_stage("transcribing", f"расшифровка (Whisper {WHISPER_MODEL})", meta)
        segments, words = transcribe(meta["wav"], on_status=lambda m: on_stage("transcribing", m))
        save_transcript(stream_id, segments, words, meta)
        loudness = compute_loudness(meta["wav"])
        _free_model()  # освобождаем VRAM перед LLM

        if not segments:
            on_stage("done", "речь не распознана")
            return meta, []

        on_stage("filtering", "ищу цитаты")
        quotes = filter_funnel(
            segments, words, loudness, meta, on_status=lambda m: on_stage("filtering", m)
        )
        on_stage("done", f"готово: {len(quotes)} фраз")
        return meta, quotes
    except Exception as e:
        _free_model()
        on_stage("error", str(e))
        raise
