"""Cursor account usage reader and diagnostic adapter (Track 107A).

Counts Cursor requests that never touch a local store (ACP front-ends such as
Paseo) from the owner's Cursor account: daily per-model token totals from the
dashboard's billing-items export, read with the session Cursor already holds.
Login sources follow steipete/CodexBar's Cursor provider (MIT), which reads the
same session in production.

It is opt-in per machine (``[retro] cursor_account_usage``), runs only on
attended pushes and ``mm recapture``, and never writes or refreshes anything
on Cursor's side. Login sources, first match wins (all read-only, in memory only):

- Cursor.app's ``state.vscdb`` (SQLite, opened read-only), ``ItemTable`` key
  ``cursorAuth/accessToken``, when it exists (macOS first; on Linux after the
  agent file). A plain JWT needs no Keychain access. A value prefixed
  ``cursor-safe-storage:`` is encrypted; mm does not decrypt it and records
  ``login_encrypted``.
- Linux: cursor-agent's ``$XDG_CONFIG_HOME/cursor/auth.json`` (default
  ``~/.config/cursor/auth.json``), field ``accessToken``.
- macOS: cursor-agent's own login, kept in the Keychain as a generic password
  (service ``<prefix>-access-token``, account ``<prefix>-user``) and read with
  ``/usr/bin/security find-generic-password ... -w`` under a timeout. A Keychain
  prompt may appear once.

The JWT is used only while its ``exp`` is more than 60 s away (``login_expired``
otherwise) and is never refreshed. Auth is the cookie
``WorkosCursorSessionToken=<userId>%3A%3A<jwt>``; the user id is the last
``|``-separated part of the JWT ``sub`` claim, and the account key is its hash.

One endpoint: ``GET /api/dashboard/v2/export-billing-items-csv?scope=user&startTime=..&endTime=..``
(ISO 8601 UTC), a read-only CSV with one row per billed model call. The per-request
events endpoint stops at the account's move to the unified plan (2026-10-21 on the
owner's account) while this export continues, and the two agree exactly on every
day both cover (CONTRACT.md). The export is slow and erratic (4 s to 28 s for one day,
66 to 155 s for 30), so finished UTC days are cached in ``cursor-account-usage.json``
and each read fetches only missing days, newest first, 3 per request. A read publishes the
contiguous run of cached days ending at the newest one (``since`` .. ``through``); the
fleet rule replaces local usage only for those days, so a partial fill is safe.

The login never reaches config, caches, events, logs, diagnostics or fixtures:
``_read_login`` returns it, ``_session_cookie`` and ``_fetch`` consume it, and
nothing stores it. The cache holds reader state plus per-day per-model token
counts (never the login, URLs, response bodies or e-mail addresses).
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from mind_meld import host_usage
from mind_meld.lockedjson import LockContended, locked_json_rmw, locked_json_snapshot
from mind_meld.token_usage import TOKEN_FIELDS

CACHE_PATH = Path.home() / ".config" / "mind-meld" / "cursor-account-usage.json"
"""Reader state and per-day per-model token counts. Never the login, URLs or bodies."""

LOOKBACK_DAYS = 30
"""Finished UTC days of account history one read covers."""
CHUNK_DAYS = 3
"""Days per request. The export's latency is mostly fixed per request, not per row."""
SETTLE_HOURS = 1
"""A UTC day is final this long after its end, so no partial day is cached just after midnight."""

BILLING_URL = "https://cursor.com/api/dashboard/v2/export-billing-items-csv"
ALLOWED_REQUESTS: frozenset[tuple[str, str]] = frozenset({("GET", BILLING_URL)})
"""The only ``(method, URL)`` pair this module may request (query string aside), read-only."""
ALLOWED_URLS: dict[str, str] = {"billing": BILLING_URL}
ORIGIN = "https://cursor.com"
PRODUCT = "cursor"
"""Rows of other products (``grok-chat``) are not Cursor usage and are ignored."""

MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_MODEL_ID_BYTES = 128
MAX_DIAG_MODELS = 64
MAX_CACHED_DAYS = 400
_MAX_COUNTER = 10**13
_EXPIRY_MARGIN_S = 60
_MAX_STORE_BYTES = 1024 * 1024
_APP_KEY = "cursorAuth/accessToken"
KEYCHAIN_PREFIX = "cursor"
"""cursor-agent derives its Keychain names from one prefix: account ``<prefix>-user``,
service ``<prefix>-access-token``. Confirmed live 2026-11-04 (cursor-agent 2026.10.22):
the item exists under ``cursor`` and ``security`` returns a JWT-shaped value."""
_SECURITY = "/usr/bin/security"
_PROMPT_TIMEOUT_S = 120.0
"""The bound for ``mm enable-source``'s one login read, so a Keychain prompt can be answered."""
_ENCRYPTED_PREFIX = "cursor-safe-storage:"

_COLUMNS = ("Billing Item ID", "Event Time", "Product", "Usage", "Model")
_USAGE_KEYS: dict[str, str] = {
    "prompt-text-tokens": "input",
    "cache-write-text-tokens": "cache_create",
    "cached-prompt-text-tokens": "cache_read",
    "completion-text-tokens": "output",
}
"""``Usage`` JSON key per ``TOKEN_FIELDS`` name. Disjoint: ``cached-prompt-text-tokens``
exceeds ``prompt-text-tokens`` on most rows, and the daily sums equal the per-event
``inputTokens`` (uncached), ``cacheReadTokens`` and ``outputTokens`` exactly (CONTRACT.md),
so no inclusive normalization applies."""

_KNOWN_NAMES = frozenset({*_COLUMNS, "startTime", *_USAGE_KEYS})
"""Names a failure may carry as its ``field``: ours, never response-supplied."""


@dataclass(frozen=True)
class Cause:
    reason: host_usage.Reason
    broke: str
    fix: str
    """The one fixing action, without a trailing period."""


def _macos() -> bool:
    return sys.platform == "darwin"


def _sign_in_fix() -> str:
    return "Run cursor-agent login"


CAUSES: dict[str, Cause] = {
    "no_login": Cause("io_error", "Cursor has no signed-in session on this machine", ""),
    "login_encrypted": Cause(
        "io_error",
        "Cursor.app stores its login encrypted, which mm does not decrypt",
        "Run mm disable-source cursor-account",
    ),
    "login_expired": Cause("io_error", "the Cursor login has expired", ""),
    "login_refused": Cause("io_error", "cursor.com refused the Cursor login", ""),
    "store_unreadable": Cause(
        "io_error", "Cursor's login store could not be read", "Run mm enable-source cursor-account"
    ),
    "http_error": Cause("io_error", "cursor.com returned an error", "Run mm push"),
    "network": Cause("io_error", "cursor.com could not be reached", "Run mm push"),
    "missing_key": Cause(
        "unsupported", "cursor.com's usage response lost a field mm reads", "Run mm update"
    ),
    "changed_field": Cause(
        "unsupported", "cursor.com's usage events changed a field mm reads", "Run mm update"
    ),
    "deadline": Cause("deadline", "the account read ran out of time", "Run mm push"),
}
"""Closed vocabulary of failure causes: persisted reason, what broke, the fix.

An empty ``fix`` is platform-dependent and resolved by ``fix_for``. ``host_usage.Reason``
stays unchanged; ``last_reason_detail.cause`` refines it. ``deadline`` keeps the
shared deadline remedy; it is listed for completeness."""


def fix_for(cause: str) -> str:
    """The single fixing action for ``cause``, without a trailing period."""
    entry = CAUSES[cause]
    return entry.fix or _sign_in_fix()


class AccountFailure(Exception):
    """One failed account read. Messages are fixed text; they never carry the
    login, a URL, a header or response content."""

    def __init__(self, cause: str, *, field: str | None = None, status: int | None = None) -> None:
        super().__init__(cause)
        self.cause = cause
        self.field = field if field in _KNOWN_NAMES else None
        self.status = status if isinstance(status, int) and 100 <= status <= 599 else None

    @property
    def reason(self) -> host_usage.Reason:
        return CAUSES[self.cause].reason

    @property
    def detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"cause": self.cause}
        if self.field is not None:
            detail["field"] = self.field
        if self.status is not None:
            detail["status"] = self.status
        return detail


@dataclass(frozen=True)
class CursorAccountResult(host_usage.HostUsageResult):
    """A whole-account read: ``hosts`` and ``tokens_by_day`` for the account's
    lookback window, plus the account key and window start the fleet dedup
    rule needs. Not merged into the machine's own host totals."""

    account: str | None = None
    since: str | None = None
    read_day: str | None = None
    through: str | None = None
    """Newest UTC day with an event in the window, or None. The account's events can end
    while use continues (a unified plan keeps its usage elsewhere), so the fleet rule
    replaces local usage only up to this day."""


# ---------------------------------------------------------------------------
# Login store (read-only; the only place that touches Cursor's credential)
# ---------------------------------------------------------------------------


def _app_db_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Cursor"
        / "User"
        / "globalStorage"
        / "state.vscdb"
    )


def _config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if raw.startswith("/"):
        return Path(raw)
    return Path.home() / ".config"


def _agent_auth_path() -> Path:
    return _config_home() / "cursor" / "auth.json"


def _linux_db_path() -> Path:
    return _config_home() / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _decode_db_value(value: object) -> str | None:
    """Text, or BLOB bytes as CodexBar decodes them (ASCII UTF-16LE before UTF-8)."""
    if isinstance(value, str):
        return value
    if not isinstance(value, (bytes, bytearray)):
        return None
    data = bytes(value)
    if (
        data
        and len(data) % 2 == 0
        and all(0 < data[i] < 128 and data[i + 1] == 0 for i in range(0, len(data), 2))
    ):
        decoded = data.decode("utf-16-le")
        if decoded.strip():
            return decoded
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("utf-16-le")
        except UnicodeDecodeError:
            return None


def _query_state_db(path: Path, *, immutable: bool) -> object:
    uri = (
        "file:" + urllib.parse.quote(str(path)) + "?mode=ro" + ("&immutable=1" if immutable else "")
    )
    import sqlite3  # lazy: the CLI must start on a Python built without sqlite3

    connection = sqlite3.connect(uri, uri=True, timeout=0.25)
    try:
        row = connection.execute(
            "SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (_APP_KEY,)
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else row[0]


def _read_app_login(path: Path) -> str:
    """Cursor.app's access token from its VS Code-style state DB, read-only.

    An idle WAL-mode database with no sidecars needs immutable mode, so mm never
    recreates files in Cursor's directory; a live WAL is read normally.
    """
    try:
        present = path.is_file()
    except OSError:
        raise AccountFailure("store_unreadable") from None
    if not present:
        raise AccountFailure("no_login")
    try:
        import sqlite3
    except ImportError:
        raise AccountFailure("store_unreadable") from None
    try:
        # An idle WAL-mode database (no sidecars) is read immutable so SQLite never
        # creates -wal/-shm in Cursor's directory; a live WAL is read normally.
        sidecars = (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"))
        raw = _query_state_db(path, immutable=not any(p.exists() for p in sidecars))
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            raise AccountFailure("no_login") from None
        raise AccountFailure("store_unreadable") from None
    except (sqlite3.Error, OSError, ValueError):
        raise AccountFailure("store_unreadable") from None
    value = _decode_db_value(raw)
    if value is None and raw is not None:
        raise AccountFailure("store_unreadable")
    if value is None or not value.strip():
        raise AccountFailure("no_login")
    if value.startswith(_ENCRYPTED_PREFIX):
        raise AccountFailure("login_encrypted")
    return value


def _read_agent_login(path: Path) -> str:
    """cursor-agent's ``accessToken`` from ``cursor/auth.json``, read-only."""
    try:
        if not path.is_file():
            raise AccountFailure("no_login")
        if path.stat().st_size > _MAX_STORE_BYTES:
            raise AccountFailure("store_unreadable")
        data = path.read_bytes()
    except OSError:
        raise AccountFailure("store_unreadable") from None
    try:
        parsed = json.loads(data)
    except ValueError:
        raise AccountFailure("store_unreadable") from None
    if not isinstance(parsed, dict):
        raise AccountFailure("store_unreadable")
    value = parsed.get("accessToken")
    if not isinstance(value, str) or not value.strip():
        raise AccountFailure("no_login")
    return value.strip()


def _read_keychain_login(timeout: float | None) -> str:
    """cursor-agent's access token from the macOS Keychain, in memory only.

    The value goes from the child's stdout straight to the caller; stderr is
    discarded unread so no message, prompt text or secret can reach a log.
    """
    argv = [
        _SECURITY,
        "find-generic-password",
        "-s",
        f"{KEYCHAIN_PREFIX}-access-token",
        "-a",
        f"{KEYCHAIN_PREFIX}-user",
        "-w",
    ]
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout if timeout is not None else _PROMPT_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        raise AccountFailure("store_unreadable") from None
    try:
        if done.returncode == 44:  # errSecItemNotFound
            raise AccountFailure("no_login")
        if done.returncode != 0:
            raise AccountFailure("store_unreadable")
        value = done.stdout.strip()
    finally:
        done = None  # type: ignore[assignment]
    if not value:
        raise AccountFailure("no_login")
    return value


def _login_sources() -> list[Callable[[float | None], str]]:
    """Stores to try, in order, for this platform."""
    if _macos():
        return [lambda _t: _read_app_login(_app_db_path()), _read_keychain_login]
    return [
        lambda _t: _read_agent_login(_agent_auth_path()),
        lambda _t: _read_app_login(_linux_db_path()),
    ]


def _read_login(timeout: float | None) -> str:
    """Return Cursor's access token (a JWT), in memory only; never stored or logged.

    Tries each store for the platform in order. An absent session (``no_login``)
    falls through to the next store; any other failure is remembered and raised if
    no store yields a login, so a named cause is never hidden by a later ``no_login``.
    """
    first: AccountFailure | None = None
    for source in _login_sources():
        try:
            return source(timeout)
        except AccountFailure as failure:
            if failure.cause != "no_login" and first is None:
                first = failure
    raise first if first is not None else AccountFailure("no_login")


def _jwt_payload(jwt: str) -> dict[str, Any]:
    parts = jwt.split(".")
    if len(parts) < 2 or not parts[1]:
        raise AccountFailure("store_unreadable")
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment))
    except (ValueError, binascii.Error):
        raise AccountFailure("store_unreadable") from None
    if not isinstance(payload, dict):
        raise AccountFailure("store_unreadable")
    return payload


_USER_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _user_id(subject: object) -> str:
    """The last ``|``-separated part of the JWT ``sub`` claim."""
    parts = [p for p in subject.split("|") if p] if isinstance(subject, str) else []
    if not parts or not _USER_ID.fullmatch(parts[-1]):
        raise AccountFailure("store_unreadable")
    return parts[-1]


def _session_cookie(jwt: str, now: datetime) -> str:
    """The auth cookie for ``jwt``, only while its ``exp`` is more than 60 s away."""
    payload = _jwt_payload(jwt)
    user = _user_id(payload.get("sub"))
    expiry = payload.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        raise AccountFailure("store_unreadable")
    if expiry - now.timestamp() <= _EXPIRY_MARGIN_S:
        raise AccountFailure("login_expired")
    return f"WorkosCursorSessionToken={user}%3A%3A{jwt}"


# ---------------------------------------------------------------------------
# HTTP (the only place that touches the network)
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the session off cursor.com; refuse it instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    """The one HTTP seam; tests replace it, and the suite forbids the real one."""
    return urllib.request.build_opener(_NoRedirect)


def _allowed(url: str, method: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return (method, base) in ALLOWED_REQUESTS and not parts.fragment


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    if left <= 0:
        raise AccountFailure("deadline")
    return left


def _fetch(
    method: str, url: str, cookie: str, *, body: bytes | None = None, deadline: float | None
) -> bytes:
    """One allowlisted request; the timeout is the time left before ``deadline``."""
    if not _allowed(url, method):
        raise AssertionError("cursor_account requested a URL outside ALLOWED_REQUESTS")
    timeout = _remaining(deadline)
    headers = {"Accept": "text/csv, application/json", "Cookie": cookie}
    if method == "POST":
        headers.update({"Content-Type": "application/json", "Origin": ORIGIN})
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    opener = _build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        cause = "login_refused" if status in (401, 403) else "http_error"
        raise AccountFailure(cause, status=status) from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        raise AccountFailure("network") from None
    if len(payload) > MAX_BODY_BYTES:
        raise AccountFailure("http_error")
    return payload


# ---------------------------------------------------------------------------
# Parsing (strict; any drift fails the whole read) and the per-day cache
# ---------------------------------------------------------------------------

DayTotals = dict[str, list[int]]
"""``{model: [input, cache_create, cache_read, output]}`` for one UTC day."""


def _account_key(jwt: str) -> str:
    """Hash of the user id in the JWT ``sub`` (the same id ``api/auth/me`` returns)."""
    return hashlib.sha256(
        _user_id(_jwt_payload(jwt).get("sub")).lower().encode("utf-8")
    ).hexdigest()


def _billing_url(first: date, last: date) -> str:
    start = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    end = datetime(last.year, last.month, last.day, tzinfo=timezone.utc) + timedelta(days=1)
    stamp = "%Y-%m-%dT%H%%3A%M%%3A%S.000Z"
    return (
        f"{BILLING_URL}?scope=user&startTime={start.strftime(stamp)}&endTime={end.strftime(stamp)}"
    )


def _counter(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise AccountFailure("changed_field", field=name)
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or value < 0 or value > _MAX_COUNTER:
        raise AccountFailure("changed_field", field=name)
    return value


def _row_day(value: str) -> str:
    text = value.strip()
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise AccountFailure("changed_field", field="Event Time") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _parse_billing(body: bytes, first: date, last: date) -> dict[str, DayTotals]:
    """Per-day per-model counters for ``first``..``last``; quiet days map to ``{}``.

    Only ``cursor`` rows count. A row's ``Usage`` is a JSON object of token counters
    (``{}`` is a call that carried none). Anything unrecognised fails the whole read.
    """
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AccountFailure("changed_field", field="Billing Item ID") from None
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        raise AccountFailure("changed_field", field="Billing Item ID") from None
    index = {name: position for position, name in enumerate(header)}
    for column in _COLUMNS:
        if column not in index:
            raise AccountFailure("changed_field", field=column)
    days: dict[str, DayTotals] = {}
    cursor = first
    while cursor <= last:
        days[cursor.isoformat()] = {}
        cursor += timedelta(days=1)
    seen: set[str] = set()
    try:
        for row in reader:
            if not row:
                continue
            if len(row) != len(header):
                raise AccountFailure("changed_field", field="Billing Item ID")
            if row[index["Product"]] != PRODUCT:
                continue
            item = row[index["Billing Item ID"]]
            if not item or item in seen:
                if not item:
                    raise AccountFailure("changed_field", field="Billing Item ID")
                continue  # one billed call, however many times the export repeats it
            seen.add(item)
            day = _row_day(row[index["Event Time"]])
            if day not in days:
                continue
            model = row[index["Model"]].strip()
            if not model or len(model.encode("utf-8")) > MAX_MODEL_ID_BYTES:
                raise AccountFailure("changed_field", field="Model")
            try:
                usage = json.loads(row[index["Usage"]])
            except ValueError:
                raise AccountFailure("changed_field", field="Usage") from None
            if not isinstance(usage, dict):
                raise AccountFailure("changed_field", field="Usage")
            counters = [0, 0, 0, 0]
            for key, value in usage.items():
                if key not in _USAGE_KEYS:
                    raise AccountFailure("changed_field", field="Usage")
                counters[TOKEN_FIELDS.index(_USAGE_KEYS[key])] += _counter(value, key)
            if any(counters):
                total = days[day].setdefault(model, [0, 0, 0, 0])
                for slot, amount in enumerate(counters):
                    total[slot] += amount
    except csv.Error:
        raise AccountFailure("changed_field", field="Billing Item ID") from None
    return days


def _cached_days(data: dict[str, Any]) -> dict[str, DayTotals]:
    """Validated per-day totals from the cache; anything malformed drops the whole map."""
    raw = data.get("days")
    if data.get("version") != host_usage.CACHE_VERSION or not isinstance(raw, dict):
        return {}
    if len(raw) > MAX_CACHED_DAYS:
        return {}
    days: dict[str, DayTotals] = {}
    for day, models in raw.items():
        if not isinstance(day, str) or not _DAY.fullmatch(day) or not isinstance(models, dict):
            return {}
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return {}
        entry: DayTotals = {}
        for model, counters in models.items():
            if (
                not isinstance(model, str)
                or not model
                or len(model.encode("utf-8")) > MAX_MODEL_ID_BYTES
                or not isinstance(counters, list)
                or len(counters) != 4
                or any(
                    isinstance(n, bool) or not isinstance(n, int) or n < 0 or n > _MAX_COUNTER
                    for n in counters
                )
            ):
                return {}
            entry[model] = list(counters)
        days[day] = entry
    return days


def _wanted_days(now: datetime) -> list[date]:
    """Finished UTC days to cover, newest first."""
    final_before = (now - timedelta(hours=SETTLE_HOURS)).astimezone(timezone.utc).date()
    return [final_before - timedelta(days=n) for n in range(1, LOOKBACK_DAYS + 1)]


def _refresh(
    cookie: str, days: dict[str, DayTotals], wanted: list[date], deadline: float
) -> tuple[dict[str, DayTotals], AccountFailure | None]:
    """Fetch missing wanted days, newest first, ``CHUNK_DAYS`` per request.

    Returns the updated map and the first transient failure (the progress made before it
    is kept). A refused login or a changed format raises: nothing may be published then.
    """
    missing = [d for d in wanted if d.isoformat() not in days]
    updated = dict(days)
    for at in range(0, len(missing), CHUNK_DAYS):
        chunk = missing[at : at + CHUNK_DAYS]
        first, last = min(chunk), max(chunk)
        try:
            body = _fetch("GET", _billing_url(first, last), cookie, deadline=deadline)
        except AccountFailure as failure:
            if failure.cause in {"network", "deadline", "http_error"}:
                return updated, failure
            raise
        parsed = _parse_billing(body, first, last)
        updated.update(
            {day: totals for day, totals in parsed.items() if date.fromisoformat(day) in chunk}
        )
    return updated, None


def _coverage(days: dict[str, DayTotals], wanted: list[date]) -> tuple[date, date] | None:
    """The contiguous run of cached wanted days ending at the newest one: ``(first, last)``."""
    have = [d for d in wanted if d.isoformat() in days]  # newest first
    if not have:
        return None
    last = have[0]
    first = last
    while (first - timedelta(days=1)).isoformat() in days and (
        first - timedelta(days=1) >= wanted[-1]
    ):
        first -= timedelta(days=1)
    return first, last


def _result(
    days: dict[str, DayTotals], span: tuple[date, date], account: str, now: datetime
) -> CursorAccountResult:
    first, last = span
    buckets = host_usage.HostUsageBuckets()
    at = first
    while at <= last:
        for model, counters in days.get(at.isoformat(), {}).items():
            host_usage._add_usage(buckets, at.isoformat(), model, dict(zip(TOKEN_FIELDS, counters)))
        at += timedelta(days=1)
    return CursorAccountResult(
        buckets.by_family,
        complete=True,
        tokens_by_day=buckets.by_day,
        account=account,
        since=first.isoformat(),
        read_day=now.astimezone(timezone.utc).date().isoformat(),
        through=last.isoformat(),
    )


@dataclass
class _Progress:
    """What a read learned, for the cache write (never the login)."""

    days: dict[str, DayTotals]
    account: str | None = None
    account_changed: bool = False


def _read_account(deadline: float, now: datetime, progress: _Progress) -> CursorAccountResult:
    """One pass: login, missing days, the published run. Raises AccountFailure.

    Any unexpected exception is re-raised as a fixed-text ``AccountFailure`` chained
    to nothing, so no message that might carry the login reaches ``events_tail``'s
    ``host reader ... raised`` notice or a traceback. ``AssertionError`` is the test
    guard and passes through. A transient failure with any cached coverage still
    publishes that coverage (an older ``through``); with none it raises.
    """
    _remaining(deadline)
    try:
        jwt = _read_login(_remaining(deadline))
        account = _account_key(jwt)
        if progress.account != account:
            # Unbound legacy caches also need a fresh fetch: their owner is unknown.
            progress.days = {}
            progress.account = account
            progress.account_changed = True
        cookie = _session_cookie(jwt, now)
    except (AccountFailure, AssertionError):
        raise
    except Exception:
        raise AccountFailure("store_unreadable") from None
    finally:
        jwt = ""
    wanted = _wanted_days(now)
    try:
        updated, failure = _refresh(cookie, progress.days, wanted, deadline)
    except (AccountFailure, AssertionError):
        raise
    except Exception:
        raise AccountFailure("http_error") from None
    finally:
        cookie = ""
    progress.days = updated
    span = _coverage(updated, wanted)
    if span is None:
        raise failure or AccountFailure("deadline")
    return _result(updated, span, account, now)


def check_login(*, timeout: float | None = None, now: datetime | None = None) -> str | None:
    """Presence and expiry check for ``mm enable-source``: no network, a cause or None."""
    try:
        _session_cookie(_read_login(timeout), now or datetime.now(timezone.utc))
    except AccountFailure as failure:
        return failure.cause
    except AssertionError:
        raise
    except Exception:
        return "store_unreadable"
    return None


# ---------------------------------------------------------------------------
# Cache (account-bound day totals and reader state) and the reader
# ---------------------------------------------------------------------------


def _empty_cache() -> dict[str, Any]:
    return {"version": host_usage.CACHE_VERSION, "complete_once": False}


def _cached_detail(data: dict[str, Any]) -> dict[str, Any] | None:
    """Validated blocker detail; a hand-edited cache cannot forge text."""
    if host_usage._cached_last_reason(data) is None:
        return None
    raw = data.get("last_reason_detail")
    if not isinstance(raw, dict):
        return None
    cause = raw.get("cause")
    if not isinstance(cause, str) or cause not in CAUSES:
        return None
    detail: dict[str, Any] = {"cause": cause}
    named = raw.get("field")
    if isinstance(named, str) and named in _KNOWN_NAMES:
        detail["field"] = named
    status = raw.get("status")
    if type(status) is int and 100 <= status <= 599:
        detail["status"] = status
    return detail


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _cached_newest_day(data: dict[str, Any]) -> str | None:
    raw = data.get("newest_event_day")
    if data.get("version") != host_usage.CACHE_VERSION or not isinstance(raw, str):
        return None
    if not _DAY.fullmatch(raw):
        return None
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return raw


def _cached_models(data: dict[str, Any]) -> list[str]:
    raw = data.get("models")
    if data.get("version") != host_usage.CACHE_VERSION or not isinstance(raw, list):
        return []
    models = [m for m in raw if isinstance(m, str) and m and len(m.encode()) <= MAX_MODEL_ID_BYTES]
    return sorted(set(models))[:MAX_DIAG_MODELS]


def blocker(detail: dict[str, Any] | None) -> tuple[str, str] | None:
    """(what broke, the one fixing action) for a recorded cause, else None."""
    if not isinstance(detail, dict):
        return None
    cause = detail.get("cause")
    if not isinstance(cause, str) or cause not in CAUSES:
        return None
    broke = CAUSES[cause].broke
    status = detail.get("status")
    named = detail.get("field")
    if cause == "http_error" and type(status) is int:
        broke = f"cursor.com returned HTTP {status}"
    elif named in _KNOWN_NAMES:
        broke = f"{broke} ({named})"
    return broke, fix_for(cause)


def read_cursor_account_usage(
    *,
    deadline: float | None = None,
    consented: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> host_usage.HostUsageResult:
    """Read the owner's Cursor account usage once, under a deadline.

    Closed by default: unconsented, it returns ``no_metadata_ledger`` before
    any login, network or cache access. Any failure returns an incomplete
    result with no buckets; a complete result is a ``CursorAccountResult``.
    """
    if not consented:
        return host_usage._incomplete("no_metadata_ledger")
    started = time.monotonic()
    read_deadline = deadline if deadline is not None else started + host_usage.DEFAULT_READ_BUDGET_S
    if host_usage._expired(read_deadline):
        return host_usage._incomplete("deadline")
    now = clock() if clock is not None else datetime.now(timezone.utc)
    try:
        with locked_json_rmw(
            CACHE_PATH,
            mode=0o600,
            compact=True,
            default_factory=_empty_cache,
            retry_intervals=(),
            on_contention="raise" if host_usage._DIAGNOSTIC.get() is not None else "warn",
            contention_warning="Cursor account usage cache was locked; skipping the account read",
        ) as locked:
            if not locked.is_locked:
                return host_usage._incomplete("locked")
            prior_data = locked.data if isinstance(locked.data, dict) else _empty_cache()
            prior = (
                host_usage._cached_last_reason(prior_data),
                host_usage._cached_reason_since(prior_data),
            )
            failure: AccountFailure | None = None
            with host_usage._pause_gc():
                progress = _Progress(_cached_days(prior_data), prior_data.get("account"))
                try:
                    result: host_usage.HostUsageResult = _read_account(read_deadline, now, progress)
                except AccountFailure as caught:
                    failure = caught
                    result = host_usage._incomplete(caught.reason)
            if progress.account_changed:
                # Timing, models and complete_once belong to the old account too.
                prior_data = _empty_cache()
                prior = (None, None)
            ready = time.monotonic()
            over_budget = result.complete and host_usage._expired(read_deadline)
            carried = host_usage._carry_reason(*prior, result, now, over_budget=over_budget)
            timing = host_usage._carry_read_timing(
                prior_data,
                result,
                carried[0],
                started,
                ready,
                read_deadline,
                now,
                over_budget=over_budget,
            )
            new_data: dict[str, Any] = {
                **timing,
                "version": host_usage.CACHE_VERSION,
                "complete_once": bool(prior_data.get("complete_once") is True or result.complete),
                "last_reason": carried[0],
                "last_reason_since": carried[1],
            }
            if progress.account is not None:
                new_data["account"] = progress.account
            newest = (
                result.through
                if isinstance(result, CursorAccountResult)
                else _cached_newest_day(prior_data)
            )
            if newest is not None:
                new_data["newest_event_day"] = newest
            kept = {
                day: totals
                for day, totals in progress.days.items()
                if date.fromisoformat(day) >= _wanted_days(now)[-1]
            }
            if kept:
                new_data["days"] = kept
            if result.complete:
                new_data["models"] = sorted(
                    {m for day in result.tokens_by_day.values() for m in day.get("by_model", {})}
                )[:MAX_DIAG_MODELS]
            else:
                new_data["models"] = _cached_models(prior_data)
            if carried[0] is not None:
                if failure is not None and carried[0] == failure.reason:
                    new_data["last_reason_detail"] = failure.detail
                elif carried[0] == prior[0]:
                    kept = _cached_detail(prior_data)
                    if kept is not None:
                        new_data["last_reason_detail"] = kept
            if not result.complete and new_data == prior_data:
                raise host_usage._NoCacheCommit(result)
            locked.data = new_data
            if over_budget:
                result = host_usage._incomplete("deadline")
        host_usage._notice_cache_write_failure(locked.write_error)
        return result
    except LockContended:
        host_usage._diagnostic(
            "mm: notice: Cursor account usage cache was locked; skipping the account read\n", False
        )
        return host_usage._incomplete("locked")
    except host_usage._NoCacheCommit as aborted:
        return aborted.result
    except OSError:
        return host_usage._incomplete("io_error")


def cursor_account_usage_diag() -> dict[str, Any]:
    """Cache-only reader state. Never reads the login or calls the network."""
    blank: dict[str, Any] = {
        **host_usage._cached_read_timing({}),
        "cache_state": "missing",
        "complete_once": False,
        "last_reason": None,
        "last_reason_since": None,
        "last_reason_detail": None,
        "newest_event_day": None,
        "days_cached": 0,
        "model_count": 0,
        "models": [],
    }
    try:
        with locked_json_snapshot(CACHE_PATH) as snap:
            data = snap.data
            state = snap.state
    except OSError:
        return {**blank, "cache_state": "unreadable"}
    if state != "valid" or not isinstance(data, dict):
        return {
            **blank,
            "cache_state": "missing" if state in {"missing", "empty"} else "unreadable",
        }
    models = _cached_models(data)
    return {
        **host_usage._cached_read_timing(data),
        "cache_state": "ok",
        "complete_once": data.get("complete_once") is True
        and data.get("version") == host_usage.CACHE_VERSION,
        "last_reason": host_usage._cached_last_reason(data),
        "last_reason_since": host_usage._cached_reason_since(data),
        "last_reason_detail": _cached_detail(data),
        "newest_event_day": _cached_newest_day(data),
        "days_cached": len(_cached_days(data)),
        "model_count": len(models),
        "models": models,
    }


# ---------------------------------------------------------------------------
# Census (Track 107A task 1): evidence for CONTRACT.md, names and counts only
# ---------------------------------------------------------------------------


def census(*, deadline: float, now: datetime | None = None) -> dict[str, Any]:
    """Summarize one live chunk of the billing export without keeping any value.

    Fetches the newest CHUNK_DAYS finished days and reports shape only: column
    names, row counts, product / usage-type / charge-category value counts, Usage
    key names with their value type names, model ids, the UTC date span, and how many
    rows carried each counter. Refuses to return a summary containing the login or cookie.
    """
    when = now if now is not None else datetime.now(timezone.utc)
    jwt = _read_login(_remaining(deadline))
    cookie = _session_cookie(jwt, when)
    try:
        wanted = _wanted_days(when)[:CHUNK_DAYS]
        first, last = min(wanted), max(wanted)
        body = _fetch("GET", _billing_url(first, last), cookie, deadline=deadline)
        summary = _summarize_billing(body, first, last)
        text = json.dumps(summary)
        if (jwt and jwt in text) or (cookie and cookie in text):
            raise AssertionError("census summary contained the login value")
        return summary
    finally:
        jwt = cookie = ""


def _summarize_billing(body: bytes, first: date, last: date) -> dict[str, Any]:
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig", "replace"), newline="")))
    summary: dict[str, Any] = {
        "window": [first.isoformat(), last.isoformat()],
        "bytes": len(body),
        "columns": list(rows[0]) if rows else [],
        "rows": len(rows),
        "billing_item_ids_unique": len({r.get("Billing Item ID") for r in rows}),
    }
    for column in (
        "Product",
        "Charge Category",
        "Usage Type",
        "Principal Type",
        "Pricing Currency",
    ):
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.get(column, "")] = counts.get(row.get(column, ""), 0) + 1
        summary[column] = counts
    keys: dict[str, set[str]] = {}
    carried = {k: 0 for k in _USAGE_KEYS}
    for row in rows:
        try:
            usage = json.loads(row.get("Usage", ""))
        except ValueError:
            continue
        if isinstance(usage, dict):
            for key, value in usage.items():
                keys.setdefault(str(key), set()).add(type(value).__name__)
                if key in carried:
                    carried[key] += 1
    summary["usage_keys"] = {k: sorted(v) for k, v in sorted(keys.items())}
    summary["rows_with_counter"] = carried
    days = sorted({_day_or_none(r.get("Event Time", "")) for r in rows} - {None})
    summary["date_span"] = [days[0], days[-1]] if days else []
    models = sorted({r.get("Model", "") for r in rows})
    summary["model_count"] = len(models)
    summary["models"] = models[:MAX_DIAG_MODELS]
    return summary


def _day_or_none(value: str) -> str | None:
    try:
        return _row_day(value)
    except AccountFailure:
        return None


__all__ = [
    "ALLOWED_REQUESTS",
    "ALLOWED_URLS",
    "CACHE_PATH",
    "CAUSES",
    "AccountFailure",
    "CursorAccountResult",
    "TOKEN_FIELDS",
    "blocker",
    "census",
    "check_login",
    "cursor_account_usage_diag",
    "fix_for",
    "read_cursor_account_usage",
]
