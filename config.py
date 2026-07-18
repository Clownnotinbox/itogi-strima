"""Центральная конфигурация. Все параметры можно переопределить через переменные окружения."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("KRYL_DATA_DIR", BASE_DIR / "data"))
AUDIO_DIR = DATA_DIR / "audio"
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
SUMMARY_DIR = DATA_DIR / "summaries"
QUOTE_EXPORT_DIR = DATA_DIR / "quote_exports"
DB_PATH = DATA_DIR / "stream_quotes.sqlite3"
CHANNEL_PATH = DATA_DIR / "channel.json"     # профиль одного стримера
WATCHES_PATH = DATA_DIR / "live_watches.json"
PUBLIC_DIR = BASE_DIR / "public"             # публичный one-pager + data.json

for d in (DATA_DIR, AUDIO_DIR, TRANSCRIPT_DIR, SUMMARY_DIR, QUOTE_EXPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- STT (faster-whisper) ---
WHISPER_MODEL = os.getenv("KRYL_WHISPER_MODEL", "large-v3")
# "auto" -> попробовать cuda, при ошибке откатиться на cpu
WHISPER_DEVICE = os.getenv("KRYL_WHISPER_DEVICE", "auto")
WHISPER_COMPUTE_CUDA = os.getenv("KRYL_WHISPER_COMPUTE_CUDA", "int8_float16")
WHISPER_COMPUTE_CPU = os.getenv("KRYL_WHISPER_COMPUTE_CPU", "int8")
LANGUAGE = os.getenv("KRYL_LANGUAGE", "ru")

# --- Ollama ---
OLLAMA_URL = os.getenv("KRYL_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("KRYL_OLLAMA_MODEL", "gemma4:12b")
OLLAMA_TIMEOUT = int(os.getenv("KRYL_OLLAMA_TIMEOUT", "600"))

# --- Логика отбора ---
TOP_N = int(os.getenv("KRYL_TOP_N", "5"))          # сколько фраз оставлять на стрим
WINDOW_SEGMENTS = int(os.getenv("KRYL_WINDOW_SEGMENTS", "8"))   # сегментов в окне для LLM
WINDOW_OVERLAP = int(os.getenv("KRYL_WINDOW_OVERLAP", "2"))     # перекрытие окон
# Доля самых «перспективных» окон, которые вообще доходят до LLM (дешёвый предотсев по
# длине/паразитам/маркерам/громкости). 1.0 = гнать всё, 0.5 = только лучшую половину.
PRESELECT_RATIO = float(os.getenv("KRYL_PRESELECT_RATIO", "0.5"))
MIN_WORDS = 3                                        # короче — почти всегда мусор
MAX_WORDS = 22                                       # длиннее — это уже не афоризм
SCORE_KEEP_THRESHOLD = float(os.getenv("KRYL_KEEP_THRESHOLD", "50"))  # проходной балл 0-100

# Скачивать только аудио и не тяжелее чем нужно
YTDLP_FORMAT = os.getenv("KRYL_YTDLP_FORMAT", "bestaudio/best")
# Для защищённого/подписочного контента: взять куки из браузера ("chrome"/"firefox"/"edge")
# или из файла cookies.txt. Пусто = без кук.
COOKIES_FROM_BROWSER = os.getenv("KRYL_COOKIES_FROM_BROWSER", "").strip()
COOKIES_FILE = os.getenv("KRYL_COOKIES_FILE", "").strip()

# --- Twitch live monitor ---
LIVE_POLL_SECONDS = int(os.getenv("KRYL_LIVE_POLL_SECONDS", "60"))
LIVE_CHUNK_SECONDS = int(os.getenv("KRYL_LIVE_CHUNK_SECONDS", "240"))
LIVE_MAX_SECONDS = int(os.getenv("KRYL_LIVE_MAX_SECONDS", "0"))  # 0 = до конца эфира
LIVE_QUOTE_LIMIT = int(os.getenv("KRYL_LIVE_QUOTE_LIMIT", "30"))
LIVE_MIN_QUOTES = int(os.getenv("KRYL_LIVE_MIN_QUOTES", "3"))
LIVE_QUOTES_PER_HOUR = float(os.getenv("KRYL_LIVE_QUOTES_PER_HOUR", "2.8"))
LIVE_DYNAMIC_KEEP_THRESHOLD = float(os.getenv("KRYL_LIVE_DYNAMIC_KEEP_THRESHOLD", "72"))
LIVE_MAX_WORDS = int(os.getenv("KRYL_LIVE_MAX_WORDS", "38"))
LIVE_USE_LLM = os.getenv("KRYL_LIVE_USE_LLM", "0").strip().lower() in ("1", "true", "yes")
LIVE_SUMMARY_SECONDS = int(os.getenv("KRYL_LIVE_SUMMARY_SECONDS", "7200"))  # 2 часа
LIVE_QUOTE_INTERVAL_SECONDS = int(os.getenv("KRYL_LIVE_QUOTE_INTERVAL_SECONDS", str(LIVE_SUMMARY_SECONDS)))
LIVE_SITE_PUBLISH_SECONDS = int(os.getenv("KRYL_LIVE_SITE_PUBLISH_SECONDS", str(LIVE_SUMMARY_SECONDS)))
LIVE_FIRST_UPDATE_DELAY_SECONDS = int(os.getenv("KRYL_LIVE_FIRST_UPDATE_DELAY_SECONDS", "0"))
LIVE_RECONNECT_GRACE_SECONDS = int(os.getenv("KRYL_LIVE_RECONNECT_GRACE_SECONDS", "900"))
LIVE_RECONNECT_DELAY_SECONDS = int(os.getenv("KRYL_LIVE_RECONNECT_DELAY_SECONDS", "20"))

# GitHub Pages / static site deployment. Local publish works even when auto-push is off.
PAGES_AUTO_PUSH = os.getenv("KRYL_PAGES_AUTO_PUSH", "0").strip().lower() in ("1", "true", "yes")
PAGES_REPO_URL = os.getenv("KRYL_PAGES_REPO_URL", "").strip()
PAGES_BRANCH = os.getenv("KRYL_PAGES_BRANCH", "main").strip() or "main"

# Twitch chat. OAuth токен должен быть вида oauth:xxxx и хранится только локально в env.
TWITCH_CHAT_LOGIN = os.getenv("KRYL_TWITCH_CHAT_LOGIN", "").strip()
TWITCH_CHAT_OAUTH_TOKEN = os.getenv("KRYL_TWITCH_CHAT_OAUTH_TOKEN", "").strip()
TWITCH_CHAT_NOTICE = os.getenv(
    "KRYL_TWITCH_CHAT_NOTICE",
    "Привет, я тут, чтобы запоминать цитаты.",
).strip()
