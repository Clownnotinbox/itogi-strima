"""Эвристики многоступенчатого отсева. Это «клей» между STT и LLM:
окна для LLM, восстановление таймкодов, просодия (громкость), финальный балл и дедуп.

Идея funnel'а (подробно в README): дешёвые правила отбраковывают явный мусор ещё до LLM,
LLM достаёт кандидатов, второй LLM-проход их судит, а итоговый балл смешивает оценку жюри
с сигналами, которые модели даются плохо — длина, деиксис, обобщающие маркеры и громкость.
"""
import math
import re

from config import (
    MAX_WORDS,
    MIN_WORDS,
    WINDOW_OVERLAP,
    WINDOW_SEGMENTS,
)

# Слова-паразиты и служебная речь — их доля топит фразу.
FILLERS = {
    "ну", "вот", "это", "значит", "типа", "короче", "блин", "как бы", "эээ", "ээ", "мм",
    "это самое", "в общем", "так сказать", "собственно", "получается", "чё", "че", "щас",
    "ага", "угу", "окей", "ладно", "слушай", "смотри", "погоди", "секунду", "секундочку",
}
# Обобщающие / «весомые» маркеры — часто ядро афоризма.
MARKERS = {
    "жизнь", "жизни", "смерть", "любовь", "судьба", "человек", "люди", "всегда", "никогда",
    "каждый", "главное", "счастье", "правда", "время", "деньги", "мир", "душа", "страх",
    "свобода", "сила", "выбор", "смысл", "никто", "всё", "все", "ничего",
}
# Деиктики: если фраза начинается с них — она почти наверняка требует контекста.
DEIXIS_START = {
    "он", "она", "они", "оно", "это", "этот", "эта", "вот", "тут", "там", "здесь",
    "сейчас", "сегодня", "тогда", "потом", "поэтому", "а", "и", "но",
}

_word_re = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)


def norm_tokens(text: str) -> list[str]:
    return _word_re.findall(text.lower())


# --------------------------------------------------------------------------- окна
def build_windows(segments: list[dict], loudness=None) -> list[dict]:
    """Скользящие окна с перекрытием, чтобы фраза на стыке сегментов не потерялась.
    Каждому окну считаем дешёвый «приор перспективности» — по нему потом предотсев."""
    windows = []
    step = max(1, WINDOW_SEGMENTS - WINDOW_OVERLAP)
    for i in range(0, len(segments), step):
        chunk = segments[i : i + WINDOW_SEGMENTS]
        if not chunk:
            break
        text = " ".join(s["text"].strip() for s in chunk)
        windows.append({"text": text, "prior": window_prior(chunk, loudness)})
        if i + WINDOW_SEGMENTS >= len(segments):
            break
    return windows


def window_prior(chunk: list[dict], loudness=None) -> float:
    """Насколько окно вообще стоит показывать LLM (0..1). Максимум по сегментам:
    хороший сегмент = нормальная длина, мало паразитов, есть маркеры или всплеск громкости."""
    best = 0.0
    for s in chunk:
        sig = signals(s["text"])
        pr = prosody(s.get("start"), s.get("end"), loudness) if loudness else 0.0
        val = (
            0.45 * sig["length"]
            + 0.25 * sig["marker"]
            + 0.15 * sig["diversity"]
            + 0.15 * pr
            - 0.30 * sig["filler"]
        )
        best = max(best, val)
    return max(0.0, min(1.0, best))


# ---------------------------------------------------------------- восстановление времени
def locate(text: str, words: list[dict], segments: list[dict]) -> tuple[float | None, float | None]:
    """Находим спан фразы. Сначала по словам с таймкодами, иначе по сегментам."""
    cand = norm_tokens(text)
    if not cand:
        return None, None

    if words:
        flat = [w for w in words if w.get("word")]
        toks = [norm_tokens(w["word"])[0] if norm_tokens(w["word"]) else "" for w in flat]
        n = len(cand)
        best_i, best_hits = -1, -1
        for i in range(0, max(1, len(toks) - n + 1)):
            window = toks[i : i + n]
            hits = sum(1 for a, b in zip(window, cand) if a == b)
            if hits > best_hits:
                best_hits, best_i = hits, i
        if best_i >= 0 and best_hits >= max(2, n // 2):
            span = flat[best_i : best_i + n]
            return span[0].get("start"), span[-1].get("end")

    # fallback: сегмент с максимальным пересечением токенов
    cand_set = set(cand)
    best_seg, best_overlap = None, 0
    for s in segments:
        overlap = len(cand_set & set(norm_tokens(s["text"])))
        if overlap > best_overlap:
            best_overlap, best_seg = overlap, s
    if best_seg:
        return best_seg.get("start"), best_seg.get("end")
    return None, None


# ---------------------------------------------------------------------- сигналы-эвристики
def signals(text: str) -> dict:
    toks = norm_tokens(text)
    n = len(toks)
    if n == 0:
        return {"n_words": 0, "length": 0, "filler": 1, "marker": 0, "deixis": 1, "diversity": 0}

    # длина: гауссиана с пиком ~8 слов
    length = math.exp(-((n - 8) ** 2) / (2 * 5 ** 2))
    if n < MIN_WORDS or n > MAX_WORDS:
        length *= 0.25

    joined = " " + " ".join(toks) + " "
    filler_hits = sum(joined.count(" " + f + " ") for f in FILLERS)
    filler = min(1.0, filler_hits / max(1, n))

    marker = min(1.0, sum(1 for t in toks if t in MARKERS) / max(1, n) * 3)
    deixis = 1.0 if toks[0] in DEIXIS_START else 0.0
    diversity = len(set(toks)) / n
    return {
        "n_words": n,
        "length": length,
        "filler": filler,
        "marker": marker,
        "deixis": deixis,
        "diversity": diversity,
    }


def prosody(t_start, t_end, loudness) -> float:
    """Средняя нормированная громкость на отрезке (0..1). loudness = список (t, level0..1)."""
    if not loudness or t_start is None or t_end is None:
        return 0.0
    vals = [lv for (t, lv) in loudness if t_start <= t <= t_end]
    if not vals:
        # ближайшая точка
        vals = [min(loudness, key=lambda p: abs(p[0] - t_start))[1]]
    return sum(vals) / len(vals)


AXES_WEIGHTS = {
    "citiruemost": 0.30,
    "emotsiya": 0.22,
    "originalnost": 0.18,
    "obraznost": 0.16,
    "samodost": 0.14,
}


def final_score(axes: dict, sig: dict, pros: float) -> float:
    base = sum(axes.get(a, 0) * w for a, w in AXES_WEIGHTS.items())  # 0..100 (ось originalnost*0.10 → сумма весов 0.92)
    base /= sum(AXES_WEIGHTS.values())

    score = base
    score *= 0.6 + 0.4 * sig["length"]        # длина может срезать до 40%
    score -= sig["filler"] * 40               # паразиты
    score -= sig["deixis"] * 22               # требует контекста
    score -= (1 - sig["diversity"]) * 12       # повторы слов
    score += sig["marker"] * 8                # обобщающие маркеры
    score += pros * 14                        # эмоциональный всплеск по громкости
    return max(0.0, min(100.0, score))


# --------------------------------------------------------------------------- дедуп
def dedupe(cands: list[dict]) -> list[dict]:
    """Semantic-lite: похожие по токенам фразы схлопываем, оставляя лучший балл."""
    kept: list[dict] = []
    for c in sorted(cands, key=lambda x: x["score"], reverse=True):
        c_set = set(norm_tokens(c["text"]))
        dup = False
        for k in kept:
            k_set = set(norm_tokens(k["text"]))
            union = c_set | k_set
            if union and len(c_set & k_set) / len(union) >= 0.6:
                dup = True
                break
        if not dup:
            kept.append(c)
    return kept
