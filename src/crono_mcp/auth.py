"""Session cookie handling: pull from Chrome, persist in the macOS keychain.

SECURITY:
- Domain is hardcoded to cronometer.com.
- Cookie values never appear in messages, logs, or reprs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

KEYRING_SERVICE = "crono-mcp-readonly"
KEYRING_USER = "cronometer_session_cookies"
COOKIE_DOMAIN = "cronometer.com"
SESSION_COOKIE = "sesnonce"
SUPPORTED_BROWSERS = ["chrome", "brave", "edge", "chromium", "firefox", "safari"]


@dataclass
class CookieResult:
    success: bool
    message: str
    cookies: dict[str, str] | None = field(default=None, repr=False)
    browser: str | None = None

    def __repr__(self) -> str:
        n = len(self.cookies) if self.cookies else 0
        return f"CookieResult(success={self.success}, cookies=<{n}>, message={self.message!r}, browser={self.browser!r})"


def extract_from_browser(browser: str | None = "chrome") -> CookieResult:
    """Read cronometer.com cookies from a local browser profile."""
    import browser_cookie3

    names = [browser.lower()] if browser else SUPPORTED_BROWSERS
    errors = []
    for name in names:
        func = getattr(browser_cookie3, name, None)
        if func is None or name not in SUPPORTED_BROWSERS:
            return CookieResult(False, f"Unknown browser {name!r}. Supported: {', '.join(SUPPORTED_BROWSERS)}")
        try:
            jar = func(domain_name=COOKIE_DOMAIN)
            cookies = {
                c.name: c.value
                for c in jar
                if c.value and (c.domain or "").lstrip(".").endswith(COOKIE_DOMAIN)
            }
        except Exception as e:  # never echo the exception text; could contain data
            errors.append(f"{name}: {type(e).__name__}")
            continue
        if cookies.get(SESSION_COOKIE):
            return CookieResult(True, f"Found Cronometer session in {name}", cookies, name)
        errors.append(f"{name}: no Cronometer session (log in at cronometer.com first)")
    return CookieResult(False, "Could not find a Cronometer session. " + "; ".join(errors))


def store(cookies: dict[str, str]) -> None:
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, json.dumps(cookies))


def load() -> dict[str, str] | None:
    try:
        raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except KeyringError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get(SESSION_COOKIE) else None


def clear() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except (PasswordDeleteError, KeyringError):
        pass
