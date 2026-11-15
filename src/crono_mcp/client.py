"""Read-only Cronometer client.

Cronometer has no public API for individuals. This client uses calls the
web app itself makes:

  1. GWT-RPC ``authenticate``               -> your numeric user id
  2. GWT-RPC ``generateAuthorizationToken`` -> short-lived export token
  3. ``GET /export?generate=<type>``        -> CSV ("Export Data")
  4. GWT-RPC ``getServings``                -> diary entries for a date range
  5. GWT-RPC ``getFood``                    -> one food's nutrient profile
  6. GWT-RPC ``getTotalNutrients``          -> a day's official nutrient totals

READ-ONLY BY CONSTRUCTION: the only POST this module can send is one of the
fixed RPC bodies in ``_RPC_BODIES``, all of which are reads; everything else
is a GET. RPC parameters are integers only, so a body cannot be altered into a
different call. There is no code path that can modify your diary.

Protocol details adapted from cphoskins/cronometer-mcp (MIT, see
THIRD_PARTY_LICENSE_cronometer-mcp).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime

import httpx

from crono_mcp import auth, gwt
from crono_mcp.exportlimit import ExportLimit

logger = logging.getLogger(__name__)
# httpx logs full URLs at INFO, which would include short-lived export tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE = "https://cronometer.com"
GWT_APP_URL = f"{BASE}/cronometer/app"
GWT_MODULE_BASE = f"{BASE}/cronometer/"
GWT_NOCACHE_JS_URL = f"{BASE}/cronometer/cronometer.nocache.js"
GWT_CACHE_JS_URL = f"{BASE}/cronometer/{{permutation}}.cache.js"
EXPORT_URL = f"{BASE}/export"

# Fallbacks only; discovered fresh from Cronometer's JS on first use.
FALLBACK_PERMUTATION = "0EFF06F8710774B34D04BFA3409289E9"
FALLBACK_GWT_HEADER = "B82D6F42004DB8749BB1C2080E1A4479"

EXPORT_TYPES = {
    "servings": "servings",
    "daily_summary": "dailySummary",
    "exercises": "exercises",
    "biometrics": "biometrics",
    "notes": "notes",
}

_SVC = "https://cronometer.com/cronometer/|{gwt_header}|com.cronometer.shared.rpc.CronometerService|"

# GWT type signatures ("<class>/<hash>") used in request bodies. The hash changes when Cronometer
# changes a class, so current values are read from the app's JavaScript on startup (see
# _discover_hashes); these are only fallbacks, valid as of Nov 2026.
FALLBACK_SIGNATURES = {
    "t_string": "java.lang.String/2004016611",
    "t_integer": "java.lang.Integer/3438268394",
    "t_authscope": "com.cronometer.shared.user.AuthScope/2065601159",
    "t_day": "com.cronometer.shared.entries.models.Day/782579793",
    "t_time": "com.cronometer.shared.entries.models.Time/1552252503",
    "t_dayquery": "com.cronometer.shared.entries.DayQueryType/1802942352",
}

# The ONLY RPC calls this client may make. All are reads.
_RPC_BODIES = {
    "authenticate": (
        "7|0|5|" + _SVC + "authenticate|{t_integer}|"
        "1|2|3|4|1|5|5|{tz_offset}|"
    ),
    "generateAuthorizationToken": (
        "7|0|8|" + _SVC + "generateAuthorizationToken|{t_string}|I|{t_authscope}|"
        "{nonce}|1|2|3|4|4|5|6|6|7|8|{user_id}|3600|7|2|"
    ),
    # getServings(nonce, userId, startDay, endDay, Time=null, Time=null)
    "getServings": (
        "7|0|9|" + _SVC + "getServings|{t_string}|I|{t_day}|{t_time}|"
        "{nonce}|1|2|3|4|6|5|6|7|7|8|8|9|{user_id}|"
        "7|{d0}|{m0}|{y0}|7|{d1}|{m1}|{y1}|0|0|"
    ),
    # getFood(nonce, foodId)
    "getFood": (
        "7|0|7|" + _SVC + "getFood|{t_string}|I|"
        "{nonce}|1|2|3|4|2|5|6|7|{food_id}|"
    ),
    # getTotalNutrients(nonce, userId, startDay, endDay, DayQueryType=0 (all days),
    #                   includeEverything=true, sevenDayWindow=false)
    "getTotalNutrients": (
        "7|0|10|" + _SVC + "getTotalNutrients|{t_string}|I|{t_day}|{t_dayquery}|Z|"
        "{nonce}|1|2|3|4|7|5|6|7|7|8|9|9|10|{user_id}|"
        "7|{d0}|{m0}|{y0}|7|{d1}|{m1}|{y1}|8|0|1|0|"
    ),
}

TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class AuthError(RuntimeError):
    """Session missing/expired. Fix: log into cronometer.com in Chrome, then refresh auth."""


class CronometerError(RuntimeError):
    pass


class _OutOfDate(Exception):
    """Cronometer rejected a request built from stale build ids or type signatures."""


def discover_signatures(js: str) -> dict[str, str]:
    """Current "<class>/<hash>" type signatures from the app's JavaScript, with fallbacks."""
    found = dict(FALLBACK_SIGNATURES)
    for key, sig in FALLBACK_SIGNATURES.items():
        cls = sig.rsplit("/", 1)[0]
        m = re.search(r"'(" + re.escape(cls) + r"/\d+)'", js)
        if m:
            found[key] = m.group(1)
        else:
            logger.warning("Type signature for %s not found; using fallback", cls)
    return found


class ExportLimitError(CronometerError):
    """Cronometer's 10-exports-per-day limit is reached; the export was not attempted."""


def _local_tz_offset_minutes() -> int:
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() // 60) if off else 0


class CronometerClient:
    def __init__(self, cookies: dict[str, str] | None = None, *, persist: bool = True,
                 export_limit: ExportLimit | None = None):
        self._persist = persist
        self._export_limit = export_limit or ExportLimit(persist=persist)
        self._cookies: dict[str, str] | None = cookies
        self._http = httpx.Client(timeout=TIMEOUT, follow_redirects=False,
                                  headers={"User-Agent": "crono-mcp-readonly/0.1"})
        self._permutation = FALLBACK_PERMUTATION
        self._gwt_header = FALLBACK_GWT_HEADER
        self._hashes_discovered = False
        self._signatures = dict(FALLBACK_SIGNATURES)
        self._user_id: str | None = None

    # ---------- cookies ----------
    def _ensure_cookies(self) -> dict[str, str]:
        if self._cookies is None:
            self._cookies = auth.load()
        if self._cookies is None:
            # Deliberately NOT reading Chrome here: each Chrome read can trigger a
            # macOS "Chrome Safe Storage" prompt. Only explicit auth/refresh does that.
            raise AuthError("No stored Cronometer session. Run `crono-mcp auth` (or the refresh tool) once.")
        return self._cookies

    def reload_from_browser(self, browser: str = "chrome") -> str:
        res = auth.extract_from_browser(browser)
        if not res.success:
            raise AuthError(res.message)
        self._cookies = res.cookies
        self._user_id = None
        if self._persist:
            auth.store(self._cookies)
        return res.message

    def _absorb_set_cookies(self, resp: httpx.Response) -> None:
        """Keep up with session-cookie rotation the server does on our requests."""
        changed = False
        for name, value in resp.cookies.items():
            if value and self._cookies is not None and self._cookies.get(name) != value:
                self._cookies[name] = value
                changed = True
        self._http.cookies.clear()  # we manage cookies ourselves
        if changed and self._persist and self._cookies:
            auth.store(self._cookies)

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._ensure_cookies().items())

    # ---------- GWT plumbing ----------
    def _discover_hashes(self) -> None:
        if self._hashes_discovered:
            return
        self._hashes_discovered = True
        try:
            r = self._http.get(GWT_NOCACHE_JS_URL)
            r.raise_for_status()
            m = re.search(r"='([A-F0-9]{32})'", r.text)
            if not m:
                logger.warning("GWT permutation not found; using fallback")
                return
            perm = m.group(1)
            r = self._http.get(GWT_CACHE_JS_URL.format(permutation=perm))
            r.raise_for_status()
            self._permutation = perm
            h = re.search(r"'app','([A-F0-9]{32})'", r.text)
            if h:
                self._gwt_header = h.group(1)
            else:
                logger.warning("GWT header not found; using fallback")
            self._signatures = discover_signatures(r.text)
        except httpx.HTTPError as e:
            logger.warning("GWT hash discovery failed (%s); using fallbacks", type(e).__name__)

    def _rpc(self, method: str, **params: int) -> str:
        try:
            return self._rpc_once(method, **params)
        except _OutOfDate:
            # Cronometer deployed since we read its JavaScript: rediscover once and retry.
            self._hashes_discovered = False
            try:
                return self._rpc_once(method, **params)
            except _OutOfDate as e:
                raise CronometerError(f"Cronometer RPC {method} rejected as out of date: {e}") from e

    def _rpc_once(self, method: str, **params: int) -> str:
        if method not in _RPC_BODIES:  # belt and braces: no other RPCs, ever
            raise CronometerError(f"RPC {method!r} is not permitted (read-only client)")
        if not all(type(v) is int for v in params.values()):
            raise CronometerError("RPC parameters must be integers")
        self._discover_hashes()
        cookies = self._ensure_cookies()
        body = _RPC_BODIES[method].format(
            gwt_header=self._gwt_header,
            tz_offset=_local_tz_offset_minutes(),
            nonce=cookies.get(auth.SESSION_COOKIE, ""),
            user_id=self._user_id or "",
            **self._signatures,
            **params,
        )
        try:
            r = self._http.post(
                GWT_APP_URL,
                content=body.encode(),
                headers={
                    "content-type": "text/x-gwt-rpc; charset=UTF-8",
                    "x-gwt-module-base": GWT_MODULE_BASE,
                    "x-gwt-permutation": self._permutation,
                    "cookie": self._cookie_header(),
                },
            )
        except httpx.HTTPError as e:
            raise CronometerError(f"Cronometer RPC {method} failed ({type(e).__name__})") from e
        self._absorb_set_cookies(r)
        text = r.text
        if "NotLoggedInException" in text or r.status_code in (401, 403):
            raise AuthError("Cronometer session expired or invalid.")
        if "IncompatibleRemoteServiceException" in text:
            raise _OutOfDate(text[:200])
        if r.status_code >= 400 or not text.startswith("//OK"):
            # Response may include the IncompatibleRemoteService text that indicates a Cronometer deploy.
            raise CronometerError(f"Cronometer RPC {method} failed (HTTP {r.status_code}): {text[:200]}")
        return text

    def _user(self) -> str:
        if self._user_id is None:
            m = re.search(r"OK\[(\d+),", self._rpc("authenticate"))
            if not m:
                raise CronometerError("Could not read user id from Cronometer authenticate response")
            self._user_id = m.group(1)
        return self._user_id

    def _export_token(self) -> str:
        self._user()
        text = self._rpc("generateAuthorizationToken")
        m = re.search(r'"([^"]+)"', text)
        if not m:
            raise CronometerError("Could not read export token from Cronometer response")
        return m.group(1)

    # ---------- public API ----------
    def check(self) -> str:
        """Validate the session; returns the Cronometer user id."""
        return self._user()

    def export_csv(self, export_type: str, start: date, end: date) -> str:
        if export_type not in EXPORT_TYPES:
            raise ValueError(f"export_type must be one of {sorted(EXPORT_TYPES)}")
        if end < start:
            raise ValueError("end date is before start date")
        blocked = self._export_limit.blocked_reason()
        if blocked:
            raise ExportLimitError(blocked)
        token = self._export_token()
        try:
            r = self._http.get(
                EXPORT_URL,
                params={
                    "nonce": token,
                    "generate": EXPORT_TYPES[export_type],
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                headers={"cookie": self._cookie_header()},
            )
        except httpx.HTTPError as e:
            raise CronometerError(f"Cronometer export failed ({type(e).__name__})") from e
        self._absorb_set_cookies(r)
        if r.status_code in (401, 403):
            raise AuthError("Cronometer rejected the export request (session expired?).")
        if r.status_code == 429:
            self._export_limit.record_rate_limited()
            raise ExportLimitError("Cronometer's daily export limit (10/day) was reached; "
                                   "exports paused for 24 hours.")
        if r.status_code >= 400:
            raise CronometerError(f"Cronometer export failed (HTTP {r.status_code})")
        text = r.text
        if text.lstrip().lower().startswith(("<!doctype", "<html")):
            raise AuthError("Cronometer returned a web page instead of CSV (session expired?).")
        self._export_limit.record_success()
        return text

    def export_budget(self) -> dict:
        """Remaining exports under Cronometer's 10/day limit (no network)."""
        return self._export_limit.status()

    def export_rows(self, export_type: str, start: date, end: date) -> list[dict[str, str]]:
        return parse_csv(self.export_csv(export_type, start, end))

    # ---------- diary RPCs (read-only) ----------
    @staticmethod
    def _range_params(start: date, end: date) -> dict[str, int]:
        if end < start:
            raise ValueError("end date is before start date")
        return {"d0": start.day, "m0": start.month, "y0": start.year,
                "d1": end.day, "m1": end.month, "y1": end.year}

    def servings(self, start: date, end: date) -> list[gwt.Serving]:
        self._user()
        return gwt.parse_servings(self._rpc("getServings", **self._range_params(start, end)))

    def food(self, food_id: int) -> gwt.Food:
        return gwt.parse_food(self._rpc("getFood", food_id=int(food_id)), int(food_id))

    def total_nutrients(self, day: date) -> gwt.Totals:
        self._user()
        return gwt.parse_total_nutrients(self._rpc("getTotalNutrients", **self._range_params(day, day)))

    def close(self) -> None:
        self._http.close()


def parse_csv(text: str) -> list[dict[str, str]]:
    text = text.lstrip("﻿")
    if not text.strip():
        return []
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(io.StringIO(text))]
