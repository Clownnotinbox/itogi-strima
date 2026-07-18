"""Minimal Twitch IRC helpers for one disclosure message and chat reaction signals."""
from __future__ import annotations

import random
import re
import socket
import threading
import time

from config import TWITCH_CHAT_LOGIN, TWITCH_CHAT_NOTICE, TWITCH_CHAT_OAUTH_TOKEN


def configured() -> bool:
    return bool(TWITCH_CHAT_LOGIN and TWITCH_CHAT_OAUTH_TOKEN)


def send_notice(channel_login: str, message: str | None = None) -> tuple[bool, str]:
    if not configured():
        return False, "Twitch chat token is not configured"

    login = TWITCH_CHAT_LOGIN.lstrip("@").lower()
    token = TWITCH_CHAT_OAUTH_TOKEN
    if not token.startswith("oauth:"):
        token = "oauth:" + token
    channel = "#" + channel_login.lstrip("@").lower()
    text = (message or TWITCH_CHAT_NOTICE).strip()
    if not text:
        return False, "Twitch chat message is empty"

    with socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=15) as sock:
        sock.settimeout(5)
        f = sock.makefile("rwb", buffering=0)
        f.write(f"PASS {token}\r\n".encode("utf-8"))
        f.write(f"NICK {login}\r\n".encode("utf-8"))
        f.write(f"JOIN {channel}\r\n".encode("utf-8"))

        deadline_errors = []
        while True:
            try:
                line = f.readline().decode("utf-8", "replace")
            except socket.timeout:
                break
            if not line:
                break
            low = line.lower()
            if low.startswith("ping "):
                f.write(("PONG " + line.split(" ", 1)[1]).encode("utf-8"))
            if "login authentication failed" in low or "improperly formatted auth" in low:
                return False, "Twitch отклонил chat OAuth-токен"
            if "invalid nick" in low:
                return False, "Twitch отклонил chat login"
            if f"join {channel}" in low or " 366 " in low:
                break
            if "notice" in low:
                deadline_errors.append(line.strip())

        f.write(f"PRIVMSG {channel} :{text}\r\n".encode("utf-8"))
        f.write(f"PART {channel}\r\n".encode("utf-8"))

    return True, "notice sent"


_LAUGH_RE = re.compile(r"((?:ах|ха){3,}|(?:ah|ha){3,}|(?:хд|xd)+)", re.IGNORECASE)
_REACTION_RE = re.compile(
    r"((?:ах|ха){3,}|(?:ah|ha){3,}|(?:хд|xd)+|ору|лол|лул|жесть|clip|клип|kekw|кекв|lul|omegalul|pog|wtf|рофл|😂|🤣|!{2,})",
    re.IGNORECASE,
)


class ChatReactionTracker:
    """Collect lightweight chat excitement signals mapped to stream seconds."""

    def __init__(self, channel_login: str, stream_time_at_start: float = 0.0):
        self.channel_login = channel_login.lstrip("@").lower()
        self.stream_time_at_start = float(stream_time_at_start or 0.0)
        self.local_started_at = time.time()
        self._items: list[tuple[float, float]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def score(self, t_start, t_end, pad: float = 30.0) -> float:
        if t_start is None:
            return 0.0
        start = float(t_start) - pad
        end = float(t_end if t_end is not None else t_start) + pad
        with self._lock:
            total = sum(weight for sec, weight in self._items if start <= sec <= end)
        return max(0.0, min(1.0, total / 18.0))

    def _stream_second_now(self) -> float:
        return self.stream_time_at_start + max(0.0, time.time() - self.local_started_at)

    def _remember(self, message: str):
        reaction = bool(_REACTION_RE.search(message))
        laugh = bool(_LAUGH_RE.search(message))
        letters = [c for c in message if c.isalpha()]
        caps_ratio = sum(1 for c in letters if c.isupper()) / max(1, len(letters))
        weight = 2.4 if reaction else 1.0
        if laugh:
            weight += 0.8
        if reaction and caps_ratio >= 0.55:
            weight += 0.7
        if laugh and len(message) >= 12:
            weight += 0.5
        if len(message) <= 6:
            weight *= 0.75
        with self._lock:
            self._items.append((self._stream_second_now(), weight))
            cutoff = self._stream_second_now() - 8 * 3600
            if len(self._items) > 5000:
                self._items = [(sec, w) for sec, w in self._items if sec >= cutoff]

    def _run(self):
        channel = "#" + self.channel_login
        nick = f"justinfan{random.randint(10000, 99999)}"
        while not self._stop.is_set():
            try:
                with socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=20) as sock:
                    sock.settimeout(2)
                    f = sock.makefile("rwb", buffering=0)
                    f.write(f"NICK {nick}\r\n".encode("utf-8"))
                    f.write(f"JOIN {channel}\r\n".encode("utf-8"))
                    while not self._stop.is_set():
                        try:
                            raw = f.readline()
                        except socket.timeout:
                            continue
                        if not raw:
                            break
                        line = raw.decode("utf-8", "replace")
                        low = line.lower()
                        if low.startswith("ping "):
                            f.write(("PONG " + line.split(" ", 1)[1]).encode("utf-8"))
                            continue
                        if " privmsg " not in low:
                            continue
                        msg = line.split(" PRIVMSG ", 1)[-1].split(" :", 1)[-1].strip()
                        if msg:
                            self._remember(msg)
            except Exception:
                if self._stop.wait(10):
                    break
