"""Cursor account usage reader (Track 107A): adapter contract, safety and wiring.

Mirrors steipete/CodexBar's Cursor login handling and reads the dashboard's billing-items
export. Everything here uses synthetic responses (``tests/fixtures/host_sessions/cursor_account/``),
a fake JWT built at run time and a temporary SQLite state DB. ``conftest._forbid_cursor_account_io``
makes the real login loader and HTTP opener raise, so a test that forgets a fake fails loudly.
Nothing here is census evidence (CONTRACT.md has that).
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mind_meld import config as mm_config
from mind_meld import events, events_tail, host_registry, host_usage
from mind_meld.cli import app
from mind_meld.hosts import cursor_account as ca

ORIGINAL_READ_LOGIN = ca._read_login
FIXTURES = Path(__file__).parent / "fixtures" / "host_sessions" / "cursor_account"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
BILL_KEY = ("GET", ca.BILLING_URL)
runner = CliRunner()
HEADER = (
    "Billing Item ID,Event Time,Product,Charge Category,Usage Type,Principal Type,Principal ID,"
    "Usage,Pricing Currency,Pricing Contracted,Model,User Email"
)


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def make_jwt(sub: str = "synthetic|user_synthetic01", exp: int = 4_102_444_800) -> str:
    """A fake three-part JWT (never a real credential)."""
    return f"{_b64({'alg': 'none'})}.{_b64({'sub': sub, 'exp': exp})}.c2lnbmF0dXJl"


SENTINEL = make_jwt()
"""The fake login. Every leak test searches for it and for its payload segment."""
COOKIE = f"WorkosCursorSessionToken=user_synthetic01%3A%3A{SENTINEL}"


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class _Opener:
    def __init__(self, io: "_AccountIO") -> None:
        self._io = io

    def open(self, request, timeout=None):
        url = request.full_url
        method = request.get_method()
        self._io.calls.append(
            {
                "url": url,
                "method": method,
                "headers": {k.lower(): v for k, v in request.header_items()},
                "body": request.data,
                "timeout": timeout,
            }
        )
        outcome = self._io.routes[(method, url.split("?")[0])]
        if callable(outcome):
            outcome = outcome(url)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(url, outcome, "synthetic", {}, None)  # type: ignore[arg-type]
        return _Response(outcome)


class _AccountIO:
    """The injected login loader and HTTP opener, with a record of every touch."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], object] = {
            BILL_KEY: (FIXTURES / "billing.csv").read_bytes()
        }
        self.calls: list[dict] = []
        self.login_calls = 0
        self.login = SENTINEL
        self.login_error: BaseException | None = None

    def read_login(self, timeout):
        self.login_calls += 1
        if self.login_error is not None:
            raise self.login_error
        return self.login

    @property
    def touched(self) -> bool:
        return bool(self.calls) or self.login_calls > 0

    def windows(self) -> list[tuple[str, str]]:
        """``(startTime, endTime)`` of every billing request, URL-decoded."""
        out = []
        for call in self.calls:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)
            out.append((query["startTime"][0], query["endTime"][0]))
        return out


@pytest.fixture
def account_io(monkeypatch) -> _AccountIO:
    io = _AccountIO()
    monkeypatch.setattr(ca, "_read_login", io.read_login)
    monkeypatch.setattr(ca, "_build_opener", lambda: _Opener(io))
    monkeypatch.setattr(ca, "_macos", lambda: False)
    return io


def _read(**kwargs):
    kwargs.setdefault("consented", True)
    kwargs.setdefault("deadline", time.monotonic() + 30)
    kwargs.setdefault("clock", lambda: NOW)
    return ca.read_cursor_account_usage(**kwargs)


def _day(result, family, day):
    return result.hosts[family][day]


def _csv(*rows: str) -> bytes:
    return ("\n".join([HEADER, *rows]) + "\n").encode()


def _row(item, ts, model, usage, *, product="cursor"):
    text = json.dumps(usage, separators=(",", ":")).replace('"', '""')
    return (
        f'{item},{ts},{product},usage,included,user,user_synthetic01,"{text}",USD,'
        f"0.0000000000,{model},user@example.invalid"
    )


def _at(day: str, hour: int = 10) -> str:
    return f"{day}T{hour:02d}:00:00.000Z"


# ── the reader ────────────────────────────────────────────────────────────


def test_unconsented_call_touches_no_login_network_or_cache(account_io):
    result = ca.read_cursor_account_usage()
    assert not result.complete and result.reason == "no_metadata_ledger"
    assert not account_io.touched
    assert not ca.CACHE_PATH.exists()


def test_expired_deadline_returns_before_the_cache_or_login(account_io):
    result = _read(deadline=time.monotonic() - 1)
    assert not result.complete and result.reason == "deadline"
    assert not account_io.touched
    assert not ca.CACHE_PATH.exists()


def test_success_counts_families_disjointly_and_ignores_other_products(account_io):
    result = _read()
    assert result.complete and isinstance(result, ca.CursorAccountResult)
    assert result.read_day == "2026-11-03" and result.through == "2026-11-02"
    assert re.fullmatch(r"[0-9a-f]{64}", result.account)
    assert set(result.hosts) == {"grok", "claude", "codex", "other"}
    assert _day(result, "grok", "2026-11-01") == {
        "input": 100,
        "cache_create": 0,
        "cache_read": 900,
        "output": 50,
    }
    assert _day(result, "claude", "2026-11-01")["cache_create"] == 10
    assert _day(result, "codex", "2026-11-02")["input"] == 300
    # the duplicated b4 row counts once, b5 carried no counters, and b6 is the grok-chat product
    assert _day(result, "other", "2026-11-02") == {
        "input": 40,
        "cache_create": 0,
        "cache_read": 0,
        "output": 5,
    }
    assert "2026-09-01" not in result.hosts["grok"]
    day = result.tokens_by_day["2026-11-02"]
    assert day["input"] == 340 and set(day["by_model"]) == {"gpt-5.1-codex", "composer-2"}
    assert not result.partial_days


@pytest.mark.parametrize(
    ("model", "family"),
    [
        ("grok-4.7-xhigh", "grok"),
        ("claude-opus-4", "claude"),
        ("gpt-5", "codex"),
        ("zeta-1", "other"),
    ],
)
def test_models_land_in_their_families(account_io, model, family):
    account_io.routes[BILL_KEY] = _csv(
        _row("m1", _at("2026-11-02"), model, {"prompt-text-tokens": "7"})
    )
    result = _read()
    assert result.complete and list(result.hosts) == [family]


def test_the_account_key_is_the_hash_of_the_jwt_user_id(account_io):
    import hashlib

    assert _read().account == hashlib.sha256(b"user_synthetic01").hexdigest()
    account_io.login = make_jwt(sub="auth0|User_Synthetic01")
    assert _read().account == hashlib.sha256(b"user_synthetic01").hexdigest()


def test_cache_holds_state_and_per_day_counts_only(account_io):
    assert _read().complete
    cached = json.loads(ca.CACHE_PATH.read_text())
    assert set(cached) <= {
        "version",
        "complete_once",
        "last_reason",
        "last_reason_since",
        "last_reason_detail",
        "last_complete_ms",
        "last_complete_at",
        "last_deadline_allotted_ms",
        "newest_event_day",
        "models",
        "days",
        "account",
    }
    assert cached["complete_once"] is True and cached["last_reason"] is None
    assert cached["newest_event_day"] == "2026-11-02"
    assert cached["days"]["2026-11-01"]["grok-4.7-xhigh-fast"] == [100, 0, 900, 50]
    assert cached["days"]["2026-10-20"] == {}, "a quiet day is cached as covered"
    assert cached["models"] == [
        "claude-opus-5-5-medium",
        "composer-2",
        "gpt-5.1-codex",
        "grok-4.7-xhigh-fast",
    ]
    text = ca.CACHE_PATH.read_text()
    assert SENTINEL not in text and "cursor.com" not in text and "example.invalid" not in text
    assert "user_synthetic01" not in text
    assert oct(ca.CACHE_PATH.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("legacy", [False, True])
def test_login_change_or_unbound_cache_never_reuses_old_totals(account_io, legacy):
    original = _read()
    assert original.complete and original.hosts
    if legacy:
        cached = json.loads(ca.CACHE_PATH.read_text())
        cached.pop("account", None)
        ca.CACHE_PATH.write_text(json.dumps(cached))
    else:
        account_io.login = make_jwt(sub="synthetic|different_user")
    account_io.calls.clear()
    account_io.routes[BILL_KEY] = urllib.error.URLError("synthetic")
    result = _read()
    assert not result.complete and not result.hosts
    cached = json.loads(ca.CACHE_PATH.read_text())
    assert not cached.get("days")
    assert cached["complete_once"] is False
    assert not cached.get("models") and not cached.get("newest_event_day")
    account_io.routes[BILL_KEY] = _csv()
    refreshed = _read()
    assert refreshed.complete and not refreshed.hosts
    assert json.loads(ca.CACHE_PATH.read_text())["account"] == refreshed.account
    if not legacy:
        assert refreshed.account != original.account


def test_diag_is_cache_only_and_reports_the_last_read(account_io):
    assert ca.cursor_account_usage_diag()["cache_state"] == "missing"
    _read()
    account_io.calls.clear()
    before = account_io.login_calls
    state = ca.cursor_account_usage_diag()
    assert state["cache_state"] == "ok" and state["complete_once"] is True
    assert state["model_count"] == 4 and state["last_reason"] is None
    assert state["days_cached"] == ca.LOOKBACK_DAYS and state["newest_event_day"] == "2026-11-02"
    assert account_io.calls == [] and account_io.login_calls == before


def test_a_window_with_no_usage_is_a_real_empty_read(account_io):
    account_io.routes[BILL_KEY] = _csv()
    result = _read()
    assert result.complete and result.hosts == {} and result.account
    assert result.through == "2026-11-02" and result.since == "2026-10-04"


# ── the per-day cache: fetch only what is missing, newest first ───────────


def test_a_cold_read_fetches_every_wanted_day_in_three_day_chunks_newest_first(account_io):
    result = _read()
    windows = account_io.windows()
    assert len(windows) == 10, "30 days in chunks of 3"
    assert windows[0] == ("2026-10-31T00:00:00.000Z", "2026-11-03T00:00:00.000Z")
    assert windows[-1] == ("2026-10-04T00:00:00.000Z", "2026-10-07T00:00:00.000Z")
    assert result.since == "2026-10-04" and result.through == "2026-11-02"


def test_a_warm_read_costs_nothing_and_a_new_day_fetches_only_that_day(account_io):
    _read()
    account_io.calls.clear()
    again = _read()
    assert again.complete and account_io.calls == [], "every wanted day is cached"
    later = _read(clock=lambda: NOW + timedelta(days=1))
    assert later.through == "2026-11-03"
    assert account_io.windows() == [("2026-11-03T00:00:00.000Z", "2026-11-04T00:00:00.000Z")]
    assert _day(later, "grok", "2026-11-01")["input"] == 100, "older days come from the cache"


def test_a_day_is_not_final_until_an_hour_after_it_ends(account_io):
    early = datetime(2026, 11, 3, 0, 30, tzinfo=timezone.utc)
    result = _read(clock=lambda: early)
    assert result.through == "2026-11-01", "11-02 is still settling at 00:30"
    assert account_io.windows()[0][1] == "2026-11-02T00:00:00.000Z"


def test_a_failure_mid_fill_publishes_the_days_already_cached(account_io):
    chunks = {"n": 0}
    good = (FIXTURES / "billing.csv").read_bytes()

    def flaky(url):
        chunks["n"] += 1
        return good if chunks["n"] <= 2 else urllib.error.URLError("synthetic")

    account_io.routes[BILL_KEY] = flaky
    result = _read()
    assert result.complete and result.through == "2026-11-02"
    assert result.since == "2026-10-28", "two good chunks of three days: 10-28 .. 11-02"
    state = ca.cursor_account_usage_diag()
    assert state["days_cached"] == 6 and state["last_reason"] is None  # 10-28 .. 11-02
    # the next read resumes where it stopped and completes the rest
    account_io.routes[BILL_KEY] = good
    account_io.calls.clear()
    assert _read().since == "2026-10-04"
    assert len(account_io.calls) == 8, "only the 24 missing days, three per request"


def test_a_failure_before_any_day_is_cached_fails_closed(account_io):
    account_io.routes[BILL_KEY] = urllib.error.URLError("synthetic")
    result = _read()
    assert not result.complete and result.reason == "io_error" and not result.hosts
    assert ca.cursor_account_usage_diag()["last_reason_detail"] == {"cause": "network"}


def test_a_gap_in_the_cache_ends_the_published_run_at_the_gap(account_io):
    _read()
    data = json.loads(ca.CACHE_PATH.read_text())
    del data["days"]["2026-11-02"]  # yesterday is missing; older days are cached
    ca.CACHE_PATH.write_text(json.dumps(data))
    account_io.routes[BILL_KEY] = urllib.error.URLError("synthetic")
    result = _read()
    assert result.complete and result.through == "2026-11-01" and result.since == "2026-10-04"
    assert "2026-11-02" not in result.tokens_by_day
    data["days"]["2026-10-20"] = {"m": [1, 2, 3]}  # malformed: the whole map is dropped
    ca.CACHE_PATH.write_text(json.dumps(data))
    assert not _read().complete


def test_cached_days_outside_the_lookback_are_pruned(account_io):
    _read()
    data = json.loads(ca.CACHE_PATH.read_text())
    data["days"]["2026-08-01"] = {"old-model": [1, 1, 1, 1]}
    ca.CACHE_PATH.write_text(json.dumps(data))
    _read()
    assert "2026-08-01" not in json.loads(ca.CACHE_PATH.read_text())["days"]


def test_the_newest_day_is_reported_and_a_bad_cached_day_is_ignored(account_io):
    _read()
    assert ca.cursor_account_usage_diag()["newest_event_day"] == "2026-11-02"
    data = json.loads(ca.CACHE_PATH.read_text())
    data["newest_event_day"] = "2026-13-45"
    ca.CACHE_PATH.write_text(json.dumps(data))
    assert ca.cursor_account_usage_diag()["newest_event_day"] is None


def test_a_failed_read_keeps_the_previous_newest_day(account_io):
    _read()
    account_io.login_error = ca.AccountFailure("login_expired")
    assert not _read().complete
    assert ca.cursor_account_usage_diag()["newest_event_day"] == "2026-11-02"


# ── what it may call ───────────────────────────────────────────────────────


def test_requests_are_only_the_allowlisted_get_with_the_cookie_and_bounded_timeouts(account_io):
    assert _read(deadline=time.monotonic() + 5).complete
    for call in account_io.calls:
        assert (call["method"], call["url"].split("?")[0]) in ca.ALLOWED_REQUESTS
        assert 0 < call["timeout"] <= 5
        assert call["headers"]["cookie"] == COOKIE
        assert "authorization" not in call["headers"] and call["body"] is None
        assert call["headers"].get("origin") is None
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)
        assert query["scope"] == ["user"] and set(query) == {"scope", "startTime", "endTime"}


def test_the_cookie_userid_is_the_last_pipe_part_of_the_subject():
    jwt = make_jwt(sub="auth0|google-oauth2|user_abc.1-2")
    assert ca._session_cookie(jwt, NOW) == f"WorkosCursorSessionToken=user_abc.1-2%3A%3A{jwt}"


@pytest.mark.parametrize("sub", ["", "|", "a|b c", "a|b/c", None, 5])
def test_an_unusable_subject_is_refused(sub):
    with pytest.raises(ca.AccountFailure) as caught:
        ca._session_cookie(make_jwt(sub=sub), NOW)
    assert caught.value.cause == "store_unreadable"


def test_module_names_no_other_https_url_and_one_fixed_keychain_command():
    source = Path(ca.__file__).read_text()
    literals = set(re.findall(r"https?://[^\s\"'`)]+", source))
    assert literals <= set(ca.ALLOWED_URLS.values()) | {"https://cursor.com"}, literals
    assert source.count("find-generic-password") == 2  # the docstring and the one argv
    assert source.count("subprocess.run(") == 1 and "shell=True" not in source
    for banned in ("add-generic-password", "delete-generic-password", "keyring", "secretstorage"):
        assert banned not in source, banned


def test_a_request_outside_the_allowlist_is_refused_before_any_request(account_io):
    for method, url in (
        ("GET", "https://example.invalid/api/usage"),
        ("POST", ca.BILLING_URL),
        ("GET", "https://grok.com/dashboard/api/dashboard/v2/export-billing-items-csv"),
        ("GET", "https://cursor.com/api/auth/me"),
        ("GET", ca.BILLING_URL + "#frag"),
        ("DELETE", ca.BILLING_URL),
    ):
        with pytest.raises(AssertionError):
            ca._fetch(method, url, COOKIE, deadline=None)
    assert account_io.calls == []


def test_a_redirect_fails_closed(account_io):
    assert ca._NoRedirect().redirect_request(None, None, 302, "x", {}, "https://elsewhere") is None
    account_io.routes[BILL_KEY] = 302
    result = _read()
    assert not result.complete and result.reason == "io_error" and not result.hosts
    assert ca.cursor_account_usage_diag()["last_reason_detail"] == {
        "cause": "http_error",
        "status": 302,
    }


# ── failure modes (criterion 9) ────────────────────────────────────────────

_GOOD_USAGE = {"prompt-text-tokens": "5"}
_ROUTE_FAILURES = [
    (403, "io_error", "login_refused"),
    (401, "io_error", "login_refused"),
    (
        _csv('b1,not-a-time,cursor,usage,included,user,u,"{}",USD,0,m,e'),
        "unsupported",
        "changed_field",
    ),
    (b"Billing Item ID,Event Time\nb1,2026-11-02T10:00:00.000Z\n", "unsupported", "changed_field"),
    (b"", "unsupported", "changed_field"),
    (
        _csv(_row("b1", _at("2026-11-02"), "", _GOOD_USAGE)),
        "unsupported",
        "changed_field",
    ),
    (
        _csv(_row("b1", _at("2026-11-02"), "grok-4.7", {"prompt-text-tokens": "x"})),
        "unsupported",
        "changed_field",
    ),
    (
        _csv(_row("b1", _at("2026-11-02"), "grok-4.7", {"prompt-text-tokens": "-3"})),
        "unsupported",
        "changed_field",
    ),
    (
        _csv(_row("b1", _at("2026-11-02"), "grok-4.7", {"surprise-tokens": "3"})),
        "unsupported",
        "changed_field",
    ),
    (
        _csv(_row("b1", _at("2026-11-02"), "grok-4.7", ["not", "an", "object"])),
        "unsupported",
        "changed_field",
    ),
    (
        _csv("", _row("", _at("2026-11-02"), "grok-4.7", _GOOD_USAGE)),
        "unsupported",
        "changed_field",
    ),
]


@pytest.mark.parametrize(("outcome", "reason", "cause"), _ROUTE_FAILURES)
def test_drift_and_refusals_fail_closed(account_io, outcome, reason, cause):
    account_io.routes[BILL_KEY] = outcome
    result = _read()
    assert not result.complete and result.reason == reason
    assert not result.hosts and not result.tokens_by_day
    state = ca.cursor_account_usage_diag()
    assert state["last_reason"] == reason and state["last_reason_detail"]["cause"] == cause
    assert state["complete_once"] is False and state["days_cached"] == 0
    broke, fix = ca.blocker(state["last_reason_detail"])
    assert broke and fix == ca.fix_for(cause) and "\n" not in broke


@pytest.mark.parametrize(
    ("outcome", "cause"),
    [
        (500, "http_error"),
        (urllib.error.URLError("synthetic"), "network"),
        (TimeoutError("synthetic"), "network"),
    ],
)
def test_a_transient_failure_with_nothing_cached_names_its_cause(account_io, outcome, cause):
    account_io.routes[BILL_KEY] = outcome
    assert not _read().complete
    assert ca.cursor_account_usage_diag()["last_reason_detail"]["cause"] == cause


def test_drift_after_a_cached_fill_publishes_nothing(account_io):
    _read()
    later = NOW + timedelta(days=1)
    account_io.routes[BILL_KEY] = _csv("only,a,wrong,header")
    result = _read(clock=lambda: later)
    assert not result.complete and not result.hosts, "no stale coverage under a changed format"
    assert ca.cursor_account_usage_diag()["last_reason"] == "unsupported"


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (ca.AccountFailure("no_login"), "no_login"),
        (ca.AccountFailure("login_encrypted"), "login_encrypted"),
        (ca.AccountFailure("store_unreadable"), "store_unreadable"),
        (RuntimeError(f"store said {SENTINEL}"), "store_unreadable"),
    ],
)
def test_login_failures_fail_closed_without_the_message(account_io, error, cause):
    account_io.login_error = error
    result = _read()
    assert not result.complete and result.reason == "io_error"
    assert ca.cursor_account_usage_diag()["last_reason_detail"]["cause"] == cause
    assert SENTINEL not in ca.CACHE_PATH.read_text()
    assert account_io.calls == []


def test_a_login_within_60_seconds_of_expiry_is_expired_and_never_sent(account_io):
    soon = int(NOW.timestamp()) + 60
    account_io.login = make_jwt(exp=soon)
    result = _read()
    assert not result.complete and account_io.calls == []
    assert ca.cursor_account_usage_diag()["last_reason_detail"] == {"cause": "login_expired"}
    account_io.login = make_jwt(exp=soon + 1)
    assert _read().complete


def test_a_login_without_an_expiry_or_payload_is_unreadable_not_sent(account_io):
    for broken in ("not-a-jwt", f"a.{_b64({'sub': 'x|y'})}.c", f"a.{'!!'}.c"):
        account_io.login = broken
        assert not _read().complete
        assert ca.cursor_account_usage_diag()["last_reason_detail"]["cause"] == "store_unreadable"
    assert account_io.calls == []


def test_a_later_success_clears_the_standing_blocker(account_io):
    account_io.routes[BILL_KEY] = 403
    assert not _read().complete
    assert ca.cursor_account_usage_diag()["last_reason"] == "io_error"
    account_io.routes[BILL_KEY] = (FIXTURES / "billing.csv").read_bytes()
    assert _read().complete
    state = ca.cursor_account_usage_diag()
    assert state["last_reason"] is None and state["last_reason_detail"] is None
    assert state["complete_once"] is True


def test_a_permanent_blocker_survives_a_later_transient_failure(account_io):
    account_io.routes[BILL_KEY] = _csv("only,a,wrong,header")
    _read()
    account_io.routes[BILL_KEY] = 500
    _read()
    state = ca.cursor_account_usage_diag()
    assert state["last_reason"] == "unsupported"
    assert state["last_reason_detail"]["cause"] == "changed_field"


def test_unreadable_cache_detail_cannot_forge_a_blocker(account_io):
    _read()
    data = json.loads(ca.CACHE_PATH.read_text())
    data.update(
        last_reason="io_error",
        last_reason_since="2026-11-03T00:00:00+00:00",
        last_reason_detail={"cause": "forged\nline", "field": "Evil", "status": 99999},
    )
    ca.CACHE_PATH.write_text(json.dumps(data))
    assert ca.cursor_account_usage_diag()["last_reason_detail"] is None
    assert ca.blocker({"cause": "forged"}) is None


def test_locked_cache_returns_locked_without_calls(account_io):
    import fcntl

    ca.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ca.CACHE_PATH.write_text("{}")
    with open(ca.CACHE_PATH) as handle:  # locked_json_rmw flocks the data path itself
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = _read()
    assert not result.complete and result.reason == "locked"
    assert not account_io.touched


def test_the_fix_for_each_cause():
    assert ca.fix_for("login_expired") == "Run cursor-agent login"
    assert ca.fix_for("login_refused") == "Run cursor-agent login"
    assert ca.fix_for("no_login") == "Run cursor-agent login"
    assert ca.fix_for("login_encrypted") == "Run mm disable-source cursor-account"
    assert ca.fix_for("network") == "Run mm push"
    assert ca.fix_for("changed_field") == "Run mm update"


# ── Cursor's login stores, read-only (macOS state DB, Linux auth.json) ─────


def _state_db(path: Path, value, *, wal: bool = False, table: bool = True) -> Path:
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    if table:
        connection.execute(
            "CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)"
        )
        connection.execute("INSERT INTO ItemTable VALUES ('other', 'x')")
        if value is not None:
            connection.execute(
                "INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", (value,)
            )
    connection.commit()
    connection.close()
    return path


def test_app_login_reads_a_plain_jwt_without_touching_the_database(tmp_path):
    folder = tmp_path / "cursor"
    folder.mkdir()
    db = _state_db(folder / "state.vscdb", SENTINEL)
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    assert ca._read_app_login(db) == SENTINEL
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    assert sorted(p.name for p in folder.iterdir()) == ["state.vscdb"]


def test_app_login_reads_an_idle_wal_database_without_creating_sidecars(tmp_path):
    folder = tmp_path / "cursor"
    folder.mkdir()
    db = _state_db(folder / "state.vscdb", SENTINEL, wal=True)
    for sidecar in (db.with_name("state.vscdb-wal"), db.with_name("state.vscdb-shm")):
        sidecar.unlink(missing_ok=True)
    before = db.read_bytes()
    assert ca._read_app_login(db) == SENTINEL
    assert sorted(p.name for p in folder.iterdir()) == ["state.vscdb"]
    assert db.read_bytes() == before


@pytest.mark.parametrize("blob", [SENTINEL.encode("utf-16-le"), SENTINEL.encode()])
def test_app_login_decodes_blob_values(tmp_path, blob):
    assert ca._read_app_login(_state_db(tmp_path / "state.vscdb", blob)) == SENTINEL


def test_an_encrypted_login_fails_closed_with_its_own_cause(tmp_path):
    db = _state_db(tmp_path / "state.vscdb", "cursor-safe-storage:v1:AAAA")
    with pytest.raises(ca.AccountFailure) as caught:
        ca._read_app_login(db)
    assert caught.value.cause == "login_encrypted"
    assert "cursor-safe-storage" not in json.dumps(caught.value.detail)


@pytest.mark.parametrize(
    ("value", "table", "cause"),
    [(None, True, "no_login"), ("   ", True, "no_login"), (None, False, "no_login")],
)
def test_app_login_without_a_session_is_no_login(tmp_path, value, table, cause):
    db = _state_db(tmp_path / "state.vscdb", value, table=table)
    with pytest.raises(ca.AccountFailure) as caught:
        ca._read_app_login(db)
    assert caught.value.cause == cause


def test_app_login_missing_database_is_no_login_and_garbage_is_unreadable(tmp_path):
    with pytest.raises(ca.AccountFailure) as caught:
        ca._read_app_login(tmp_path / "absent.vscdb")
    assert caught.value.cause == "no_login"
    junk = tmp_path / "junk.vscdb"
    junk.write_bytes(b"this is not a database" * 100)
    with pytest.raises(ca.AccountFailure) as caught:
        ca._read_app_login(junk)
    assert caught.value.cause == "store_unreadable"


def test_agent_login_reads_the_access_token_field_and_follows_a_symlink(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"accessToken": f"  {SENTINEL}\n", "refreshToken": "ignored"}))
    link = tmp_path / "auth.json"
    link.symlink_to(real)
    before = real.read_bytes()
    assert ca._read_agent_login(link) == SENTINEL
    assert real.read_bytes() == before


@pytest.mark.parametrize(
    ("content", "cause"),
    [
        (None, "no_login"),
        ('{"accessToken": ""}', "no_login"),
        ('{"other": 1}', "no_login"),
        ('{"accessToken": 5}', "no_login"),
        ("not json", "store_unreadable"),
        ("[1]", "store_unreadable"),
    ],
)
def test_agent_login_failures(tmp_path, content, cause):
    path = tmp_path / "auth.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(ca.AccountFailure) as caught:
        ca._read_agent_login(path)
    assert caught.value.cause == cause


class _FakeSecurity:
    """Stands in for subprocess.run so no test reaches the real Keychain."""

    def __init__(self, returncode=0, stdout=SENTINEL + "\n", stderr="", error=None):
        self.args = []
        self.kwargs = {}
        self.returncode, self.stdout, self.stderr, self.error = returncode, stdout, stderr, error

    def __call__(self, argv, **kwargs):
        self.args.append(list(argv))
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def test_keychain_login_runs_one_fixed_command_and_returns_only_stdout(monkeypatch):
    fake = _FakeSecurity()
    monkeypatch.setattr(ca.subprocess, "run", fake)
    assert ca._read_keychain_login(7.5) == SENTINEL
    assert fake.args == [
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "cursor-access-token",
            "-a",
            "cursor-user",
            "-w",
        ]
    ]
    assert fake.kwargs["timeout"] == 7.5 and fake.kwargs["capture_output"] is True
    assert not fake.kwargs.get("shell")
    ca._read_keychain_login(None)
    assert fake.kwargs["timeout"] == ca._PROMPT_TIMEOUT_S, "enable-source waits for the prompt"


@pytest.mark.parametrize(
    ("fake", "cause"),
    [
        (_FakeSecurity(returncode=44), "no_login"),
        (_FakeSecurity(stdout="\n"), "no_login"),
        (_FakeSecurity(returncode=36, stdout="", stderr=SENTINEL), "store_unreadable"),
        (_FakeSecurity(returncode=1, stdout=SENTINEL), "store_unreadable"),
        (
            _FakeSecurity(error=subprocess.TimeoutExpired(["security"], 1, output=SENTINEL)),
            "store_unreadable",
        ),
        (_FakeSecurity(error=FileNotFoundError(SENTINEL)), "store_unreadable"),
    ],
)
def test_keychain_failures_name_a_cause_and_never_carry_output(monkeypatch, fake, cause):
    monkeypatch.setattr(ca.subprocess, "run", fake)
    with pytest.raises(ca.AccountFailure) as caught:
        ca._read_keychain_login(1.0)
    assert caught.value.cause == cause
    assert SENTINEL not in json.dumps(caught.value.detail) + str(caught.value)
    assert caught.value.__cause__ is None


def _stores(monkeypatch, tmp_path, *, macos, db=None, auth=None, keychain=None):
    monkeypatch.setattr(ca, "_macos", lambda: macos)
    monkeypatch.setattr(ca, "_app_db_path", lambda: tmp_path / "mac.vscdb")
    monkeypatch.setattr(ca, "_linux_db_path", lambda: tmp_path / "linux.vscdb")
    monkeypatch.setattr(ca, "_agent_auth_path", lambda: tmp_path / "auth.json")
    if db is not None:
        _state_db(tmp_path / ("mac.vscdb" if macos else "linux.vscdb"), db)
    if auth is not None:
        (tmp_path / "auth.json").write_text(json.dumps({"accessToken": auth}))
    fake = keychain or _FakeSecurity(returncode=44)
    monkeypatch.setattr(ca.subprocess, "run", fake)
    return fake


def test_macos_prefers_the_cursor_app_database_over_the_keychain(tmp_path, monkeypatch):
    fake = _stores(monkeypatch, tmp_path, macos=True, db="from-app", keychain=_FakeSecurity())
    assert ORIGINAL_READ_LOGIN(None) == "from-app"
    assert fake.args == [], "a plain JWT in the app database needs no Keychain access"


def test_macos_falls_back_to_the_agent_keychain_when_the_app_database_is_absent(
    tmp_path, monkeypatch
):
    fake = _stores(monkeypatch, tmp_path, macos=True, keychain=_FakeSecurity())
    assert ORIGINAL_READ_LOGIN(None) == SENTINEL
    assert len(fake.args) == 1


def test_macos_encrypted_app_token_falls_back_but_is_named_when_nothing_else_works(
    tmp_path, monkeypatch
):
    _stores(monkeypatch, tmp_path, macos=True, db="cursor-safe-storage:v1:AAAA")
    with pytest.raises(ca.AccountFailure) as caught:
        ORIGINAL_READ_LOGIN(None)
    assert caught.value.cause == "login_encrypted"
    _stores(monkeypatch, tmp_path, macos=True, keychain=_FakeSecurity())
    assert ORIGINAL_READ_LOGIN(None) == SENTINEL


def test_macos_with_no_login_anywhere_is_no_login(tmp_path, monkeypatch):
    _stores(monkeypatch, tmp_path, macos=True)
    with pytest.raises(ca.AccountFailure) as caught:
        ORIGINAL_READ_LOGIN(None)
    assert caught.value.cause == "no_login"


def test_linux_reads_the_agent_file_first_then_the_app_database(tmp_path, monkeypatch):
    fake = _stores(monkeypatch, tmp_path, macos=False, auth="from-agent", db="from-app")
    assert ORIGINAL_READ_LOGIN(None) == "from-agent"
    (tmp_path / "auth.json").unlink()
    assert ORIGINAL_READ_LOGIN(None) == "from-app"
    assert fake.args == [], "Linux never runs the Keychain command"


def test_the_linux_config_home_ignores_a_relative_xdg_value(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert ca._agent_auth_path() == tmp_path / "xdg" / "cursor" / "auth.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert ca._agent_auth_path() == Path.home() / ".config" / "cursor" / "auth.json"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert ca._agent_auth_path() == Path.home() / ".config" / "cursor" / "auth.json"


def test_the_macos_database_path_is_cursor_apps_global_storage():
    assert ca._app_db_path() == (
        Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
    )


# ── the login never lands anywhere (criterion 6) ───────────────────────────


def test_login_value_reaches_no_cache_event_or_output(account_io, capsys):
    ok = _read()
    account_io.login_error = RuntimeError(f"boom {SENTINEL}")
    bad = _read()
    assert ok.complete and not bad.complete
    snapshot = events.make_host_usage_snapshot(
        device="dev-a",
        hosts={},
        token_sources=["cursor-account"],
        cursor_account={
            "account": ok.account,
            "since": ok.since,
            "read_day": ok.read_day,
            "through": ok.through,
            "hosts": ok.hosts,
            "tokens_by_day": ok.tokens_by_day,
        },
    )
    out = capsys.readouterr()
    haystack = json.dumps(snapshot) + ca.CACHE_PATH.read_text() + out.out + out.err
    haystack += json.dumps(ca.cursor_account_usage_diag())
    payload = SENTINEL.split(".")[1]
    assert SENTINEL not in haystack and payload not in haystack


def test_a_raising_reader_notice_never_carries_the_login(account_io, capsys):
    account_io.login_error = RuntimeError(f"boom {SENTINEL}")
    readers = events_tail._default_host_readers([], cursor_account_consented=True)
    capture = events_tail._capture_host_usage(
        readers, deadline=time.monotonic() + 30, now=time.monotonic
    )
    out = capsys.readouterr()
    assert SENTINEL not in out.out + out.err + repr(capture)
    assert capture.dropped == (("cursor-account", "io_error"),)


def test_fixtures_hold_no_login_shaped_strings():
    for path in FIXTURES.iterdir():
        text = path.read_text()
        assert SENTINEL not in text
        assert not re.search(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", text), path.name
        assert not re.search(r"WorkosCursorSessionToken=[\w.-]+%3A%3A[\w.-]{12,}", text), path.name


def test_census_returns_names_and_counts_never_the_login(account_io):
    summary = ca.census(deadline=time.monotonic() + 30, now=NOW)
    assert summary["window"] == ["2026-10-31", "2026-11-02"]
    assert summary["rows"] == 8 and summary["billing_item_ids_unique"] == 7
    assert summary["Product"] == {"cursor": 7, "grok-chat": 1}
    assert summary["usage_keys"]["prompt-text-tokens"] == ["str"]
    assert summary["rows_with_counter"]["cache-write-text-tokens"] == 1
    assert summary["date_span"] == ["2026-09-01", "2026-11-02"] and summary["model_count"] == 5
    assert "Billing Item ID" in summary["columns"]
    text = json.dumps(summary)
    assert SENTINEL not in text and "user_synthetic01" not in text and "example.invalid" not in text


def test_check_login_reports_a_cause_and_makes_no_http_call(account_io):
    assert ca.check_login() is None
    account_io.login_error = ca.AccountFailure("no_login")
    assert ca.check_login() == "no_login"
    account_io.login_error = RuntimeError("x")
    assert ca.check_login() == "store_unreadable"
    account_io.login_error = None
    account_io.login = make_jwt(exp=1)
    assert ca.check_login() == "login_expired"
    assert account_io.calls == []


# ── adapter contract rows (tests/test_host_adapter_contract.py clauses) ────


def test_registry_and_diag_completeness():
    row = "cursor-account"
    assert tuple(host_registry.HOST_READER_DIAGS) == events.ACTIVE_HOST_READERS
    assert events.ACTIVE_HOST_READERS[-1] == row and events.HOST_USAGE_TOKEN_SOURCES[-1] == row
    assert row not in events_tail.WARMABLE_HOST_READERS
    assert events_tail.HOST_READER_SOURCE_GATE[row] is None
    spec = host_registry.HOST_READER_DIAGS[row]
    assert callable(getattr(spec.owner, spec.function))
    state = host_registry.reader_usage_diag(row)
    required = {
        "cache_state",
        "last_reason",
        "last_reason_since",
        "last_reason_detail",
        "model_count",
        "models",
        "complete_once",
        "last_complete_ms",
        "last_complete_at",
    }
    assert required <= set(state)
    assert host_registry.reader_cache_cold(row, state) is True
    assert host_registry.reader_cache_cold(row, {**state, "complete_once": True}) is False


def test_call_time_owner_lookup_reaches_the_dispatch_sites(monkeypatch):
    sentinel = host_usage.HostUsageResult({"other": {}}, complete=True)
    monkeypatch.setattr(ca, "read_cursor_account_usage", lambda *_a, **_kw: sentinel)
    monkeypatch.setattr(ca, "cursor_account_usage_diag", lambda: {"cache_state": "sentinel"})
    readers = dict(events_tail._default_host_readers([], cursor_account_consented=True))
    assert readers["cursor-account"](deadline=1000.0) is sentinel
    assert host_registry.reader_usage_diag("cursor-account") == {"cache_state": "sentinel"}


def test_path_globals_resolve_under_tmp_path(tmp_path):
    paths = {k: v for k, v in vars(ca).items() if isinstance(v, Path)}
    assert paths, "expected CACHE_PATH on the adapter"
    for name, value in paths.items():
        if name == "FIXTURES":
            continue
        assert value.is_relative_to(tmp_path), f"cursor_account.{name}={value}"


def test_fresh_import_never_loads_cli():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, mind_meld.hosts.cursor_account;"
            " assert 'mind_meld.cli' not in sys.modules, list(sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


# ── capture wiring (criteria 7, 8, 9, 14) ──────────────────────────────────


def test_default_readers_gate_the_account_reader_on_its_own_consent():
    assert events_tail._default_host_readers([], cursor_consented=True) != ()
    names = [n for n, _ in events_tail._default_host_readers([], cursor_consented=True)]
    assert names == ["cursor"]
    both = [
        n
        for n, _ in events_tail._default_host_readers(
            [{"name": "codex", "type": "generic"}],
            cursor_consented=True,
            cursor_account_consented=True,
        )
    ]
    assert both == ["codex", "cursor", "cursor-account"]


def _good_reader(family, day="2026-11-02"):
    usage = {"input": 5, "cache_create": 0, "cache_read": 0, "output": 1}

    def read(*, deadline):
        return host_usage.HostUsageResult(
            {family: {day: dict(usage)}},
            complete=True,
            tokens_by_day={day: {**usage, "by_model": {f"{family}-m": dict(usage)}}},
        )

    return read


def test_account_totals_travel_apart_from_the_machine_totals(account_io):
    readers = (
        ("cursor", _good_reader("other")),
        ("cursor-account", lambda *, deadline: _read(deadline=deadline)),
    )
    capture = events_tail._capture_host_usage(readers, deadline=time.monotonic() + 30)
    assert capture.token_sources == ("cursor", "cursor-account")
    assert set(capture.hosts) == {"other"}, "account totals must not merge into hosts"
    assert capture.hosts["other"]["2026-11-02"]["input"] == 5
    assert capture.cursor_usage["hosts"] == capture.hosts
    assert capture.cursor_account["since"] == "2026-10-04"
    assert capture.cursor_account["through"] == "2026-11-02"
    assert set(capture.cursor_account["hosts"]) == {"grok", "claude", "codex", "other"}
    assert capture.usage_days["cursor-account"]


def test_the_account_reader_keeps_a_time_floor_when_earlier_readers_overran():
    seen: dict[str, float] = {}

    def slow_first(*, deadline):
        return host_usage.HostUsageResult({}, complete=True)

    def account(*, deadline):
        seen["left"] = deadline - clock[0]
        return host_usage.HostUsageResult({}, complete=True)

    clock = [0.0]

    def now():
        return clock[0]

    def first(*, deadline):
        clock[0] = 2.9  # the local readers used nearly the whole 3 s host phase
        return slow_first(deadline=deadline)

    events_tail._capture_host_usage(
        (("codex", first), ("cursor-account", account)), deadline=3.0, now=now
    )
    assert seen["left"] == pytest.approx(events_tail.HOST_READER_FLOOR_MS["cursor-account"] / 1000)
    assert events_tail.HOST_READER_FLOOR_MS["cursor-account"] >= 2000


def test_a_failing_account_read_leaves_the_other_readers_published(account_io):
    account_io.routes[BILL_KEY] = 500
    readers = (
        ("codex", _good_reader("codex")),
        ("cursor-account", lambda *, deadline: _read(deadline=deadline)),
    )
    capture = events_tail._capture_host_usage(readers, deadline=time.monotonic() + 30)
    assert capture.complete and capture.token_sources == ("codex",)
    assert capture.dropped == (("cursor-account", "io_error"),)
    assert capture.cursor_account is None and capture.hosts["codex"]


def test_warm_retry_merge_carries_both_new_fields():
    initial = events_tail.HostUsageCapture(
        {}, cursor_usage={"hosts": {}, "tokens_by_day": {}}, token_sources=("cursor",)
    )
    retry = events_tail.HostUsageCapture(
        {}, cursor_account={"account": "a" * 64}, token_sources=("cursor-account",)
    )
    merged = events_tail._merge_warm_retry_capture(
        initial,
        retry,
        readers=(("cursor", None), ("cursor-account", None)),
        retried_names={"cursor-account"},
    )
    assert merged.cursor_usage == {"hosts": {}, "tokens_by_day": {}}
    assert merged.cursor_account == {"account": "a" * 64}


_DAY = "2026-11-02"
_USAGE = {"input": 5, "cache_create": 0, "cache_read": 0, "output": 1}
_ACCOUNT = {
    "account": "a" * 64,
    "since": "2026-10-05",
    "read_day": "2026-11-03",
    "through": _DAY,
    "hosts": {"grok": {_DAY: dict(_USAGE)}},
    "tokens_by_day": {_DAY: {**_USAGE, "by_model": {"grok-4.7": dict(_USAGE)}}},
}


def test_snapshot_without_the_new_fields_is_unchanged():
    kwargs = dict(
        device="dev-a",
        hosts={"codex": {_DAY: dict(_USAGE)}},
        token_sources=["codex"],
        tokens_by_day={_DAY: {**_USAGE, "by_model": {"gpt-5": dict(_USAGE)}}},
        ts=NOW,
    )
    row = events.make_host_usage_snapshot(**kwargs)
    assert "cursor_usage" not in row and "cursor_account" not in row
    assert row == events.make_host_usage_snapshot(**kwargs, cursor_usage=None, cursor_account=None)


def test_snapshot_field_shapes_are_pinned():
    own = {
        "hosts": {"other": {_DAY: dict(_USAGE)}, "grok": {"2020-01-01": dict(_USAGE)}},
        "tokens_by_day": {_DAY: {**_USAGE, "by_model": {"zeta": dict(_USAGE)}}},
    }
    row = events.make_host_usage_snapshot(
        device="dev-a",
        hosts={"other": {_DAY: dict(_USAGE)}},
        token_sources=["cursor", "cursor-account"],
        tokens_by_day={_DAY: {**_USAGE, "by_model": {"zeta": dict(_USAGE)}}},
        usage_days={"cursor": [_DAY], "cursor-account": [_DAY]},
        cursor_usage=own,
        cursor_account=_ACCOUNT,
        ts=NOW,
    )
    assert set(row["cursor_usage"]) == {"hosts", "tokens_by_day"}
    assert row["cursor_usage"]["hosts"] == {"other": {_DAY: _USAGE}}, "limited to kept days"
    assert set(row["cursor_account"]) == {
        "account",
        "since",
        "read_day",
        "through",
        "hosts",
        "tokens_by_day",
    }
    assert row["cursor_account"]["through"] == _DAY
    assert row["cursor_account"]["hosts"] == _ACCOUNT["hosts"]
    assert row["hosts"] == {"other": {_DAY: _USAGE}}, "account totals never enter hosts"
    assert row["empty_sources"] == []


def test_empty_sources_for_the_account_reader_uses_its_own_days():
    row = events.make_host_usage_snapshot(
        device="dev-a",
        hosts={"codex": {_DAY: dict(_USAGE)}},
        token_sources=["codex", "cursor-account"],
        tokens_by_day={_DAY: {**_USAGE, "by_model": {"gpt-5": dict(_USAGE)}}},
        usage_days={"codex": [_DAY], "cursor-account": []},
        cursor_account={**_ACCOUNT, "hosts": {}, "tokens_by_day": {}},
        ts=NOW,
    )
    assert row["empty_sources"] == ["cursor-account"]
    assert row["cursor_account"]["hosts"] == {}


# ── consent surfaces (criteria 4, 14) ──────────────────────────────────────


def _config(tmp_path, monkeypatch, retro=None):
    storage = tmp_path / "store"
    storage.mkdir(exist_ok=True)
    config = {
        "device": {"id": "dev-a", "name": "Dev A"},
        "storage": {"path": str(storage)},
        "sync": {"max_file_size": 52_428_800, "sources": []},
        "crypto": {"argon2_memory_kb": 1024},
    }
    if retro:
        config["retro"] = retro
    path = tmp_path / "config.toml"
    mm_config.save_config(config, path)
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", path)
    monkeypatch.setattr("mind_meld.seen_sources.SEEN_DIR", tmp_path / "mm_state")
    return path


def test_consent_is_off_by_default_and_must_be_boolean(tmp_path, monkeypatch):
    path = _config(tmp_path, monkeypatch)
    assert mm_config.cursor_account_usage_enabled(mm_config.load_config()) is False
    _config(tmp_path, monkeypatch, {"cursor_account_usage": "yes"})
    with pytest.raises(mm_config.ConfigError, match="retro.cursor_account_usage must be a boolean"):
        mm_config.load_config()
    assert path.exists()


def test_enable_and_disable_touch_only_their_own_key(tmp_path, monkeypatch, account_io):
    import tomllib

    path = _config(tmp_path, monkeypatch, {"cursor_host_usage": True, "author_emails": ["a@b.c"]})
    result = runner.invoke(app, ["enable-source", "cursor-account"])
    assert result.exit_code == 0, result.output
    retro = tomllib.loads(path.read_text())["retro"]
    assert retro == {
        "cursor_host_usage": True,
        "cursor_account_usage": True,
        "author_emails": ["a@b.c"],
    }
    assert account_io.calls == [] and account_io.login_calls == 1, "one presence check, no network"
    again = runner.invoke(app, ["enable-source", "cursor-account"])
    assert "already enabled" in again.output and account_io.login_calls == 1 + 1
    off = runner.invoke(app, ["disable-source", "cursor-account"])
    assert off.exit_code == 0, off.output
    retro = tomllib.loads(path.read_text())["retro"]
    assert retro["cursor_account_usage"] is False and retro["cursor_host_usage"] is True
    assert account_io.calls == []


def test_cursor_toggle_never_grants_account_consent(tmp_path, monkeypatch):
    import tomllib

    path = _config(tmp_path, monkeypatch)
    assert runner.invoke(app, ["enable-source", "cursor"]).exit_code == 0
    retro = tomllib.loads(path.read_text())["retro"]
    assert retro.get("cursor_host_usage") is True and "cursor_account_usage" not in retro


def test_enable_with_an_unreadable_login_ends_on_the_fix(tmp_path, monkeypatch, account_io):
    _config(tmp_path, monkeypatch)
    account_io.login_error = ca.AccountFailure("no_login")
    result = runner.invoke(app, ["enable-source", "cursor-account"])
    assert result.exit_code == 0, result.output
    last = " ".join((result.output + result.stderr).split())
    assert last.endswith("Run cursor-agent login.")


def test_cursor_account_is_a_known_source_name(tmp_path, monkeypatch):
    from mind_meld.sourceflow import _known_source_names

    _config(tmp_path, monkeypatch)
    assert "cursor-account" in _known_source_names(mm_config.load_config())


def test_consent_remedy_names_the_account_command_and_leaves_cursor_unchanged():
    assert "mm enable-source cursor-account" in mm_config.host_usage_consent_remedy(
        "cursor-account", enabled=True
    )
    assert "completion hook" in mm_config.host_usage_consent_remedy("cursor", enabled=True)


# ── call sites that must never reach the network (criteria 7, 14) ──────────


def _tail_sources(tmp_path):
    return [
        {
            "name": "mm-events",
            "path": str(tmp_path / "events_root"),
            "type": "generic",
            "include_dirs": ["events"],
            "exclude_patterns": [],
        }
    ]


@pytest.mark.parametrize("quiet", [True, False])
def test_only_the_non_quiet_tail_selects_the_account_reader(tmp_path, monkeypatch, quiet):
    from mind_meld import identity as mm_identity

    sources = _tail_sources(tmp_path)
    config = {"sync": {"sources": sources}, "retro": {"cursor_account_usage": True}}
    seen: dict = {}
    result = events_tail.CaptureResult(
        git_rows=[],
        session_rows=[],
        host_rows=[],
        host_capture=events_tail.HostUsageCapture({}),
        root_discovery=events.GitRootDiscovery((), (), False),
        discovery_errors=[],
        session_walk_exceeded_budget=False,
        warn_lock_unavailable=False,
        token_cache_requested=False,
    )
    monkeypatch.setattr(
        events,
        "resolve_push_cursor",
        lambda *_a, **_k: events.CursorResolution(since=NOW),
    )
    monkeypatch.setattr(
        events_tail, "_capture_event_snapshots", lambda *_a, **kw: seen.update(kw) or result
    )
    monkeypatch.setattr(
        mm_identity,
        "gather_local_identities",
        lambda *, allow_refresh=True, root_discovery=None: [],
    )
    monkeypatch.setattr(events, "write_push_event", lambda *_a, **_k: None)
    events_tail._run_events_tail(config, sources, "dev-a", dry_run=False, quiet=quiet)
    names = [name for name, _ in seen["host_readers"]]
    assert ("cursor-account" in names) is (not quiet)


def test_init_backfill_never_selects_the_account_reader():
    source = Path(events_tail.__file__).read_text()
    backfill = source[source.index("def _run_events_backfill") :]
    assert "cursor_account_consented" not in backfill


def test_status_diag_and_publication_never_touch_login_or_network(
    tmp_path, monkeypatch, account_io
):
    from tests.test_diag import PASSPHRASE, _enable_capture_sources, _setup

    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    _enable_capture_sources(tmp_path, readers=())
    cfg = mm_config.load_config()
    cfg.setdefault("retro", {}).update(cursor_host_usage=True, cursor_account_usage=True)
    mm_config.save_config(cfg)
    for args in (["status"], ["diag"], ["diag", "--json"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
    assert not account_io.touched


# ── status, diag and the ACP gap notice (criteria 9, 11) ───────────────────


def _status_setup(tmp_path, monkeypatch, *, account=None, cursor_reason=None):
    from tests.test_diag import PASSPHRASE, _enable_capture_sources, _setup

    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    _enable_capture_sources(tmp_path, readers=())
    cfg = mm_config.load_config()
    retro = cfg.setdefault("retro", {})
    retro["cursor_host_usage"] = True
    retro["cursor_account_usage"] = account is not None
    mm_config.save_config(cfg)
    monkeypatch.setattr(host_usage, "cursor_hook_state", lambda: "installed")
    monkeypatch.setattr(ca, "_macos", lambda: False)
    cursor_cache = {
        "version": host_usage.CACHE_VERSION,
        "runs": {},
        "complete_once": True,
        **(
            {"last_reason": cursor_reason, "last_reason_since": "2026-11-01T00:00:00+00:00"}
            if cursor_reason
            else {}
        ),
    }
    host_usage.CURSOR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    host_usage.CURSOR_CACHE_PATH.write_text(json.dumps(cursor_cache))
    if account is not None:
        ca.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ca.CACHE_PATH.write_text(json.dumps({"version": host_usage.CACHE_VERSION, **account}))


def _status(tmp_path, monkeypatch, **kwargs):
    _status_setup(tmp_path, monkeypatch, **kwargs)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())


ACP = "Cursor runs through ACP front-ends such as Paseo are not counted"
_WORKING = {
    "complete_once": True,
    "last_complete_at": "2026-11-03T10:00:00+00:00",
    "newest_event_day": "2026-11-02",
}
_FAILING = {
    "complete_once": False,
    "last_reason": "io_error",
    "last_reason_since": "2026-11-03T10:00:00+00:00",
    "last_reason_detail": {"cause": "login_refused"},
}


def test_notice_shows_with_the_reader_off_beside_a_successful_scan(tmp_path, monkeypatch):
    plain = _status(tmp_path, monkeypatch)
    assert "a prior scan completed successfully" in plain and ACP in plain
    assert "run mm enable-source cursor-account to count them" in plain
    assert "Cursor account usage:" not in plain


def test_notice_shows_with_the_reader_off_beside_a_reader_blocker(tmp_path, monkeypatch):
    plain = _status(tmp_path, monkeypatch, cursor_reason="io_error")
    assert ACP in plain and "mm enable-source cursor-account" in plain


def test_notice_remains_until_account_local_inclusion_is_proven(tmp_path, monkeypatch):
    plain = _status(tmp_path, monkeypatch, account=_WORKING)
    assert ACP in plain and "inclusion is unproven" in plain
    assert "Cursor account usage: enabled; last complete read" in plain
    assert "fleet account totals held: local inclusion is unproven" in plain


def test_a_working_account_reader_with_an_old_ledger_keeps_the_acp_notice(tmp_path, monkeypatch):
    stale = {**_WORKING, "newest_event_day": "2026-10-21"}
    plain = _status(tmp_path, monkeypatch, account=stale)
    assert ACP in plain and "the Cursor account's usage is only read through 2026-10-21" in plain
    assert "account usage through 2026-10-21" in plain


def test_a_working_account_reader_shows_its_newest_event_day(tmp_path, monkeypatch):
    plain = _status(tmp_path, monkeypatch, account=_WORKING)
    assert "account usage through 2026-11-02" in plain and ACP in plain


def test_failing_account_reader_names_what_broke_and_the_fix_last(tmp_path, monkeypatch):
    plain = _status(tmp_path, monkeypatch, account=_FAILING)
    assert ACP in plain and "until Cursor account usage reads successfully" in plain
    line = plain[plain.index("Cursor account usage:") :]
    assert line.startswith(
        "Cursor account usage: enabled, failing — cursor.com refused the Cursor login. "
        "Run cursor-agent login."
    )


def test_enabled_without_a_read_yet_points_at_push(tmp_path, monkeypatch):
    plain = _status(tmp_path, monkeypatch, account={"complete_once": False})
    assert "Cursor account usage: enabled, but no successful read yet — run mm push." in plain
    assert ACP in plain


def test_diag_shows_the_blocker_and_json_the_reader_state(tmp_path, monkeypatch):
    _status_setup(tmp_path, monkeypatch, account=_FAILING)
    plain = " ".join(runner.invoke(app, ["diag"]).output.split())
    assert "Cursor account:" in plain and "consented: yes" in plain
    assert "usage read blocker: enabled, failing — cursor.com refused" in plain
    state = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_usage"][
        "cursor-account"
    ]
    assert state["consented"] is True and state["last_reason"] == "io_error"
    assert state["last_reason_detail"] == {"cause": "login_refused"}
    assert SENTINEL not in json.dumps(state)


def test_diag_json_reports_the_reader_off_by_default(tmp_path, monkeypatch):
    _status_setup(tmp_path, monkeypatch)
    state = json.loads(runner.invoke(app, ["diag", "--json"]).stdout)["host_usage"][
        "cursor-account"
    ]
    assert state["consented"] is False and state["complete_once"] is False
