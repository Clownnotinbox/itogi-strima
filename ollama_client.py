"""Клиент Ollama + промпты для двух проходов фильтрации.

Проход 1 (extract) — из окна расшифровки достаём дословных кандидатов.
Проход 2 (judge)   — каждого кандидата оцениваем по 5 осям, отсекаем ложные срабатывания.

Таймкоды LLM не трогает вообще: она возвращает только дословный текст, а привязку ко
времени мы восстанавливаем в Python нечётким сопоставлением с сегментами STT — так надёжнее.
"""
import json
import re

import requests

from config import OLLAMA_MODEL, OLLAMA_TIMEOUT, OLLAMA_URL

EXTRACT_SYSTEM = """Ты — придирчивый редактор клиповых цитат. Тебе дают кусок расшифровки русскоязычного стрима.
Твоя задача — выудить только фразы, которые реально хочется сохранить: смешные, странные,
запоминающиеся, завозные, эмоциональные, с неожиданной формулировкой или понятным клиповым моментом.

Жёсткие правила:
1. Бери ТОЛЬКО дословные фрагменты из текста. Ничего не переписывай, не сокращай, не исправляй.
2. Фраза должна быть цельной и понятной как отдельная цитата или короткий клиповый момент.
3. Отбрасывай: приветствия, бытовуху, техническую речь («так, секунду», «щас гляну»),
   ответы чату без самостоятельного смысла, рекламу, обрывки, воду, «ну вот», «короче»,
   бессвязные куски распознавания и просто мат без мысли/шутки/ситуации.
4. Лучше вернуть 0 фраз, чем слабые. Максимум 4 на фрагмент.

Ответ — строго JSON вида:
{"quotes":[{"quote":"дословная фраза","emotion":"одно слово: гнев/радость/грусть/ирония/азарт/спокойствие","reason":"почему это хорошая цитата, 3-6 слов"}]}
Если ничего достойного нет: {"quotes":[]}"""

JUDGE_SYSTEM = """Ты — строгий жюри-цитатолог. Тебе дают список фраз-кандидатов из стрима.
Оцени каждую по осям от 0 до 100:
- obraznost   — яркость, смешная/странная картинка, игра слов;
- samodost    — цельность и понятность как отдельной цитаты;
- emotsiya    — завоз, живость, эмоциональный заряд;
- originalnost— неожиданность, небанальность, мемность;
- citiruemost — реально ли захочется сохранить/процитировать.

Будь скупым на высокие баллы. Просто мат, обрывки и бессвязное STT должны получать низкие оценки.
Ответ — строго JSON: {"scores":[{"i":0,"obraznost":0,"samodost":0,"emotsiya":0,"originalnost":0,"citiruemost":0}]}
Массив scores должен идти В ТОМ ЖЕ ПОРЯДКЕ и той же длины, что и список кандидатов."""

LIVE_JUDGE_SYSTEM = """Ты — придирчивый редактор хайлайтов Twitch. Тебе дают фразы-кандидаты из live-расшифровки
и короткий контекст до и после каждой фразы.
Нужно оставить не афоризмы, а интересные, необычные, смешные, запоминающиеся, завозные цитаты.
Цельная смешная ситуация или удачная разговорная реплика уже достаточна: не требуй от каждой
фразы уровня афоризма. Для насыщенных 15–30 минут нормально оставить несколько разных моментов,
если каждый понятен и не является мусором распознавания.

Оцени каждую фразу по осям 0..100:
- celnost    — это связная человеческая фраза, не мусор STT и не обрывок;
- clip       — есть ли клиповый момент: смешно, странно, остро, запоминается;
- zavoz      — энергия, эмоция, темп, подача;
- surprise   — неожиданная формулировка, мемность, нестандартность;
- context    — понятно ли без большого внешнего контекста.

Низко оценивай:
- просто мат без мысли или шутки;
- бытовые команды и технические фразы;
- команды вида «врубай/вырубай + имя/тема», если фраза держится только на упоминании;
- фразы, где непонятно, о ком/о чём речь;
- бессвязные наборы слов после плохого распознавания.

Высоко оценивай цельные абсурдные образы, неожиданные обещания/сравнения и игровые наблюдения
с понятным поворотом. Странный набор слов сам по себе не является хорошей цитатой.

Ориентиры по стилю:
- «Я пришёл отдыхать и теперь устал от отдыха» — keep: короткий и понятный контраст;
- «После каждого промаха буду комментировать матч шёпотом» — keep: цельное необычное обещание;
- «Почему будильник знает о моих планах больше, чем я?» — keep: самостоятельный вопрос с поворотом;
- «Так, секунду, сейчас открою настройки» — drop: техническая реплика без самостоятельного смысла;
- «Включите музыку, давайте» — drop: бытовая команда;
- «Он это, ну, короче, там» — drop: незаконченный обрывок.

Предпочитай короткие цельные реплики, в которых есть конкретный образ, действие, обещание или
неожиданный поворот. Слова «каждый», «почему», мат и высокая эмоциональность сами по себе не
делают фразу цитатой. Если смысл не завершён, ставь drop даже при яркой подаче.

Контекст нужен только для понимания смысла и ошибок распознавания. Не включай соседние реплики
в цитату без причины и не повышай оценку лишь потому, что весь разговор интересный. Но если
соседняя фраза завершает мысль, создаёт контраст или добавляет панч, можешь вернуть в поле quote
более полный НЕПРЕРЫВНЫЙ фрагмент из контекста. Используй только точные слова контекста подряд:
не пересказывай, не меняй порядок и не соединяй удалённые куски. Если расширение не нужно,
поле quote оставь пустым.

Если из контекста однозначно видно, что STT перепутал или пропустил 1-3 слова, верни аккуратный
вариант в поле fix. Сохрани формулировку и мат автора. Не придумывай новый смысл, не делай фразу
литературнее и не переписывай её целиком; если уверенности нет, fix должен быть пустой строкой.

Ответ — строго JSON:
{"scores":[{"i":0,"celnost":0,"clip":0,"zavoz":0,"surprise":0,"context":0,"verdict":"keep/drop","reason":"2-5 слов","quote":"","fix":""}]}
Массив scores должен идти В ТОМ ЖЕ ПОРЯДКЕ и той же длины, что и список кандидатов."""


def _chat(
    system: str,
    user: str,
    temperature: float = 0.2,
    num_ctx: int = 8192,
    num_predict: int = 2048,
    timeout: int | None = None,
) -> str:
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "format": "json",
            "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=timeout or OLLAMA_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def _loads(raw: str) -> dict:
    """Терпимый разбор JSON: если модель обернула в мусор — вытаскиваем первый {...}."""
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


def extract_candidates(window_text: str) -> list[dict]:
    raw = _chat(EXTRACT_SYSTEM, f"Фрагмент расшифровки:\n\"\"\"\n{window_text}\n\"\"\"")
    data = _loads(raw)
    out = []
    for q in data.get("quotes", []):
        text = (q.get("quote") or "").strip()
        if text:
            out.append(
                {
                    "text": text,
                    "emotion": (q.get("emotion") or "").strip().lower(),
                    "reason": (q.get("reason") or "").strip(),
                }
            )
    return out


def judge_candidates(texts: list[str]) -> list[dict]:
    """Возвращает список dict с осями оценок, выровненный по индексам входа."""
    if not texts:
        return []
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(texts))
    raw = _chat(JUDGE_SYSTEM, f"Кандидаты:\n{listing}", temperature=0.0)
    data = _loads(raw)
    axes = ("obraznost", "samodost", "emotsiya", "originalnost", "citiruemost")
    result = [{a: 0 for a in axes} for _ in texts]
    for s in data.get("scores", []):
        i = s.get("i")
        if isinstance(i, int) and 0 <= i < len(texts):
            for a in axes:
                try:
                    result[i][a] = max(0, min(100, float(s.get(a, 0))))
                except (TypeError, ValueError):
                    result[i][a] = 0
    return result


def judge_live_candidates(texts: list[str], contexts: list[str] | None = None) -> list[dict]:
    """Оценивает live-кандидаты именно как клиповые/завозные цитаты."""
    if not texts:
        return []
    contexts = contexts or []
    rows = []
    for i, text in enumerate(texts):
        context = contexts[i].strip() if i < len(contexts) and contexts[i] else ""
        rows.append(f"{i}. Цитата: {text}")
        if context and context != text:
            rows.append(f"   Контекст: {context}")
    listing = "\n".join(rows)
    raw = _chat(
        LIVE_JUDGE_SYSTEM,
        f"Кандидаты:\n{listing}",
        temperature=0.0,
        num_ctx=4096,
        num_predict=640,
        timeout=min(OLLAMA_TIMEOUT, 180),
    )
    data = _loads(raw)
    axes = ("celnost", "clip", "zavoz", "surprise", "context")
    result = [
        {a: 0 for a in axes} | {"verdict": "drop", "reason": "", "quote": "", "fix": ""}
        for _ in texts
    ]
    parsed = 0
    for s in data.get("scores", []):
        i = s.get("i")
        if isinstance(i, int) and 0 <= i < len(texts):
            parsed += 1
            for a in axes:
                try:
                    result[i][a] = max(0, min(100, float(s.get(a, 0))))
                except (TypeError, ValueError):
                    result[i][a] = 0
            verdict = str(s.get("verdict") or "").strip().lower()
            result[i]["verdict"] = "keep" if verdict == "keep" else "drop"
            result[i]["reason"] = str(s.get("reason") or "").strip()
            result[i]["quote"] = str(s.get("quote") or "").strip()
            result[i]["fix"] = str(s.get("fix") or "").strip()
    if parsed == 0:
        raise RuntimeError("LLM вернула ответ без распознаваемых оценок")
    return result


def ping() -> tuple[bool, str]:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        names = [m["name"] for m in r.json().get("models", [])]
        ok = any(OLLAMA_MODEL.split(":")[0] in n for n in names)
        return ok, f"модели: {', '.join(names) or '—'}"
    except Exception as e:
        return False, str(e)
