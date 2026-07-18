"""Small Twitch URL helpers shared by the API and pipeline."""
import re
from urllib.parse import unquote, urlparse


_TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
_RESERVED = {
    "activate",
    "bits",
    "channel",
    "checkout",
    "communities",
    "directory",
    "downloads",
    "drops",
    "inventory",
    "jobs",
    "login",
    "logout",
    "moderator",
    "p",
    "popout",
    "prime",
    "products",
    "settings",
    "store",
    "subscriptions",
    "team",
    "turbo",
    "videos",
    "wallet",
}


def twitch_channel_login(url: str) -> str | None:
    """Return the login for plain Twitch channel URLs, otherwise None."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = parsed.netloc.lower()
    if host not in _TWITCH_HOSTS:
        return None
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if len(parts) != 1:
        return None
    login = parts[0].strip()
    if login.lower() in _RESERVED:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_]{3,25}", login):
        return None
    return login


def twitch_channel_url(login: str) -> str:
    return f"https://www.twitch.tv/{login}"
