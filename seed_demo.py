"""Заполнить витрину демо-стримом, чтобы посмотреть дизайн без реального прогона.
Запуск:  py -3.11 seed_demo.py       Удалить демо:  py -3.11 seed_demo.py --clean
"""
import sys
import time

import db

DEMO = [
    {
        "url": "https://www.youtube.com/watch?v=DEMO1",
        "title": "[ДЕМО] Ночной стрим: разговоры за жизнь",
        "source": "Youtube", "duration": 7420,
        "quotes": [
            {"text": "Смелость — это когда страшно, но ты всё равно нажимаешь кнопку", "t_start": 812,
             "score": 91.4, "emotion": "азарт", "reason": "парадокс, ядро — обобщение",
             "tags": ["обобщение", "на эмоциях"]},
            {"text": "Мы не тонем в проблемах, мы тонем в мыслях о них", "t_start": 2044,
             "score": 88.7, "emotion": "спокойствие", "reason": "метафора, самодостаточно",
             "tags": ["обобщение", "лаконично"]},
            {"text": "Планы — это способ рассмешить будущее", "t_start": 3387,
             "score": 86.2, "emotion": "ирония", "reason": "афоризм, игра слов",
             "tags": ["лаконично"]},
            {"text": "Каждый хочет перемен, но никто не хочет меняться", "t_start": 4901,
             "score": 84.9, "emotion": "грусть", "reason": "контраст, обобщение",
             "tags": ["обобщение"]},
            {"text": "Опыт — это то, что получаешь, когда не получил, что хотел", "t_start": 6210,
             "score": 82.5, "emotion": "ирония", "reason": "неожиданное определение",
             "tags": ["обобщение"]},
        ],
    },
    {
        "url": "https://www.twitch.tv/videos/DEMO2",
        "title": "[ДЕМО] Катка до утра и философия",
        "source": "Twitch", "duration": 12960,
        "quotes": [
            {"text": "Проигрывает не тот, кто упал, а тот, кто перестал вставать", "t_start": 1533,
             "score": 90.1, "emotion": "азарт", "reason": "мотив, обобщение",
             "tags": ["обобщение", "на эмоциях"]},
            {"text": "Тишина — это тоже ответ, просто очень громкий", "t_start": 4082,
             "score": 87.3, "emotion": "грусть", "reason": "парадокс, образность",
             "tags": ["лаконично"]},
            {"text": "Мечта без дедлайна — это просто красивая отмазка", "t_start": 6644,
             "score": 85.6, "emotion": "ирония", "reason": "меткая формулировка",
             "tags": ["обобщение"]},
            {"text": "Мы всё ищем свет, забыв, что сами включаемся изнутри", "t_start": 8710,
             "score": 83.8, "emotion": "спокойствие", "reason": "метафора",
             "tags": ["обобщение"]},
            {"text": "Хейт — это налог на то, что тебя вообще заметили", "t_start": 11220,
             "score": 81.9, "emotion": "радость", "reason": "неожиданный ракурс",
             "tags": ["лаконично"]},
        ],
    },
]


def clean():
    for s in db.list_streams():
        if (s.get("title") or "").startswith("[ДЕМО]"):
            db.delete_stream(s["id"])
    print("демо удалено")


def seed():
    db.init_db()
    for s in db.list_streams():
        if (s.get("title") or "").startswith("[ДЕМО]"):
            print("демо уже есть")
            return
    for stream in DEMO:
        sid = db.add_stream(stream["url"])
        db.update_stream(sid, title=stream["title"], source=stream["source"],
                         duration=stream["duration"], status="done", stage_msg="готово")
        quotes = []
        for q in stream["quotes"]:
            q = dict(q); q["t_end"] = q["t_start"] + 4
            quotes.append(q)
        db.save_quotes(sid, quotes)
    print(f"демо добавлено: {len(DEMO)} эфира")


if __name__ == "__main__":
    db.init_db()
    if "--clean" in sys.argv:
        clean()
    else:
        seed()
