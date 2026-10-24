#!/usr/bin/env python3

import email.utils
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 9128
VERSION = "2.4.5"
SERVER_TITLE = "JacRed TV + Movies"
CACHE_TTL_SECONDS = max(1, int(os.environ.get("JACRED_CACHE_TTL_SECONDS", "180")))
CACHE_MAX_ENTRIES = max(1, int(os.environ.get("JACRED_CACHE_MAX_ENTRIES", "512")))
JACRED_SOURCE_CACHE_TTL_SECONDS = max(1, int(os.environ.get("JACRED_SOURCE_CACHE_TTL_SECONDS", "180")))
JACRED_SOURCE_CACHE_MAX_ENTRIES = max(1, int(os.environ.get("JACRED_SOURCE_CACHE_MAX_ENTRIES", "128")))
MIN_UPSTREAM_INTERVAL_FLOOR_MS = 1000
_requested_min_interval_ms = max(0, int(os.environ.get("JACRED_MIN_INTERVAL_MS", "1000")))
JACRED_MIN_INTERVAL_MS = max(MIN_UPSTREAM_INTERVAL_FLOOR_MS, _requested_min_interval_ms)
if _requested_min_interval_ms < MIN_UPSTREAM_INTERVAL_FLOOR_MS:
    print("WARNING JACRED_MIN_INTERVAL_MS below the enforced 1000 ms floor; using 1000 ms", file=sys.stderr, flush=True)
JACRED_429_BACKOFF_SECONDS = max(1, int(os.environ.get("JACRED_429_BACKOFF_SECONDS", "60")))
JACRED_429_BACKOFF_STEPS_SECONDS = tuple(
    max(1, int(value.strip()))
    for value in os.environ.get("JACRED_429_BACKOFF_STEPS_SECONDS", "60,300,900,3600,21600").split(",")
    if value.strip()
) or (60, 300, 900, 3600, 21600)
JACRED_RECOVERY_INTERVALS_SECONDS = tuple(
    max(5, int(value.strip()))
    for value in os.environ.get("JACRED_RECOVERY_INTERVALS_SECONDS", "60,30,15,10,5").split(",")
    if value.strip()
) or (60, 30, 15, 10, 5)
JACRED_RECOVERY_SUCCESS_COUNT = max(1, int(os.environ.get("JACRED_RECOVERY_SUCCESS_COUNT", "3")))
JACRED_STABLE_SUCCESS_COUNT = max(1, int(os.environ.get("JACRED_STABLE_SUCCESS_COUNT", "15")))
JACRED_PROBE_FAILURE_SECONDS = max(60, int(os.environ.get("JACRED_PROBE_FAILURE_SECONDS", "300")))
JACRED_MAX_QUEUE_WAIT_SECONDS = max(1, int(os.environ.get("JACRED_MAX_QUEUE_WAIT_SECONDS", "10")))
JACRED_STATE_PATH = os.environ.get("JACRED_STATE_PATH", "/var/lib/jacred-v2-adapter/upstream_state.sqlite3").strip() or "/var/lib/jacred-v2-adapter/upstream_state.sqlite3"
JACRED_USER_AGENT = os.environ.get(
    "JACRED_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36",
).strip()
REQUEST_PARAMS_LOGGING = os.environ.get("JACRED_LOG_REQUEST_PARAMS", "0").strip().lower() in {"1", "true", "yes", "on"}
JACRED_URL = os.environ.get(
    "JACRED_URL",
    "http://127.0.0.1:9117/api/v2.0/indexers/all/results",
).strip() or "http://127.0.0.1:9117/api/v2.0/indexers/all/results"
JACRED_URL = JACRED_URL.rstrip("?&")

# Shared Apprise API notifications; no Telegram credentials are stored in this adapter.
JACRED_NOTIFICATIONS_ENABLED = os.environ.get(
    "JACRED_NOTIFICATIONS_ENABLED", "1"
).strip().lower() not in {"0", "false", "no", "off"}
JACRED_APPRISE_URL = os.environ.get(
    "JACRED_APPRISE_URL", "http://192.168.0.14:8000"
).strip().rstrip("/")
JACRED_APPRISE_CONFIG_KEY = os.environ.get(
    "JACRED_APPRISE_CONFIG_KEY", "arr-stack"
).strip()
JACRED_NOTIFICATION_TAG_WARNINGS = os.environ.get(
    "JACRED_NOTIFICATION_TAG_WARNINGS", "warnings"
).strip()
JACRED_NOTIFICATION_TAG_CRITICAL = os.environ.get(
    "JACRED_NOTIFICATION_TAG_CRITICAL", "critical"
).strip()
JACRED_NOTIFICATION_TIMEOUT_SECONDS = max(
    1, min(30, int(os.environ.get("JACRED_NOTIFICATION_TIMEOUT_SECONDS", "5")))
)
NOTIFICATION_STATE_LOCK = threading.Lock()
NOTIFICATION_STATE_MEMORY = {}

TORZNAB_NS = "http://torznab.com/schemas/2015/feed"

TV_CATEGORIES = {5000, 5070}
MOVIE_CATEGORIES = {2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090}

TEST_INFOHASH = "0000000000000000000000000000000000000001"
TEST_TV_INFOHASH = "0000000000000000000000000000000000000002"

# Cache complete Torznab responses for a short period. The cache key contains
# every query parameter that can affect the adapter output; the API key is
# deliberately ignored so identical searches from different clients share
# one cached response.
CACHE_LOCK = threading.Lock()
CACHE = {}
INFLIGHT_LOCKS = {}

# Cache raw JacRed result sets independently of rendered Torznab pages.
# Different offsets/category/season filters can reuse the same source results.
SOURCE_CACHE_LOCK = threading.Lock()
SOURCE_CACHE = {}
SOURCE_INFLIGHT_LOCKS = {}

# Global upstream limiter shared by all HTTP handler threads.
RATE_LIMIT_LOCK = threading.Lock()
UPSTREAM_REQUEST_LOCK = threading.Lock()
LAST_UPSTREAM_COMPLETION = 0.0


class UpstreamCoolingDown(Exception):
    def __init__(self, retry_after_seconds, message):
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))


PROTECTION_LOCK = threading.RLock()
PROTECTION_FIELDS = (
    "state", "block_until", "consecutive_429", "consecutive_403",
    "consecutive_failures", "failure_reason", "recovery_stage",
    "recovery_successes", "stable_successes", "manual_review", "updated_at",
)


def _protection_connection():
    db_path = os.path.abspath(JACRED_STATE_PATH)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS upstream_protection (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            state TEXT NOT NULL,
            block_until REAL NOT NULL DEFAULT 0,
            consecutive_429 INTEGER NOT NULL DEFAULT 0,
            consecutive_403 INTEGER NOT NULL DEFAULT 0,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            failure_reason TEXT NOT NULL DEFAULT '',
            recovery_stage INTEGER NOT NULL DEFAULT 0,
            recovery_successes INTEGER NOT NULL DEFAULT 0,
            stable_successes INTEGER NOT NULL DEFAULT 0,
            manual_review INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO upstream_protection
        (singleton, state, block_until, consecutive_429, consecutive_403,
         consecutive_failures, failure_reason, recovery_stage,
         recovery_successes, stable_successes, manual_review, updated_at)
        VALUES (1, 'CLOSED', 0, 0, 0, 0, '', 0, 0, 0, 0, ?)
        """,
        (time.time(),),
    )
    connection.commit()
    return connection


def read_protection_state():
    with PROTECTION_LOCK:
        connection = _protection_connection()
        try:
            row = connection.execute(
                "SELECT * FROM upstream_protection WHERE singleton = 1"
            ).fetchone()
            return dict(row)
        finally:
            connection.close()


def update_protection_state(**updates):
    allowed = set(PROTECTION_FIELDS) - {"updated_at"}
    unexpected = set(updates) - allowed
    if unexpected:
        raise ValueError("Unexpected protection-state fields: " + ", ".join(sorted(unexpected)))

    with PROTECTION_LOCK:
        connection = _protection_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM upstream_protection WHERE singleton = 1"
            ).fetchone()
            state = dict(row)
            state.update(updates)
            state["manual_review"] = int(bool(state["manual_review"]))
            state["updated_at"] = time.time()
            connection.execute(
                """
                UPDATE upstream_protection SET
                    state = ?, block_until = ?, consecutive_429 = ?,
                    consecutive_403 = ?, consecutive_failures = ?,
                    failure_reason = ?, recovery_stage = ?,
                    recovery_successes = ?, stable_successes = ?,
                    manual_review = ?, updated_at = ?
                WHERE singleton = 1
                """,
                tuple(state[field] for field in PROTECTION_FIELDS),
            )
            connection.commit()
            return state
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def reset_protection_state():
    return update_protection_state(
        state="CLOSED",
        block_until=0,
        consecutive_429=0,
        consecutive_403=0,
        consecutive_failures=0,
        failure_reason="",
        recovery_stage=0,
        recovery_successes=0,
        stable_successes=0,
        manual_review=0,
    )


def initialize_protection_state():
    state = read_protection_state()
    if state["state"] == "HALF_OPEN":
        # A process restart must never leave a phantom probe in progress.
        # Keep the block intact and add a short safety pause before a new probe.
        next_probe = max(float(state["block_until"] or 0), time.time() + JACRED_PROBE_FAILURE_SECONDS)
        update_protection_state(
            state="OPEN",
            block_until=next_probe,
            failure_reason=state["failure_reason"] or "service restarted during HALF_OPEN probe",
        )
        print(
            "WARNING persisted HALF_OPEN state recovered as OPEN "
            f"block_until={datetime.fromtimestamp(next_probe, timezone.utc).isoformat()}",
            file=sys.stderr,
            flush=True,
        )


def parse_retry_after(exc):
    headers = getattr(exc, "headers", None)
    value = clean(headers.get("Retry-After", "") if headers else "")
    if value:
        try:
            return max(1.0, float(value))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(1.0, (parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return float(JACRED_429_BACKOFF_SECONDS)


def _safe_http_error_details(exc):
    headers = getattr(exc, "headers", None)
    safe_headers = {}
    for name in ("Date", "Server", "Retry-After", "Content-Type", "CF-Ray", "X-Request-ID"):
        value = clean(headers.get(name, "") if headers else "")
        if value:
            safe_headers[name.lower()] = value[:160]

    try:
        body = exc.read(512).decode("utf-8", errors="replace")
    except Exception:
        body = ""
    body = re.sub(r"\s+", " ", body).strip()
    body = re.sub(
        r"(?i)\b(api[_-]?key|token|authorization|cookie|password)\b\s*[:=]\s*[^ ,;]+",
        r"\1=[REDACTED]",
        body,
    )[:300]
    return safe_headers, body


def register_http_429(retry_after_seconds, exc=None):
    state = read_protection_state()
    stage = int(state["consecutive_429"]) + 1
    configured = JACRED_429_BACKOFF_STEPS_SECONDS[
        min(stage - 1, len(JACRED_429_BACKOFF_STEPS_SECONDS) - 1)
    ]
    delay = max(float(configured), float(retry_after_seconds or 0))
    now = time.time()
    block_until = now + delay
    reason = f"HTTP 429 (stage {stage})"
    update_protection_state(
        state="OPEN",
        block_until=block_until,
        consecutive_429=stage,
        consecutive_failures=int(state["consecutive_failures"]) + 1,
        failure_reason=reason,
        recovery_stage=0,
        recovery_successes=0,
        stable_successes=0,
        manual_review=state["manual_review"],
    )
    print(
        f"WARNING upstream circuit OPEN reason={reason} "
        f"delay_seconds={delay:.0f} block_until={datetime.fromtimestamp(block_until, timezone.utc).isoformat()}",
        file=sys.stderr,
        flush=True,
    )
    return UpstreamCoolingDown(math.ceil(delay), f"JacRed {reason}; all upstream requests paused")


def register_http_403(exc):
    state = read_protection_state()
    count = int(state["consecutive_403"]) + 1
    policy_delay = 86400 if count == 1 else 604800
    try:
        requested_delay = parse_retry_after(exc)
    except Exception:
        requested_delay = 0
    delay = max(float(policy_delay), float(requested_delay))
    now = time.time()
    block_until = now + delay
    manual_review = count >= 2
    reason = "HTTP 403; manual review required" if manual_review else "HTTP 403; 24-hour protection block"
    safe_headers, body_excerpt = _safe_http_error_details(exc)
    update_protection_state(
        state="OPEN",
        block_until=block_until,
        consecutive_403=count,
        consecutive_failures=int(state["consecutive_failures"]) + 1,
        failure_reason=reason,
        recovery_stage=0,
        recovery_successes=0,
        stable_successes=0,
        manual_review=manual_review,
    )
    print(
        "CRITICAL upstream HTTP 403 "
        f"timestamp={datetime.now(timezone.utc).isoformat()} "
        f"headers={json.dumps(safe_headers, sort_keys=True)} "
        f"body_excerpt={body_excerpt!r} "
        f"block_until_utc={datetime.fromtimestamp(block_until, timezone.utc).isoformat()} "
        f"manual_review={str(manual_review).lower()}",
        file=sys.stderr,
        flush=True,
    )
    message = "JacRed HTTP 403; upstream circuit OPEN"
    if manual_review:
        message += " for 7 days and then remains blocked pending manual review"
    else:
        message += " for at least 24 hours"
    return UpstreamCoolingDown(math.ceil(delay), message)


def register_http_401(exc):
    state = read_protection_state()
    safe_headers, body_excerpt = _safe_http_error_details(exc)
    reason = "HTTP 401; manual authentication/configuration review required"
    update_protection_state(
        state="OPEN",
        block_until=0,
        consecutive_failures=int(state["consecutive_failures"]) + 1,
        failure_reason=reason,
        recovery_stage=0,
        recovery_successes=0,
        stable_successes=0,
        manual_review=1,
    )
    print(
        "CRITICAL upstream HTTP 401 "
        f"timestamp={datetime.now(timezone.utc).isoformat()} "
        f"headers={json.dumps(safe_headers, sort_keys=True)} "
        f"body_excerpt={body_excerpt!r} manual_review=true",
        file=sys.stderr,
        flush=True,
    )
    return UpstreamCoolingDown(
        max(86400, math.ceil(parse_retry_after(exc))),
        "JacRed HTTP 401; upstream requests disabled until manual authentication/configuration review",
    )


def register_other_failure(reason, probe=False):
    state = read_protection_state()
    safe_reason = re.sub(r"\s+", " ", str(reason)).strip()[:240]
    is_probe = bool(probe or state["state"] == "HALF_OPEN")
    if is_probe:
        block_until = time.time() + JACRED_PROBE_FAILURE_SECONDS
        next_state = "OPEN"
        safe_reason = "HALF_OPEN probe failed: " + safe_reason
    else:
        block_until = float(state["block_until"] or 0)
        next_state = state["state"]
    updated = update_protection_state(
        state=next_state,
        block_until=block_until,
        consecutive_failures=int(state["consecutive_failures"]) + 1,
        failure_reason=safe_reason,
        recovery_stage=0,
        recovery_successes=0,
        stable_successes=0,
        manual_review=state["manual_review"],
    )
    print(
        f"ERROR upstream request failed reason={safe_reason!r} state={updated['state']} "
        f"block_until={updated['block_until']:.0f}",
        file=sys.stderr,
        flush=True,
    )
    if is_probe:
        return UpstreamCoolingDown(
            JACRED_PROBE_FAILURE_SECONDS,
            "JacRed recovery probe failed; upstream circuit temporarily OPEN",
        )
    return None


def register_upstream_success(probe=False):
    state = read_protection_state()
    was_probe = bool(probe or state["state"] == "HALF_OPEN")
    recovering = bool(
        was_probe
        or int(state["consecutive_429"])
        or int(state["consecutive_403"])
        or str(state["failure_reason"]).startswith("HALF_OPEN probe failed:")
        or int(state["recovery_stage"])
        or int(state["recovery_successes"])
    )

    if was_probe:
        recovery_stage = 0
        recovery_successes = 0
    elif recovering:
        recovery_stage = int(state["recovery_stage"])
        recovery_successes = int(state["recovery_successes"]) + 1
        if recovery_successes >= JACRED_RECOVERY_SUCCESS_COUNT:
            recovery_stage = min(recovery_stage + 1, len(JACRED_RECOVERY_INTERVALS_SECONDS) - 1)
            recovery_successes = 0
    else:
        # Ordinary successful requests in CLOSED do not count as recovery
        # progress and must not activate the 60-second recovery interval.
        recovery_stage = 0
        recovery_successes = 0

    stable_successes = int(state["stable_successes"]) + 1
    clear_escalation = stable_successes >= JACRED_STABLE_SUCCESS_COUNT
    if clear_escalation:
        stable_successes = 0

    updated = update_protection_state(
        state="CLOSED",
        block_until=0,
        consecutive_failures=0,
        failure_reason="" if clear_escalation else state["failure_reason"],
        consecutive_429=0 if clear_escalation else state["consecutive_429"],
        consecutive_403=0 if clear_escalation else state["consecutive_403"],
        recovery_stage=recovery_stage,
        recovery_successes=recovery_successes,
        stable_successes=stable_successes,
        manual_review=0,
    )
    if was_probe:
        print(
            "WARNING recovery probe succeeded; gradual recovery begins "
            f"interval_seconds={JACRED_RECOVERY_INTERVALS_SECONDS[0]}",
            file=sys.stderr,
            flush=True,
        )
    elif recovering and recovery_stage != int(state["recovery_stage"]):
        print(
            f"INFO gradual recovery advanced stage={recovery_stage + 1}/"
            f"{len(JACRED_RECOVERY_INTERVALS_SECONDS)} "
            f"interval_seconds={JACRED_RECOVERY_INTERVALS_SECONDS[recovery_stage]}",
            file=sys.stderr,
            flush=True,
        )
    if clear_escalation and (
        int(state["consecutive_429"]) or int(state["consecutive_403"]) or state["failure_reason"]
    ):
        print(
            "INFO upstream protection escalation cleared after "
            f"{JACRED_STABLE_SUCCESS_COUNT} stable successful requests",
            file=sys.stderr,
            flush=True,
        )
    return updated



def begin_upstream_attempt():
    state = read_protection_state()
    now = time.time()
    if state["manual_review"]:
        raise UpstreamCoolingDown(
            86400,
            "JacRed upstream is blocked pending manual review; no automatic probe is allowed",
        )

    if state["state"] == "OPEN":
        remaining = float(state["block_until"] or 0) - now
        if remaining > 0:
            raise UpstreamCoolingDown(
                math.ceil(remaining),
                f"JacRed upstream circuit OPEN: {state['failure_reason'] or 'cooldown active'}",
            )
        with PROTECTION_LOCK:
            latest = read_protection_state()
            if latest["manual_review"]:
                raise UpstreamCoolingDown(
                    86400,
                    "JacRed upstream is blocked pending manual review",
                )
            if latest["state"] != "OPEN" or float(latest["block_until"] or 0) > time.time():
                raise UpstreamCoolingDown(
                    max(1, math.ceil(float(latest["block_until"] or time.time() + JACRED_PROBE_FAILURE_SECONDS) - time.time())),
                    "JacRed upstream circuit changed while waiting for a slot",
                )
            update_protection_state(
                state="HALF_OPEN",
                failure_reason=latest["failure_reason"] or "controlled recovery probe",
            )
        print(
            "WARNING upstream circuit HALF_OPEN; permitting exactly one controlled probe",
            file=sys.stderr,
            flush=True,
        )
        return True

    if state["state"] == "HALF_OPEN":
        # A concurrent request may have passed the initial queue check while
        # another thread claimed the sole probe. Never permit a second probe.
        raise UpstreamCoolingDown(
            JACRED_PROBE_FAILURE_SECONDS,
            "JacRed recovery probe is already in progress",
        )
    return False


def wait_for_upstream_slot(deadline):
    global LAST_UPSTREAM_COMPLETION

    state = read_protection_state()
    now_wall = time.time()
    if state["manual_review"]:
        raise UpstreamCoolingDown(
            86400,
            "JacRed upstream is blocked pending manual review",
        )

    probe_candidate = state["state"] == "OPEN"
    if probe_candidate:
        remaining_block = float(state["block_until"] or 0) - now_wall
        if remaining_block > 0:
            raise UpstreamCoolingDown(
                math.ceil(remaining_block),
                f"JacRed upstream circuit OPEN: {state['failure_reason'] or 'cooldown active'}",
            )
    elif state["state"] == "HALF_OPEN":
        raise UpstreamCoolingDown(
            JACRED_PROBE_FAILURE_SECONDS,
            "JacRed recovery probe is already in progress",
        )

    if probe_candidate:
        # One eligible request may probe after the persisted cooldown expires.
        interval_seconds = JACRED_MIN_INTERVAL_MS / 1000.0
    else:
        # Recovery pacing applies only after a rate-limit/access block or
        # a failed HALF_OPEN probe. A transient ordinary 5xx/timeout must not
        # accidentally switch every subsequent request to the 60-second step.
        recovering = bool(
            int(state["consecutive_429"])
            or int(state["consecutive_403"])
            or str(state["failure_reason"]).startswith("HALF_OPEN probe failed:")
            or int(state["recovery_stage"])
            or int(state["recovery_successes"])
        )
        if recovering:
            stage = min(
                int(state["recovery_stage"]),
                len(JACRED_RECOVERY_INTERVALS_SECONDS) - 1,
            )
            interval_seconds = max(
                JACRED_MIN_INTERVAL_MS / 1000.0,
                float(JACRED_RECOVERY_INTERVALS_SECONDS[stage]),
            )
        else:
            # Normal CLOSED operation uses the configured floor (default 5 s),
            # not the slower first step reserved for circuit recovery.
            interval_seconds = JACRED_MIN_INTERVAL_MS / 1000.0

    with RATE_LIMIT_LOCK:
        now_mono = time.monotonic()
        delay = LAST_UPSTREAM_COMPLETION + interval_seconds - now_mono

    if delay > 0:
        remaining_budget = deadline - now_mono
        if remaining_budget <= 0 or delay > remaining_budget:
            raise UpstreamCoolingDown(
                max(1, math.ceil(delay)),
                f"JacRed request deferred for {delay:.1f}s; "
                f"maximum queue wait is {JACRED_MAX_QUEUE_WAIT_SECONDS}s",
            )
        time.sleep(delay)

    # Claim HALF_OPEN only after a slot can be granted. The global request lock
    # prevents another adapter thread from issuing a concurrent probe.
    return begin_upstream_attempt()


def log_request_diagnostics(params, key):
    if not REQUEST_PARAMS_LOGGING:
        return

    # Query-string parameters are useful when diagnosing cache misses, but
    # credential-like values must never be written to the journal.
    safe_params = {}
    secret_names = {
        "apikey", "xapikey", "key", "token", "accesstoken",
        "password", "secret", "authorization",
    }
    for name, values in params.items():
        normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
        if normalized in secret_names:
            continue
        safe_params[name] = values

    key_signature = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:12]
    print(
        "REQUEST PARAMS "
        f"cache_key={key_signature} "
        f"params={json.dumps(safe_params, ensure_ascii=False, sort_keys=True)}",
        file=sys.stderr,
        flush=True,
    )


def cache_key(params):
    pairs = []
    for name, values in params.items():
        if name.lower() == "apikey":
            continue
        pairs.append(
            (
                name.lower(),
                tuple(sorted(clean(v) for v in values)),
            )
        )
    return tuple(sorted(pairs))


def get_inflight_lock(key):
    with CACHE_LOCK:
        lock = INFLIGHT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            INFLIGHT_LOCKS[key] = lock
        return lock


def prune_cache(now):
    expired = [
        key
        for key, (created, _) in CACHE.items()
        if now - created >= CACHE_TTL_SECONDS
    ]
    for key in expired:
        CACHE.pop(key, None)

    if len(CACHE) <= CACHE_MAX_ENTRIES:
        return

    oldest = sorted(CACHE.items(), key=lambda entry: entry[1][0])
    for key, _ in oldest[: len(CACHE) - CACHE_MAX_ENTRIES]:
        CACHE.pop(key, None)


def cached_request(key, producer):
    now = time.monotonic()
    with CACHE_LOCK:
        cached = CACHE.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1], True

    lock = get_inflight_lock(key)
    with lock:
        now = time.monotonic()
        with CACHE_LOCK:
            cached = CACHE.get(key)
            if cached and now - cached[0] < CACHE_TTL_SECONDS:
                return cached[1], True

        # The producer returns (xml_bytes, cacheable). Cache only complete
        # successful responses; the HTTP handler always receives XML bytes.
        result, cacheable = producer()

        if cacheable:
            now = time.monotonic()
            with CACHE_LOCK:
                CACHE[key] = (now, result)
                prune_cache(now)
        return result, False



def get_source_inflight_lock(key):
    with SOURCE_CACHE_LOCK:
        lock = SOURCE_INFLIGHT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            SOURCE_INFLIGHT_LOCKS[key] = lock
        return lock


def prune_source_cache(now):
    expired = [
        key
        for key, (created, _) in SOURCE_CACHE.items()
        if now - created >= JACRED_SOURCE_CACHE_TTL_SECONDS
    ]
    for key in expired:
        SOURCE_CACHE.pop(key, None)

    if len(SOURCE_CACHE) <= JACRED_SOURCE_CACHE_MAX_ENTRIES:
        return

    oldest = sorted(SOURCE_CACHE.items(), key=lambda entry: entry[1][0])
    for key, _ in oldest[: len(SOURCE_CACHE) - JACRED_SOURCE_CACHE_MAX_ENTRIES]:
        SOURCE_CACHE.pop(key, None)


def cached_jacred_results(query, year, media_type):
    # These are exactly the varying inputs sent to JacRed. Torznab offset,
    # limit, category filtering, and season/episode filtering are handled
    # locally and therefore do not belong in this cache key.
    key = (query, year, media_type)
    now = time.monotonic()
    with SOURCE_CACHE_LOCK:
        cached = SOURCE_CACHE.get(key)
        if cached and now - cached[0] < JACRED_SOURCE_CACHE_TTL_SECONDS:
            return cached[1], True

    lock = get_source_inflight_lock(key)
    with lock:
        now = time.monotonic()
        with SOURCE_CACHE_LOCK:
            cached = SOURCE_CACHE.get(key)
            if cached and now - cached[0] < JACRED_SOURCE_CACHE_TTL_SECONDS:
                return cached[1], True

        # Exceptions propagate and are deliberately not cached.
        results = query_jacred(query, year, media_type)
        now = time.monotonic()
        with SOURCE_CACHE_LOCK:
            SOURCE_CACHE[key] = (now, results)
            prune_source_cache(now)
        return results, False


def clean(value):
    return "" if value is None else html.unescape(str(value)).strip()


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def unique_preserve(values):
    result, seen = [], set()
    for value in values:
        value = clean(value)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def item_categories(item):
    values = item.get("Category") or []
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    return {as_int(v) for v in values if as_int(v)}


# ----------------------------------------------------------------
# Audio
# ----------------------------------------------------------------

def normalize_audio_title(title):
    title = clean(title)
    if not title:
        return ""
    for pattern in (
        r"(?i)^MVO\s*\[\s*(.*?)\s*\](.*)$",
        r"(?i)^MVO\s*\|\s*(.+)$",
        r"(?i)^MVO\s*[-–—]\s*(.+)$",
    ):
        match = re.match(pattern, title)
        if match:
            title = clean(match.group(1))
            suffix = clean(match.group(2)) if match.lastindex and match.lastindex > 1 else ""
            if suffix:
                title += " " + suffix
            break
    else:
        if re.fullmatch(r"(?i)MVO", title):
            return ""
    title = re.sub(r"^\s*\[\s*(.*?)\s*\]\s*$", r"\1", title)
    title = re.sub(r"\s+", " ", title).strip()
    return re.sub(r"(?i)\bHDrezka\b", "HDRezka", title)


def russian_audio_tracks(ffprobe):
    if not isinstance(ffprobe, list):
        return []
    result = []
    for stream in ffprobe:
        if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags") or {}
        language = clean(tags.get("language") or tags.get("LANGUAGE")).lower()
        if language not in {"rus", "ru"}:
            continue
        voice = normalize_audio_title(tags.get("title") or tags.get("TITLE"))
        if voice:
            result.append(voice)
    return unique_preserve(result)


def fallback_voices(info):
    voices = info.get("voices")
    return unique_preserve(normalize_audio_title(v) for v in voices) if isinstance(voices, list) else []


def title_voice_fallback(title):
    title = clean(title)
    result = []
    for match in re.finditer(r"(?i)(?:\b\d+\s*[xх×]\s*)?MVO\s*\(([^)]*)\)", title):
        result.extend(normalize_audio_title(x) for x in re.split(r"[,;/]+", match.group(1)))
    for match in re.finditer(r"(?i)(?:\b\d+\s*[xх×]\s*)?MVO\s*\[([^\]]+)\]", title):
        result.append(normalize_audio_title("MVO [" + match.group(1) + "]"))
    for match in re.finditer(r"(?i)\bMVO\s*[-–—:]\s*([^|+]+)", title):
        result.append(normalize_audio_title("MVO - " + match.group(1)))
    for match in re.finditer(r"(?i)\bMVO\s*\|\s*([^|]+)", title):
        result.append(normalize_audio_title("MVO | " + match.group(1)))
    return unique_preserve(result)


# ----------------------------------------------------------------
# Quality / video
# ----------------------------------------------------------------

def first_video_stream(ffprobe):
    """Choose a moving-video stream, never an attached cover or still image."""
    if not isinstance(ffprobe, list):
        return None

    # ffprobe can expose attached artwork as codec_type=video. Some APIs also
    # expose standalone still-image streams without setting attached_pic, so
    # reject those known image codecs as a second line of defence. Do not use a
    # broad codec blacklist: ordinary H.264/HEVC/AV1 video streams remain valid.
    still_image_codecs = {"bmp", "gif", "png", "tiff", "webp"}
    attached_pic_values = {"1", "true", "yes", "on"}
    candidates = []
    for stream in ffprobe:
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue

        disposition = stream.get("disposition")
        if isinstance(disposition, dict):
            attached_pic = clean(disposition.get("attached_pic")).lower()
            if attached_pic in attached_pic_values:
                continue

        codec_name = clean(stream.get("codec_name")).lower()
        if codec_name == "mjpeg" or codec_name in still_image_codecs:
            continue

        width, height = as_int(stream.get("width")), as_int(stream.get("height"))
        if width < 640 and height < 640:
            continue
        candidates.append((width * height, stream))
    return max(candidates, key=lambda x: x[0])[1] if candidates else None


def resolution_class(width, height):
    width = as_int(width)
    if width >= 3000:
        return "2160p"
    if width >= 1700:
        return "1080p"
    if width >= 1200:
        return "720p"
    return "480p" if width > 0 else ""


def normalize_quality_value(value):
    text = clean(value)
    if not text:
        return ""
    match = re.search(r"(?i)(2160|1080|720|576|480)\s*p?\b", text)
    if match:
        return match.group(1) + "p"
    if re.search(r"(?i)\b4k\b", text):
        return "2160p"
    return ""


def hdr_markers(source_title):
    """Return canonical HDR/SDR labels found in the original release title only.

    JacRed's current result schema exposes a generic info.videotype value but
    no reliable fields for distinguishing HDR10, HDR10+ and Dolby Vision.
    Therefore these labels are title-derived hints, not verified media facts.
    """
    text = clean(source_title)
    if not text:
        return []

    hdr10_plus = bool(re.search(r"(?i)(?<!\w)HDR\s*10\s*(?:\+|PLUS)(?!\w)", text))
    dv_designator = r"(?:DV|DOVI|DOLBY[\s._-]*VISION)"
    dv_profile_8_1 = bool(
        re.search(
            rf"(?i)(?<!\w){dv_designator}\s*(?:(?:PROFILE|PROF)\s*)?(?:P\s*)?8\.1(?!\w)",
            text,
        )
    )
    # Profile 8.1 has an HDR10-compatible base layer. Infer the HDR10 marker
    # only when that profile is explicitly stated in the original release title.
    hdr10 = bool(
        re.search(r"(?i)(?<!\w)HDR\s*10(?!\s*(?:\+|PLUS)|\w)", text)
        or dv_profile_8_1
    )
    generic_hdr = bool(re.search(r"(?i)(?<!\w)HDR(?!\w)", text))
    explicit_dolby_vision = bool(
        re.search(r"(?i)(?<!\w)DOLBY[\s._-]*VISION(?!\w)", text)
        or re.search(r"(?i)(?<!\w)DOVI(?!\w)", text)
    )

    # "DV" is overloaded, so accept it only when paired with a Dolby Vision
    # profile/version or another strong HDR/UHD marker in the same title.
    dv_token = bool(re.search(r"(?i)(?<!\w)DV(?!\w)", text))
    dv_profile = bool(
        re.search(
            rf"(?i)(?<!\w){dv_designator}\s*(?:(?:PROFILE|PROF)\s*)?(?:P\s*)?\d+(?:\.\d+)?(?!\w)",
            text,
        )
    )
    dv_context = bool(
        hdr10_plus
        or hdr10
        or generic_hdr
        or re.search(r"(?i)(?<!\w)(?:4K|UHD|2160P|4320P|10[ -]?BIT)(?!\w)", text)
    )
    dolby_vision = explicit_dolby_vision or (dv_token and (dv_profile or dv_context))

    markers = []
    if hdr10:
        markers.append("HDR10")
    if hdr10_plus:
        markers.append("HDR10+")
    if dolby_vision:
        markers.append("Dolby Vision")

    # A generic HDR label is useful only when the title does not name a
    # more specific HDR format. Do not infer HDR10+ from a bare "HDR+" token.
    if not markers and generic_hdr:
        markers.append("HDR")

    # Keep an explicit SDR marker only when the title contains no HDR marker.
    # If a title is internally contradictory, do not emit both classifications.
    if not markers and re.search(r"(?i)(?<!\w)SDR(?!\w)", text):
        markers.append("SDR")

    return markers


def extract_actual_quality(item, source_title):
    # Quality: ffprobe dimensions -> JacRed info.quality -> title.
    # HDR subtype labels are deliberately title-only; info.videotype and
    # codec/resolution are not evidence for HDR10 vs HDR10+ vs Dolby Vision.
    video = first_video_stream(item.get("ffprobe") or [])
    quality = resolution_class(video.get("width"), video.get("height")) if video else ""
    if not quality:
        quality = normalize_quality_value((item.get("info") or {}).get("quality"))
    if not quality:
        quality = normalize_quality_value(source_title)
    labels = hdr_markers(source_title)
    return " ".join(part for part in [quality, *labels] if part)


def actual_resolution(item):
    video = first_video_stream(item.get("ffprobe") or [])
    if not video:
        return "", ""
    width, height = as_int(video.get("width")), as_int(video.get("height"))
    return (f"{width}x{height}" if width and height else "", clean(video.get("codec_name")))


# ----------------------------------------------------------------
# TV parsing
# ----------------------------------------------------------------

def normalize_season_episode(title):
    title = clean(title)

    # In forms such as "1-4 сезоны: 1-61 серии", the season range comes
    # before the word "seasons". Parse this form before looking for "Сезоны: 1-61",
    # otherwise the episode total could be mistaken for the season range.
    prefixed_season_range = re.search(
        r"(?i)\b(\d{1,3})\s*[-–—]\s*(\d{1,3})\s+(?:сез\w*|seasons?)\b",
        title,
    )
    if prefixed_season_range:
        first, last = (int(prefixed_season_range.group(i)) for i in (1, 2))
        if last > first:
            return f"S{first:02d}-S{last:02d}"

    # Some release titles put the season number before the word "season":
    # "1 сезон 12 серия" or "1 сезон: 1-13 серии из 13". Parse these
    # single-season episode notations before the generic season-range matcher.
    single_season_episode_range = re.search(
        r"(?i)\b(\d{1,3})\s*(?:-?(?:й|ой|ый|ого|го|ему|ому|му|м|ом|ым))?"
        r"\s+сез\w*\s*[:=]?\s*\(?\s*(\d{1,4})\s*[-–—]\s*(\d{1,4})"
        r"(?:\s*сер\w*)?(?:\s+из\s+\d{1,4})?",
        title,
    )
    if single_season_episode_range:
        season, first_ep, last_ep = (
            int(single_season_episode_range.group(i)) for i in (1, 2, 3)
        )
        return f"S{season:02d}E{first_ep:02d}-{last_ep:02d}"

    single_season_episode = re.search(
        r"(?i)\b(\d{1,3})\s*(?:-?(?:й|ой|ый|ого|го|ему|ому|му|м|ом|ым))?"
        r"\s+сез\w*\s*[,;:/|—-]?\s*"
        r"(\d{1,4})\s*(?:-?(?:я|ая|й|ой|ый|юю|ую))?\s+сер\w*\b",
        title,
    )
    if single_season_episode:
        season, episode = (
            int(single_season_episode.group(i)) for i in (1, 2)
        )
        return f"S{season:02d}E{episode:02d}"

    # Explicitly labelled multi-season ranges take priority over episode ranges.
    # For packs spanning multiple seasons, the "1-61 episodes" notation may be
    # global numbering and must not be represented as S01E01-61.
    multi_season = re.search(
        r"(?i)\b(?:сезон(?:ы)?|seasons?)\s*[:=]?\s*"
        r"(\d{1,3})\s*[-–—]\s*(\d{1,3})(?:\s+из\s+\d{1,3})?\b",
        title,
    )
    if multi_season:
        first, last = (int(multi_season.group(i)) for i in (1, 2))
        if last > first:
            return f"S{first:02d}-S{last:02d}"

    # Also accept season-pack notation written as S01-S04, with or without
    # square brackets. This pattern cannot consume the episode form S01E01-04.
    season_range = re.search(
        r"(?i)(?<![A-Z0-9])S\s*(\d{1,3})\s*[-–—]\s*S?\s*(\d{1,3})\b",
        title,
    )
    if season_range:
        first, last = (int(season_range.group(i)) for i in (1, 2))
        if last > first:
            return f"S{first:02d}-S{last:02d}"

    # Some trackers list a consecutive pack as [s3,s4]. Normalize it as a range
    # only when the listed seasons are contiguous; never imply missing seasons.
    bracketed_season_list = re.search(
        r"(?i)\[\s*(s\s*\d{1,3}(?:\s*[,;/]\s*s?\s*\d{1,3})+)\s*\]",
        title,
    )
    if bracketed_season_list:
        seasons = [
            int(value)
            for value in re.findall(r"(?i)s?\s*(\d{1,3})", bracketed_season_list.group(1))
        ]
        if seasons and all(
            current + 1 == following
            for current, following in zip(seasons, seasons[1:])
        ):
            return f"S{seasons[0]:02d}-S{seasons[-1]:02d}"
        # A non-contiguous explicit list does not safely map to one season range.
        return ""

    # RuTracker may label one season and its episodes with or without colons.
    # Example: "Сезон 1 / Серии 1-4 из 4" -> S01E01-04.
    labelled_range = re.search(
        r"(?i)\bсезон\s*[:=]?\s*(\d{1,3})\s*(?:[/|,;]\s*)?"
        r"серии\s*[:=]?\s*(\d{1,4})\s*[-–—]\s*(\d{1,4})",
        title,
    )
    if labelled_range:
        season, first_ep, last_ep = (int(labelled_range.group(i)) for i in (1, 2, 3))
        return f"S{season:02d}E{first_ep:02d}-{last_ep:02d}"

    patterns = (
        r"\bS(\d{1,3})E(\d{1,4})(?:-E?(\d{1,4}))?\b",
        r"\b(\d{1,3})\s*[xх×]\s*(\d{1,4})\s*[-–—]\s*(\d{1,4})\b",
        r"\b(\d{1,3})\s*[xх×]\s*(\d{1,4})\b",
        r"\bS(\d{1,3})\s*\(\s*(\d{1,4})\s*[-–—]\s*(\d{1,4})",
        r"\b(\d{1,3})\s*(?:-?(?:й|ой|ый|ого|го|ему|ому|му|м|ом|ым))?\s+сез\w*\s*\(\s*(\d{1,4})\s*[-–—]\s*(\d{1,4})",
    )
    for pattern in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if not match:
            continue
        season = int(match.group(1))
        ep1 = int(match.group(2))
        result = f"S{season:02d}E{ep1:02d}"
        if match.lastindex and match.lastindex >= 3 and match.group(3):
            result += f"-{int(match.group(3)):02d}"
        return result

    # Detect a collection covering multiple seasons before the generic
    # "N seasons" fallback, otherwise "1-11 сезоны" incorrectly becomes S11.
    collection_patterns = (
        r"\b(\d{1,3})\s*[-–—]\s*(\d{1,3})\s+(?:сез\w*|seasons?)\b",
        r"\b(?:сезон\w*|seasons?)\s*(\d{1,3})\s*[-–—]\s*(\d{1,3})\b",
    )
    for pattern in collection_patterns:
        collection = re.search(pattern, title, re.IGNORECASE)
        if collection:
            first = int(collection.group(1))
            last = int(collection.group(2))
            if last > first:
                return f"S{first:02d}-S{last:02d}"

    # Season-only notations: [S01], "Сезон 1", "Season 1", or "S01".
    season_only_patterns = (
        r"(?i)\[\s*S\s*(\d{1,3})\s*\]",
        r"(?i)\b(?:сезон\w*|season)\s*[:=]?\s*(\d{1,3})\b",
        r"(?i)(?<![A-Z0-9])S(\d{1,3})\b(?!\s*[-,;/]\s*S?\d)",
        r"\b(\d{1,3})\s*(?:-?(?:й|ой|ый|ого|го|ему|ому|му|м|ом|ым))?\s+сез\w*",
    )
    for pattern in season_only_patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            return f"S{int(match.group(1)):02d}"
    return ""


def result_matches_season_episode(item, season, episode):
    if not season and not episode:
        return True
    title = clean(item.get("Title"))
    season_match = re.search(r"\d+", str(season or ""))
    episode_match = re.search(r"\d+", str(episode or ""))
    wanted_season = int(season_match.group()) if season_match else None
    wanted_episode = int(episode_match.group()) if episode_match else None

    match = re.search(r"(?i)\bS(\d{1,3})E(\d{1,4})(?:-E?(\d{1,4}))?\b", title)
    if not match:
        match = re.search(r"(?i)\b(\d{1,3})\s*[xх×]\s*(\d{1,4})(?:[-–—](\d{1,4}))?\b", title)
    if not match:
        return True

    if wanted_season is not None and int(match.group(1)) != wanted_season:
        return False
    if wanted_episode is None:
        return True

    first_ep = int(match.group(2))
    last_ep = int(match.group(3)) if match.group(3) else first_ep
    return first_ep <= wanted_episode <= last_ep


# ----------------------------------------------------------------
# General release data
# ----------------------------------------------------------------

def extract_year(item):
    info = item.get("info") or {}
    for text in (info.get("relased"), item.get("Title")):
        match = re.search(r"\b((?:19|20)\d{2})\b", clean(text))
        if match:
            return match.group(1)
    return ""


def extract_format(title):
    title = clean(title)
    patterns = (
        r"\b(WEB-DLRip)\s*\(([^)]+)\)",
        r"\b(WEB-DLRip)\b",
        r"\b(WEB-DL)(?:-(?:480p|576p|720p|1080p|2160p))?\b",
        r"\b(WEBRip|BDRip|BRRip|BluRay|HDRip|HDTV)\b",
        r"\bDVDRip\b|\bDVD[\s-]+Rip\b",
        r"\bDVD[59]\b",
        r"\bDVD\b",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            if index == 0:
                return f"{match.group(1)}-{clean(match.group(2))}"
            if index == 4:
                # Normalize spelling variants such as "DVD-Rip" to one token.
                return "DVDRip"
            if index == 5:
                return match.group(0).upper()
            if index == 6:
                return "DVD"
            return match.group(1)
    return ""


def extract_languages(item):
    names = {
        "rus":"Russian", "ru":"Russian", "eng":"English", "en":"English",
        "ukr":"Ukrainian", "uk":"Ukrainian", "deu":"German", "ger":"German", "de":"German",
        "fra":"French", "fre":"French", "fr":"French", "spa":"Spanish", "es":"Spanish",
        "ita":"Italian", "it":"Italian", "pol":"Polish", "pl":"Polish",
        "ces":"Czech", "cze":"Czech", "cs":"Czech", "por":"Portuguese", "pt":"Portuguese",
        "jpn":"Japanese", "ja":"Japanese", "kor":"Korean", "ko":"Korean",
        "chi":"Chinese", "zho":"Chinese", "zh":"Chinese",
    }
    result = []
    for value in item.get("languages") or []:
        value = clean(value).lower()
        if value in names:
            result.append(names[value])
    if not result:
        for stream in item.get("ffprobe") or []:
            if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
                continue
            tags = stream.get("tags") or {}
            value = clean(tags.get("language") or tags.get("LANGUAGE")).lower()
            if value in names:
                result.append(names[value])
    return unique_preserve(result)


def infohash_from_magnet(magnet):
    match = re.search(r"(?i)xt=urn:btih:([^&]+)", clean(magnet))
    return urllib.parse.unquote(match.group(1)).lower() if match else ""


def source_original_title(item):
    info = item.get("info") or {}
    return clean(info.get("originalname") or info.get("originalName") or info.get("name") or item.get("Title"))


def compact_title(value):
    tokens = re.findall(r"[a-z0-9а-яё]+", clean(value).casefold())
    while tokens and tokens[0] in {"the", "a", "an"}:
        tokens.pop(0)
    return "".join(tokens)


def title_segment_head(value):
    value = clean(value)
    value = re.split(r"[\(\[\{]", value, maxsplit=1)[0]
    value = re.sub(r"(?i)\s+(?:19|20)\d{2}(?:\s*[-–—/]\s*(?:19|20)?\d{2})?.*$", "", value)
    value = re.sub(
        r"(?i)\s+(?:2160p|1080p|720p|576p|480p|4k|HDRip|HDTV|WEB-DL|WEB-DLRip|WEBRip|BDRip|BRRip|BluRay|S\d{1,3}\b|\d{1,3}\s*(?:-?(?:й|ой|ый|ого|го))?\s+сез\w*).*$",
        "",
        value,
    )
    return value.strip(" \t-–—|,:;")

def find_russian_tv_alias(query, items):
    # Infer a Russian alias only when the queried name appears as one side of
    # a "localized title / original title" pair in multiple distinct releases.
    if re.search(r"[А-Яа-яЁё]", clean(query)):
        return None, 0, 0

    query_key = compact_title(query)
    if len(query_key) < 4:
        return None, 0, 0

    votes = {}
    display_names = {}
    seen_releases = set()
    eligible_releases = 0

    for item in items:
        raw_title = clean(item.get("Title"))
        if not raw_title:
            continue
        identity = (
            infohash_from_magnet(item.get("MagnetUri"))
            or clean(item.get("Details")).casefold()
            or raw_title.casefold()
        )
        if identity in seen_releases:
            continue
        seen_releases.add(identity)

        segments = re.split(r"\s+/\s+", raw_title)
        matching_indexes = []
        heads = [title_segment_head(segment) for segment in segments]
        for index, head in enumerate(heads):
            head_key = compact_title(head)
            if head_key and (head_key == query_key or head_key.startswith(query_key) or query_key.startswith(head_key)):
                matching_indexes.append(index)

        if not matching_indexes:
            continue

        item_candidates = {}
        for matched_index in matching_indexes:
            for index, head in enumerate(heads):
                if index == matched_index:
                    continue
                candidate = title_segment_head(head)
                if len(re.findall(r"[А-Яа-яЁё]", candidate)) < 3:
                    continue
                candidate_key = compact_title(candidate)
                if not candidate_key or candidate_key == query_key:
                    continue
                item_candidates.setdefault(candidate_key, candidate)

        if not item_candidates:
            continue

        eligible_releases += 1
        for candidate_key, candidate in item_candidates.items():
            votes[candidate_key] = votes.get(candidate_key, 0) + 1
            display_names.setdefault(candidate_key, candidate)

    if not votes:
        return None, 0, eligible_releases

    best_key = max(votes, key=lambda key: (votes[key], len(display_names[key])))
    best_count = votes[best_key]
    # Require multiple independent releases and a clear majority among
    # titles that also identify the queried series, to avoid generic aliases.
    if best_count < 2 or best_count / max(1, eligible_releases) < 0.5:
        return None, best_count, eligible_releases

    return display_names[best_key], best_count, eligible_releases


def canonical_tv_title(items, fallback):
    votes = {}
    names = {}
    for item in items:
        info = item.get("info") or {}
        value = clean(info.get("originalname") or info.get("originalName") or info.get("name"))
        if not value or re.search(r"[А-Яа-яЁё]", value):
            continue
        value = normalize_tv_original(value)
        value = re.sub(r"(?i)\s*[\(\[]?(?:19|20)\d{2}.*$", "", value).strip()
        key = compact_title(value)
        if not key:
            continue
        votes[key] = votes.get(key, 0) + 1
        names.setdefault(key, value)

    if votes:
        best_key = max(votes, key=lambda key: (votes[key], len(names[key])))
        return names[best_key]
    return normalize_tv_original(fallback)

def item_mentions_tv_title(item, canonical_title):
    target = compact_title(canonical_title)
    if len(target) < 4:
        return False
    info = item.get("info") or {}
    fields = [
        item.get("Title"),
        info.get("originalname"),
        info.get("originalName"),
        info.get("name"),
    ]
    return any(target in compact_title(value) for value in fields if value)


def with_canonical_tv_title(item, canonical_title):
    # Preserve the source release title for season/episode parsing, but expose
    # the known series name first so Sonarr can map the release to its series.
    result = dict(item)
    info = dict(item.get("info") or {})
    info["originalname"] = canonical_title
    info["originalName"] = canonical_title
    info["name"] = canonical_title
    result["info"] = info
    return result


def normalize_tv_original(text):
    text = re.sub(r"(?i)\s*\[\s*\d{1,3}\s*[xх×]\s*\d{1,4}(?:\s*[-–—]\s*\d{1,4})?(?:\s+из\s+\d{1,4})?\s*\]", "", text)
    text = re.sub(r"(?i)\s+\bS\d{1,3}(?:E\d{1,4}(?:-\d{1,4})?)?\b", "", text)
    text = re.sub(r"(?i)\s+\b\d{1,3}\s*(?:-?(?:й|ой|ый|ого|го|ему|ому|му|м|ом|ым))?\s+сез\w*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Removing an inline season label can leave an empty pair or orphaned
    # opening delimiter in a source-provided series name, e.g. "Debris (".
    text = re.sub(r"\s*\(\s*\)\s*$", "", text)
    text = re.sub(r"\s*\[\s*\]\s*$", "", text)
    text = re.sub(r"\s*\{\s*\}\s*$", "", text)
    text = re.sub(r"\s*[\(\[\{]\s*$", "", text)
    return text.strip(" \t-–—|,:;")


def normalize_movie_original(text):
    text = re.sub(r"(?i)\s*\[\s*(?:2160p|1080p|720p|576p|480p|4k|hdr10?\+?|dolby\s+vision|dv)[^\]]*\]", "", text)
    text = re.sub(r"(?i)\s+\b(?:2160p|1080p|720p|576p|480p|4k|hdr10?\+?|dv)\b", "", text)
    text = re.sub(r"(?i)\s+\b(?:WEB-DL|WEB-DLRip|WEBRip|BDRip|BRRip|BluRay|HDRip|HDTV)\b(?:[-.]?(?:AVC|HEVC|H\.265|x264|x265))?", "", text)
    text = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", text)
    return re.sub(r"\s+", " ", text).strip(" -–—|")


def release_voices(item, source_title):
    voices = russian_audio_tracks(item.get("ffprobe") or [])
    if not voices:
        voices = fallback_voices(item.get("info") or {})
    if not voices:
        voices = title_voice_fallback(source_title)
    return voices


def make_tv_title(item):
    source = clean(item.get("Title"))
    parts = [normalize_tv_original(source_original_title(item)), normalize_season_episode(source)]
    year = extract_year(item)
    fmt = extract_format(source)
    quality = extract_actual_quality(item, source)

    if year:
        parts.append(f"({year})")
    if fmt:
        parts.append(fmt)
    if quality:
        parts.append(quality)

    title = " ".join(p for p in parts if p)
    voices = release_voices(item, source)
    if voices:
        title += " | " + " | ".join(voices)
    return title + (" *** " + source if source and source != title else "")


def make_movie_title(item):
    source = clean(item.get("Title"))
    parts = [normalize_movie_original(source_original_title(item))]
    year = extract_year(item)
    fmt = extract_format(source)
    quality = extract_actual_quality(item, source)

    if year:
        parts.append(f"({year})")
    if fmt:
        parts.append(fmt)
    if quality:
        parts.append(quality)

    title = " ".join(p for p in parts if p)
    voices = release_voices(item, source)
    if voices:
        title += " | " + " | ".join(voices)
    return title + (" *** " + source if source and source != title else "")


def make_title(item, media_type):
    return make_movie_title(item) if media_type == "movie" else make_tv_title(item)


# ----------------------------------------------------------------
# Media type and category filtering
# ----------------------------------------------------------------

def item_media_type(item):
    categories = item_categories(item)
    info = item.get("info") or {}
    types = {clean(x).lower() for x in (info.get("types") or [])}
    if categories & MOVIE_CATEGORIES or types & {"movie", "film"}:
        return "movie"
    if categories & TV_CATEGORIES or types & {"serial", "series", "tv"}:
        return "tv"
    return ""


def requested_media_types(request_type, requested_categories):
    if request_type == "movie":
        return {"movie"}
    if request_type == "tvsearch":
        return {"tv"}
    if requested_categories & MOVIE_CATEGORIES and not requested_categories & TV_CATEGORIES:
        return {"movie"}
    if requested_categories & TV_CATEGORIES and not requested_categories & MOVIE_CATEGORIES:
        return {"tv"}
    return {"movie", "tv"}


def item_matches_categories(item, requested, media_type):
    if not requested:
        return True
    cats = item_categories(item)
    if media_type == "movie":
        if not cats & MOVIE_CATEGORIES:
            return False
        # JacRed v2 may expose a movie only as parent category 2000.
        # A Torznab request for a movie subcategory must still return
        # the movie; the adapter must not invent a more specific source
        # category that JacRed did not provide.
        return bool(requested & MOVIE_CATEGORIES)
    if not cats & TV_CATEGORIES:
        return False
    return 5000 in requested or bool(cats & requested & TV_CATEGORIES)


# ----------------------------------------------------------------
# JacRed and XML
# ----------------------------------------------------------------

def query_jacred(query, year, media_type):
    global LAST_UPSTREAM_COMPLETION

    params = {
        "q": query,
        "category": "movie_" if media_type == "movie" else "tv_",
        "limit": "1000",
    }
    if year:
        params["year"] = year
    separator = "&" if "?" in JACRED_URL else "?"
    url = JACRED_URL + separator + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": JACRED_USER_AGENT,
            "Accept": "application/json",
        },
    )

    # One global lock serializes every outbound query and all title/media variants.
    # The queue budget includes time spent waiting for that lock and for the
    # completion-based quiet interval.
    deadline = time.monotonic() + JACRED_MAX_QUEUE_WAIT_SECONDS
    acquired = UPSTREAM_REQUEST_LOCK.acquire(
        timeout=max(0.0, deadline - time.monotonic())
    )
    if not acquired:
        raise UpstreamCoolingDown(
            JACRED_MAX_QUEUE_WAIT_SECONDS,
            "JacRed request queue is busy; maximum queue wait was reached",
        )

    try:
        probe = wait_for_upstream_slot(deadline)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JacRed JSON response must be an object")
            results = data.get("Results") or []
            if not isinstance(results, list):
                raise ValueError("JacRed JSON field Results must be a list")
            return [item for item in results if isinstance(item, dict)]
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = parse_retry_after(exc)
                raise register_http_429(retry_after, exc) from exc
            if exc.code == 403:
                raise register_http_403(exc) from exc
            if exc.code == 401:
                raise register_http_401(exc) from exc
            cooling = register_other_failure(
                f"HTTP {exc.code} {clean(exc.reason)}",
                probe,
            )
            if cooling:
                raise cooling from exc
            raise
        except Exception as exc:
            # Circuit-control exceptions are not upstream failures and must
            # not overwrite the persisted OPEN/HALF_OPEN state.
            if isinstance(exc, UpstreamCoolingDown):
                raise
            cooling = register_other_failure(
                f"{type(exc).__name__}: {exc}",
                probe,
            )
            if cooling:
                raise cooling from exc
            raise
        finally:
            with RATE_LIMIT_LOCK:
                LAST_UPSTREAM_COMPLETION = time.monotonic()
            # Count only a completed, valid JSON response as an upstream success.
            # An error must never clear the active block or failure escalation.
            if "data" in locals() and isinstance(data, dict) and isinstance(data.get("Results") or [], list):
                register_upstream_success(probe)
                clear_notification_state_prefix("upstream:")
            elif probe:
                # A failed recovery probe must leave the circuit OPEN, even for
                # transport errors or malformed JSON that are not HTTP status codes.
                state = read_protection_state()
                if state["state"] == "HALF_OPEN":
                    register_other_failure("recovery probe failed before a valid response", probe=True)
    finally:
        UPSTREAM_REQUEST_LOCK.release()




def _notification_connection(create=True):
    db_path = os.path.abspath(JACRED_STATE_PATH)
    if not create and not os.path.isfile(db_path):
        return None
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=2000")
    if create:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS adapter_notification_state (
                event_key TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        connection.commit()
    else:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adapter_notification_state'"
        ).fetchone()
        if not exists:
            connection.close()
            return None
    return connection


def _reserve_notification(event_key, fingerprint):
    # Reserve before network I/O so concurrent requests cannot flood the same alert.
    with NOTIFICATION_STATE_LOCK:
        if NOTIFICATION_STATE_MEMORY.get(event_key) == fingerprint:
            return False
        connection = None
        try:
            connection = _notification_connection(create=True)
            row = connection.execute(
                "SELECT fingerprint FROM adapter_notification_state WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if row and str(row["fingerprint"]) == fingerprint:
                NOTIFICATION_STATE_MEMORY[event_key] = fingerprint
                return False
            connection.execute(
                """
                INSERT INTO adapter_notification_state(event_key, fingerprint, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    fingerprint=excluded.fingerprint,
                    updated_at=excluded.updated_at
                """,
                (event_key, fingerprint, time.time()),
            )
            connection.commit()
        except Exception as exc:
            # Notification state storage must never break search or protection.
            print(
                f"APPRISE_STATE_WARNING event={event_key} error_type={type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
        finally:
            if connection is not None:
                connection.close()
        NOTIFICATION_STATE_MEMORY[event_key] = fingerprint
        return True


def clear_notification_state_prefix(prefix):
    # Clear a resolved incident after valid upstream recovery. A future recurrence
    # should notify again, but repeated unchanged failures remain suppressed.
    with NOTIFICATION_STATE_LOCK:
        connection = None
        try:
            connection = _notification_connection(create=False)
            if connection is None:
                return
            rows = connection.execute(
                "SELECT event_key FROM adapter_notification_state WHERE event_key LIKE ?",
                (prefix + "%",),
            ).fetchall()
            if rows:
                connection.execute(
                    "DELETE FROM adapter_notification_state WHERE event_key LIKE ?",
                    (prefix + "%",),
                )
                connection.commit()
            for key in list(NOTIFICATION_STATE_MEMORY):
                if key.startswith(prefix):
                    NOTIFICATION_STATE_MEMORY.pop(key, None)
        except Exception as exc:
            print(
                f"APPRISE_STATE_WARNING action=clear error_type={type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
        finally:
            if connection is not None:
                connection.close()


def send_notification_once(event_key, fingerprint, title_suffix, body, tag, notify_type):
    if not JACRED_NOTIFICATIONS_ENABLED:
        print(
            f"APPRISE_SKIPPED event={event_key} reason=notifications-disabled",
            file=sys.stderr,
            flush=True,
        )
        return False

    parsed = urllib.parse.urlsplit(JACRED_APPRISE_URL)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[A-Za-z0-9_-]+", JACRED_APPRISE_CONFIG_KEY)
        or not JACRED_NOTIFICATION_TAG_WARNINGS
        or not JACRED_NOTIFICATION_TAG_CRITICAL
    ):
        print(
            f"APPRISE_FAILED event={event_key} tag={tag} reason=invalid-notification-configuration",
            file=sys.stderr,
            flush=True,
        )
        return False

    if not _reserve_notification(event_key, fingerprint):
        print(
            f"APPRISE_SUPPRESSED event={event_key} tag={tag} reason=unchanged-state",
            file=sys.stderr,
            flush=True,
        )
        return False

    endpoint = (
        JACRED_APPRISE_URL
        + "/notify/"
        + urllib.parse.quote(JACRED_APPRISE_CONFIG_KEY, safe="")
    )
    payload = json.dumps(
        {
            "title": f"{SERVER_TITLE} adapter — {title_suffix}",
            "body": body,
            "type": notify_type,
            "tag": tag,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"{SERVER_TITLE}-adapter/{VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=JACRED_NOTIFICATION_TIMEOUT_SECONDS
        ) as response:
            response.read(2048)
            status = int(response.getcode())
        if 200 <= status < 300:
            print(
                f"APPRISE_SENT event={event_key} tag={tag} status={status}",
                file=sys.stderr,
                flush=True,
            )
            return True
        print(
            f"APPRISE_FAILED event={event_key} tag={tag} http_status={status}",
            file=sys.stderr,
            flush=True,
        )
    except urllib.error.HTTPError as exc:
        print(
            f"APPRISE_FAILED event={event_key} tag={tag} http_status={exc.code}",
            file=sys.stderr,
            flush=True,
        )
    except Exception as exc:
        # Never send a notification about notification delivery failure itself.
        print(
            f"APPRISE_FAILED event={event_key} tag={tag} error_type={type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
    return False


def notify_upstream_problem(exc, media_type=""):
    message = str(exc)
    lower = message.lower()
    status_match = re.search(r"(?i)\bHTTP\s+(401|403|429|5\d\d)\b", message)
    status = int(status_match.group(1)) if status_match else None
    branch_label = {
        "movie": "ветка поиска фильмов",
        "tv": "ветка поиска сериалов",
    }.get(media_type, "общая проверка upstream")

    if status == 401 or "manual authentication/configuration review" in lower:
        event_key = "upstream:authentication"
        fingerprint = "http-401-manual-review"
        tag = JACRED_NOTIFICATION_TAG_CRITICAL
        notify_type = "failure"
        title = "Критическая ошибка: требуется проверка доступа к JacRed"
        body = (
            f"{branch_label}: JacRed вернул HTTP 401. Запросы к источнику отключены "
            "до ручной проверки аутентификации/конфигурации. Подробности — в журнале службы."
        )
    elif status == 403:
        manual = "manual review" in lower
        event_key = "upstream:access-block"
        fingerprint = "http-403-manual-review" if manual else "http-403-protection-block"
        tag = JACRED_NOTIFICATION_TAG_CRITICAL
        notify_type = "failure"
        title = (
            "Критическая ошибка: JacRed требует ручной проверки"
            if manual
            else "Критическая ошибка: JacRed ограничил доступ"
        )
        body = (
            f"{branch_label}: JacRed вернул HTTP 403; защитный блок включён. "
            + (
                "Автоматическое восстановление запрещено, требуется ручная проверка. "
                if manual
                else "Следующая проверка возможна только после защитной паузы. "
            )
            + "Не сбрасывайте блокировку без проверки причины. Подробности — в журнале службы."
        )
    elif status == 429:
        stage_match = re.search(r"(?i)\bstage\s+(\d+)", message)
        stage = int(stage_match.group(1)) if stage_match else 1
        event_key = "upstream:rate-limit"
        fingerprint = f"http-429-stage-{stage}"
        tag = JACRED_NOTIFICATION_TAG_WARNINGS
        notify_type = "warning"
        title = "Предупреждение: JacRed ограничил частоту запросов"
        body = (
            f"{branch_label}: получен HTTP 429, ступень задержки {stage}. "
            "Адаптер соблюдает период ожидания и ограничивает новые обращения. "
            "Подробности — в журнале службы."
        )
    elif "queue" in lower or "maximum queue wait" in lower or "deferred for" in lower:
        event_key = "upstream:queue"
        fingerprint = "queue-wait-exceeded"
        tag = JACRED_NOTIFICATION_TAG_WARNINGS
        notify_type = "warning"
        title = "Предупреждение: очередь запросов JacRed перегружена"
        body = (
            f"{branch_label}: ожидание свободного слота upstream превысило допустимое время. "
            "Адаптер вернул временную ошибку и не запустил лишний запрос. "
            "Подробности — в журнале службы."
        )
    elif (
        isinstance(exc, (TimeoutError, ConnectionError, OSError, urllib.error.URLError))
        or "timeout" in lower
        or "timed out" in lower
    ):
        event_key = "upstream:transport"
        fingerprint = f"transport-{type(exc).__name__}"
        tag = JACRED_NOTIFICATION_TAG_WARNINGS
        notify_type = "warning"
        title = "Предупреждение: временная ошибка соединения с JacRed"
        body = (
            f"{branch_label}: запрос к источнику не завершился успешно "
            f"({type(exc).__name__}). Адаптер сохранит защитное поведение и повторит поиск "
            "при следующем обращении. Подробности — в журнале службы."
        )
    elif status is not None:
        event_key = "upstream:http-error"
        fingerprint = f"http-{status}"
        tag = JACRED_NOTIFICATION_TAG_WARNINGS
        notify_type = "warning"
        title = f"Предупреждение: ошибка HTTP {status} от JacRed"
        body = (
            f"{branch_label}: JacRed вернул HTTP {status}. "
            "Проверьте доступность источника и журнал службы."
        )
    elif isinstance(exc, UpstreamCoolingDown):
        event_key = "upstream:protection"
        fingerprint = f"cooldown-{type(exc).__name__}"
        tag = JACRED_NOTIFICATION_TAG_WARNINGS
        notify_type = "warning"
        title = "Предупреждение: активна защитная пауза JacRed"
        body = (
            f"{branch_label}: адаптер временно приостановил запросы к источнику. "
            "Проверьте состояние upstream в журнале службы."
        )
    else:
        event_key = "adapter:request-error"
        fingerprint = f"request-error-{type(exc).__name__}"
        tag = JACRED_NOTIFICATION_TAG_CRITICAL
        notify_type = "failure"
        title = "Критическая ошибка обработки поиска"
        body = (
            f"{branch_label}: во время обработки поискового запроса возникла ошибка "
            f"{type(exc).__name__}. Подробности — в журнале службы."
        )

    return send_notification_once(
        event_key, fingerprint, title, body, tag, notify_type
    )


def notify_persisted_protection_state():
    try:
        state = read_protection_state()
        if state["manual_review"]:
            reason = clean(state["failure_reason"]) or "manual review required"
            if "HTTP 403" in reason.upper():
                message = f"JacRed HTTP 403; manual review required; {reason}"
            elif "HTTP 401" in reason.upper():
                message = "JacRed HTTP 401; manual authentication/configuration review required"
            else:
                message = f"JacRed upstream blocked pending manual review; {reason}"
            notify_upstream_problem(UpstreamCoolingDown(86400, message))
        elif state["state"] == "OPEN":
            reason = clean(state["failure_reason"]) or "protection cooldown active"
            retry_after = max(
                1,
                math.ceil(float(state["block_until"] or time.time() + 1) - time.time()),
            )
            notify_upstream_problem(
                UpstreamCoolingDown(retry_after, f"JacRed upstream circuit OPEN: {reason}")
            )
    except Exception as exc:
        print(
            f"APPRISE_STATE_WARNING action=startup-state-check error_type={type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )


def notify_startup_failure(exc):
    error_code = getattr(exc, "errno", None)
    fingerprint = f"{type(exc).__name__}:{error_code if error_code is not None else 'no-code'}"
    send_notification_once(
        "adapter:startup-failure",
        fingerprint,
        "Критическая ошибка запуска",
        (
            "Адаптер не смог инициализировать защиту upstream или открыть локальный "
            f"Torznab endpoint 127.0.0.1:{PORT}. Тип ошибки: {type(exc).__name__}; "
            f"код: {error_code if error_code is not None else 'не указан'}. "
            "Проверьте журнал systemd."
        ),
        JACRED_NOTIFICATION_TAG_CRITICAL,
        "failure",
    )


def parse_publish_date(value):
    value = clean(value)
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        except ValueError:
            pass

        for fmt in (
            "%m/%d/%Y %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                return parsed.strftime("%a, %d %b %Y %H:%M:%S GMT")
            except ValueError:
                pass

    # Never turn a malformed/missing source date into "now": that creates a
    # misleading 0-minute age in Prowlarr. A fixed epoch date keeps the RSS
    # item valid while clearly representing an unknown timestamp.
    if value:
        print(
            f"WARNING invalid JacRed PublishDate: {value!r}",
            file=sys.stderr,
            flush=True,
        )
    return "Thu, 01 Jan 1970 00:00:00 GMT"


def add_text(parent, name, value):
    if value is not None:
        node = ET.SubElement(parent, name)
        node.text = str(value)


def add_attr(parent, name, value):
    node = ET.SubElement(parent, f"{{{TORZNAB_NS}}}attr")
    node.set("name", name)
    node.set("value", str(value))


def make_caps():
    caps = ET.Element("caps")
    ET.SubElement(
        caps,
        "server",
        version="1.0",
        title=SERVER_TITLE,
        strapline="JacRed v2 TV + Movies JSON to Torznab adapter",
        url=f"http://127.0.0.1:{PORT}/",
    )
    ET.SubElement(caps, "limits", max="1000", **{"default": "100"})
    searching = ET.SubElement(caps, "searching")
    ET.SubElement(searching, "search", available="yes", supportedParams="q,limit,offset,cat,year")
    ET.SubElement(searching, "tv-search", available="yes", supportedParams="q,limit,offset,cat,year,season,ep")
    ET.SubElement(searching, "movie-search", available="yes", supportedParams="q,limit,offset,cat,year")
    categories = ET.SubElement(caps, "categories")
    ET.SubElement(categories, "category", id="2000", name="Movies")
    tv = ET.SubElement(categories, "category", id="5000", name="TV")
    ET.SubElement(tv, "subcat", id="5070", name="Anime")
    return ET.tostring(caps, encoding="utf-8", xml_declaration=True)


def make_feed(results):
    rss = ET.Element("rss", version="2.0", **{"xmlns:torznab": TORZNAB_NS})
    channel = ET.SubElement(rss, "channel")
    add_text(channel, "title", SERVER_TITLE)
    add_text(channel, "link", JACRED_URL)
    add_text(channel, "description", "JacRed v2 TV + Movies JSON to Torznab adapter")

    for item, media_type in results:
        node = ET.SubElement(channel, "item")
        source = clean(item.get("Title"))
        title = make_title(item, media_type)
        details = clean(item.get("Details"))
        magnet = clean(item.get("MagnetUri"))
        infohash = infohash_from_magnet(magnet)
        size = as_int(item.get("Size"))
        seeders = as_int(item.get("Seeders"))
        leechers = as_int(item.get("Peers"))
        languages = extract_languages(item)
        resolution, codec = actual_resolution(item)
        quality = extract_actual_quality(item, source)
        category_id = "2000" if media_type == "movie" else "5000"

        add_text(node, "title", title)
        guid = ET.SubElement(node, "guid", isPermaLink="false")
        guid.text = infohash or details or magnet or title
        add_text(node, "link", details or magnet)
        add_text(node, "comments", details)
        add_text(node, "description", source)
        add_text(node, "pubDate", parse_publish_date(item.get("PublishDate")))
        add_text(node, "category", category_id)
        if languages:
            add_text(node, "language", ", ".join(languages))

        if magnet:
            ET.SubElement(
                node,
                "enclosure",
                url=magnet,
                length=str(size),
                type="application/x-bittorrent",
            )

        add_attr(node, "category", category_id)
        add_attr(node, "size", size)
        add_attr(node, "seeders", seeders)
        add_attr(node, "leechers", leechers)
        add_attr(node, "peers", seeders + leechers)

        if infohash:
            add_attr(node, "infohash", infohash)
        if magnet:
            add_attr(node, "magneturl", magnet)
        if details:
            add_attr(node, "guid", details)

        for language in languages:
            add_attr(node, "language", language)
        if resolution:
            add_attr(node, "resolution", resolution)
        if codec:
            add_attr(node, "video", codec)
        if quality:
            add_attr(node, "quality", quality)

        year = extract_year(item)
        if year:
            add_attr(node, "year", year)

        for category in sorted(item_categories(item) & MOVIE_CATEGORIES):
            if media_type == "movie" and str(category) != category_id:
                add_attr(node, "category", category)

        if media_type == "tv" and 5070 in item_categories(item):
            add_attr(node, "category", 5070)

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


# ----------------------------------------------------------------
# Local test response
# ----------------------------------------------------------------

def make_test_feed(media_types, requested_categories=None):
    requested_categories = requested_categories or set()
    results = []

    if "movie" in media_types:
        movie_categories = {2000} | (requested_categories & MOVIE_CATEGORIES)
        movie = {
            "PublishDate": "10/03/2019 00:00:00",
            "Title": "The Gentlemen (2019) WEB-DL 720p | JacRed V2 local movie test",
            "Details": "http://127.0.0.1:9128/torznab/api?t=search&q=jacred-v2-local-test",
            "MagnetUri": f"magnet:?xt=urn:btih:{TEST_INFOHASH}&dn=JacRed+V2+local+movie+test",
            "Size": 1,
            "Seeders": 0,
            "Peers": 0,
            "Category": sorted(movie_categories),
            "info": {
                "name": "The Gentlemen",
                "relased": "10/03/2019 00:00:00",
                "quality": "720p",
                "types": ["movie"],
            },
        }
        results.append((movie, "movie"))

    if "tv" in media_types:
        tv_categories = {5000}
        if 5070 in requested_categories:
            tv_categories.add(5070)
        tv = {
            "PublishDate": "10/03/2024 00:00:00",
            "Title": "The Gentlemen (2024) WEB-DL 1080p | JacRed V2 local TV test",
            "Details": "http://127.0.0.1:9128/torznab/api?t=search&q=jacred-v2-local-test-tv",
            "MagnetUri": f"magnet:?xt=urn:btih:{TEST_TV_INFOHASH}&dn=JacRed+V2+local+TV+test",
            "Size": 1,
            "Seeders": 0,
            "Peers": 0,
            "Category": sorted(tv_categories),
            "info": {
                "name": "The Gentlemen",
                "relased": "10/03/2024 00:00:00",
                "quality": "1080p",
                "types": ["series"],
            },
        }
        results.append((tv, "tv"))

    return make_feed(results)


# ----------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_xml(self, xml, content_type="application/xml; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(xml)))
        self.end_headers()
        self.wfile.write(xml)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in {"/torznab/api", "/torznab", "/api"}:
            self.send_error(404)
            return

        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        request_type = params.get("t", ["tvsearch"])[0].lower()

        if request_type == "caps":
            self.send_xml(make_caps())
            return

        if request_type not in {"search", "tvsearch", "movie"}:
            self.send_xml(make_feed([]))
            return

        query = params.get("q", [""])[0].strip()
        requested = {as_int(x) for x in params.get("cat", [""])[0].split(",") if as_int(x)}
        season = params.get("season", [""])[0].strip()
        episode = params.get("ep", [""])[0].strip()
        year = params.get("year", [""])[0].strip()

        if not query:
            has_ids = any(
                params.get(k, [""])[0].strip()
                for k in ("imdbid", "tvdbid", "tmdbid")
            )
            # Empty-q connectivity tests are served locally, with item type
            # and categories matched to the Torznab request.
            if not has_ids and not (season or episode or year):
                if request_type == "movie":
                    test_media_types = {"movie"}
                elif request_type == "tvsearch":
                    test_media_types = {"tv"}
                elif requested:
                    test_media_types = set()
                    if requested & MOVIE_CATEGORIES:
                        test_media_types.add("movie")
                    if requested & TV_CATEGORIES:
                        test_media_types.add("tv")
                else:
                    # A generic Prowlarr test without categories covers both.
                    test_media_types = {"movie", "tv"}

                self.send_xml(
                    make_test_feed(test_media_types, requested),
                    "application/rss+xml; charset=utf-8",
                )
                return

            # A structured search without q cannot be resolved reliably.
            # Never invent a title or send an empty query to JacRed.
            self.send_xml(make_feed([]))
            return

        media_types = requested_media_types(request_type, requested)

        jac_query = query
        if "tv" in media_types:
            jac_query = re.sub(r"(?i)\bS\d{1,3}E\d{1,4}(?:-E?\d{1,4})?\b", " ", jac_query)
            jac_query = re.sub(r"(?i)\b\d{1,3}\s*[xх×]\s*\d{1,4}(?:[-–—]\d{1,4})?\b", " ", jac_query)
            jac_query = re.sub(r"(?i)\b(?:season|сезон)\s*\d{1,3}\b", " ", jac_query)
            jac_query = re.sub(r"\s+", " ", jac_query).strip() or query

        request_cache_key = cache_key(params)
        log_request_diagnostics(params, request_cache_key)

        def build_response():
            try:
                results = []
                errors = []

                for media_type in sorted(media_types):
                    try:
                        upstream_results, source_cache_hit = cached_jacred_results(
                            jac_query, year, media_type
                        )
                        if source_cache_hit:
                            print(
                                f"SOURCE CACHE HIT type={media_type} q={jac_query!r} count={len(upstream_results)}",
                                file=sys.stderr,
                                flush=True,
                            )
                        else:
                            print(
                                f"UPSTREAM OK type={media_type} q={jac_query!r} count={len(upstream_results)}",
                                file=sys.stderr,
                                flush=True,
                            )

                        if media_type == "tv":
                            alias, alias_votes, alias_evidence = find_russian_tv_alias(
                                jac_query, upstream_results
                            )
                            if alias and compact_title(alias) != compact_title(jac_query):
                                canonical = canonical_tv_title(upstream_results, jac_query)
                                print(
                                    f"TV ALIAS DETECTED q={jac_query!r} alias={alias!r} "
                                    f"evidence={alias_votes}/{alias_evidence} canonical={canonical!r}",
                                    file=sys.stderr,
                                    flush=True,
                                )
                                alias_results, alias_cache_hit = cached_jacred_results(
                                    alias, year, media_type
                                )
                                if alias_cache_hit:
                                    print(
                                        f"SOURCE CACHE HIT type={media_type} q={alias!r} count={len(alias_results)}",
                                        file=sys.stderr,
                                        flush=True,
                                    )
                                else:
                                    print(
                                        f"UPSTREAM OK type={media_type} q={alias!r} count={len(alias_results)}",
                                        file=sys.stderr,
                                        flush=True,
                                    )

                                # Keep only additional records tied to the same
                                # canonical series; avoid injecting unrelated
                                # releases returned by a broad Russian phrase.
                                matching_alias_results = [
                                    with_canonical_tv_title(item, canonical)
                                    for item in alias_results
                                    if item_mentions_tv_title(item, canonical)
                                ]
                                upstream_results = list(upstream_results) + matching_alias_results
                                print(
                                    f"TV ALIAS MERGE q={jac_query!r} alias={alias!r} "
                                    f"source_results={len(alias_results)} "
                                    f"matching_results={len(matching_alias_results)}",
                                    file=sys.stderr,
                                    flush=True,
                                )
                    except UpstreamCoolingDown as exc:
                        notify_upstream_problem(exc, media_type)
                        # A circuit-open/cooldown condition is fatal for this
                        # operation: do not start remaining branches or aliases.
                        raise
                    except Exception as exc:
                        notify_upstream_problem(exc, media_type)
                        errors.append((media_type, exc))
                        print(
                            f"UPSTREAM ERROR type={media_type} q={jac_query!r}: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue

                    for item in upstream_results:
                        if item_media_type(item) != media_type:
                            continue
                        if not item_matches_categories(item, requested, media_type):
                            continue
                        if media_type == "tv" and not result_matches_season_episode(item, season, episode):
                            continue
                        results.append((item, media_type))

                if errors and not results:
                    raise errors[0][1]

                unique, seen = [], set()
                for item, media_type in results:
                    key_value = (
                        infohash_from_magnet(item.get("MagnetUri"))
                        or clean(item.get("Details") or item.get("MagnetUri") or item.get("Title")).casefold()
                    )
                    key = (media_type, key_value)
                    if not key_value or key in seen:
                        continue
                    seen.add(key)
                    unique.append((item, media_type))

                limit = max(1, min(as_int(params.get("limit", ["100"])[0], 100), 1000))
                offset = max(0, as_int(params.get("offset", ["0"])[0], 0))
                return (
                    make_feed(unique[offset:offset + limit]),
                    not errors,
                )

            except Exception:
                traceback.print_exc(file=sys.stderr)
                raise

        try:
            xml, from_cache = cached_request(request_cache_key, build_response)
            print(
                f"CACHE {'HIT' if from_cache else 'MISS'} "
                f"type={request_type} q={query!r}",
                file=sys.stderr,
                flush=True,
            )
            self.send_xml(
                xml,
                "application/rss+xml; charset=utf-8",
            )
        except UpstreamCoolingDown as exc:
            body = str(exc).encode("utf-8", errors="replace")
            self.send_response(503)
            self.send_header("Retry-After", str(exc.retry_after_seconds))
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            body = str(exc).encode("utf-8", errors="replace")
            status = 503 if exc.code == 429 else 502
            self.send_response(status)
            if exc.code == 429:
                self.send_header(
                    "Retry-After",
                    str(max(1, int(parse_retry_after(exc) + 0.999))),
                )
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = str(exc).encode("utf-8", errors="replace")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)



if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--reset-upstream-protection":
        reset_protection_state()
        print(f"Upstream protection state reset to CLOSED in {JACRED_STATE_PATH}", flush=True)
        sys.exit(0)

    try:
        initialize_protection_state()
        print(
            f"{SERVER_TITLE} v{VERSION} listening on http://{HOST}:{PORT}/torznab; "
            f"JacRed source={JACRED_URL}; minimum interval={JACRED_MIN_INTERVAL_MS}ms; "
            f"state={JACRED_STATE_PATH}; notifications={'enabled' if JACRED_NOTIFICATIONS_ENABLED else 'disabled'}",
            flush=True,
        )
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        clear_notification_state_prefix("adapter:startup-failure")
        notify_persisted_protection_state()
        server.serve_forever()
    except Exception as exc:
        notify_startup_failure(exc)
        raise
