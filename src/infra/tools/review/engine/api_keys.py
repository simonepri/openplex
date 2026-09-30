"""Manages API key pools with quota checking, rate-limit header inspection, and smart sorting."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import email.message
import email.utils
import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_UNIT_FACTORS: Mapping[str, float] = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


@dataclasses.dataclass
class KeyQuotaStatus:
    """Represents quota and rate limit status of an API key."""

    key: str
    provider: str
    valid: bool = True
    has_quota: bool = True
    requests_remaining: int | None = None
    tokens_remaining: int | None = None
    reset_time: datetime.datetime | None = None
    error_message: str | None = None

    @property
    def sort_key(self) -> tuple[int, float, int]:
        health_rank = 0 if (self.valid and self.has_quota) else 1
        reset_ts = self.reset_time.timestamp() if self.reset_time else float("inf")
        remaining = -(self.tokens_remaining or 0)
        return (health_rank, reset_ts, remaining)


def parse_keys(*env_vars: str) -> list[str]:
    """Parses API keys or tokens from arbitrary env vars (supporting CSV, JSON arrays, or singular strings)."""
    keys: list[str] = []
    for var in env_vars:
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        if raw.startswith("["):
            with contextlib.suppress(Exception):
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    for item in parsed:
                        cleaned = str(item).strip()
                        if cleaned and cleaned not in keys:
                            keys.append(cleaned)
                    continue
        for k in raw.split(","):
            cleaned = k.strip()
            if cleaned and cleaned not in keys:
                keys.append(cleaned)
    return keys


def parse_duration(val: str) -> datetime.timedelta | None:
    """Parses standard rate limit duration strings like 12s, 5m, 1h, 500ms."""
    if not val:
        return None
    val = val.strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)(ms|s|m|h|d)?$", val)
    if not m:
        return None
    amount = float(m.group(1))
    unit = m.group(2) or "s"
    seconds = amount * _UNIT_FACTORS.get(unit, 1.0)
    return datetime.timedelta(seconds=seconds)


def parse_rfc3339_or_date(val: str) -> datetime.datetime | None:
    """Parses RFC3339 or HTTP-date string."""
    if not val:
        return None
    with contextlib.suppress(Exception):
        return datetime.datetime.fromisoformat(val)
    with contextlib.suppress(Exception):
        return email.utils.parsedate_to_datetime(val)
    return None


def check_anthropic_key(key: str) -> KeyQuotaStatus:
    """Checks quota and rate limit status for an Anthropic API key."""
    status = KeyQuotaStatus(key=key, provider="anthropic")
    url = "https://api.anthropic.com/v1/messages"
    payload = json.dumps({
        "model": "claude-3-5-haiku-latest",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode("utf-8")
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            _parse_anthropic_headers(resp.headers, status)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            status.valid = False
            status.has_quota = False
            status.error_message = "Invalid API key"
            return status
        if e.code == 429:
            status.has_quota = False
            status.error_message = "Rate limit or quota exceeded"
            _parse_anthropic_headers(e.headers, status)
            return status
        _parse_anthropic_headers(e.headers, status)
    except Exception as e:
        status.error_message = str(e)
    return status


def _parse_anthropic_headers(headers: email.message.Message, status: KeyQuotaStatus) -> None:
    now = datetime.datetime.now(datetime.UTC)
    req_rem = headers.get("anthropic-ratelimit-requests-remaining")
    if req_rem:
        with contextlib.suppress(ValueError):
            status.requests_remaining = int(req_rem)
    tok_rem = headers.get("anthropic-ratelimit-tokens-remaining")
    if tok_rem:
        with contextlib.suppress(ValueError):
            status.tokens_remaining = int(tok_rem)

    tok_reset = headers.get("anthropic-ratelimit-tokens-reset")
    if tok_reset:
        status.reset_time = parse_rfc3339_or_date(tok_reset) or (
            now + dur if (dur := parse_duration(tok_reset)) else None
        )

    if not status.reset_time:
        req_reset = headers.get("anthropic-ratelimit-requests-reset")
        if req_reset:
            status.reset_time = parse_rfc3339_or_date(req_reset) or (
                now + dur if (dur := parse_duration(req_reset)) else None
            )

    retry = headers.get("retry-after")
    if retry and (dur := parse_duration(retry)):
        status.reset_time = now + dur


def check_openai_key(key: str) -> KeyQuotaStatus:
    """Checks quota and rate limit status for an OpenAI API key."""
    status = KeyQuotaStatus(key=key, provider="openai")
    url = "https://api.openai.com/v1/models"
    headers = {"Authorization": f"Bearer {key}"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    now = datetime.datetime.now(datetime.UTC)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            _parse_openai_headers(resp.headers, status, now)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            status.valid = False
            status.has_quota = False
            status.error_message = "Invalid API key"
            return status
        if e.code == 429:
            status.has_quota = False
            status.error_message = "Rate limit or quota exceeded"
            _parse_openai_headers(e.headers, status, now)
            return status
        _parse_openai_headers(e.headers, status, now)
    except Exception as e:
        status.error_message = str(e)
    return status


def _parse_openai_headers(
    headers: email.message.Message, status: KeyQuotaStatus, now: datetime.datetime
) -> None:
    req_rem = headers.get("x-ratelimit-remaining-requests")
    if req_rem:
        with contextlib.suppress(ValueError):
            status.requests_remaining = int(req_rem)
    tok_rem = headers.get("x-ratelimit-remaining-tokens")
    if tok_rem:
        with contextlib.suppress(ValueError):
            status.tokens_remaining = int(tok_rem)

    tok_reset = headers.get("x-ratelimit-reset-tokens")
    if tok_reset and (dur := parse_duration(tok_reset)):
        status.reset_time = now + dur
    elif not status.reset_time:
        req_reset = headers.get("x-ratelimit-reset-requests")
        if req_reset and (dur := parse_duration(req_reset)):
            status.reset_time = now + dur

    retry = headers.get("retry-after")
    if retry and (dur := parse_duration(retry)):
        status.reset_time = now + dur


def check_gemini_key(key: str) -> KeyQuotaStatus:
    """Checks quota and rate limit status for a Gemini API key."""
    status = KeyQuotaStatus(key=key, provider="gemini")
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    req = urllib.request.Request(url, method="GET")
    now = datetime.datetime.now(datetime.UTC)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            retry = resp.headers.get("retry-after")
            if retry and (dur := parse_duration(retry)):
                status.reset_time = now + dur
    except urllib.error.HTTPError as e:
        if e.code in {400, 403}:
            err_body = e.read().decode("utf-8", errors="replace")
            if "API_KEY_INVALID" in err_body or "PERMISSION_DENIED" in err_body:
                status.valid = False
                status.has_quota = False
                status.error_message = "Invalid API key"
                return status
        if e.code == 429:
            status.has_quota = False
            status.error_message = "Rate limit or quota exceeded"
            retry = e.headers.get("retry-after")
            if retry and (dur := parse_duration(retry)):
                status.reset_time = now + dur
            return status
    except Exception as e:
        status.error_message = str(e)
    return status


def select_best_key(provider: str, keys: list[str]) -> str:
    """Checks usage/quota on available keys and returns the best one (earliest reset & remaining quota)."""
    if not keys:
        raise ValueError(f"No keys supplied for provider {provider}")
    if len(keys) == 1:
        return keys[0]

    checker = {
        "anthropic": check_anthropic_key,
        "openai": check_openai_key,
        "gemini": check_gemini_key,
    }.get(provider.lower())

    if not checker:
        return keys[0]

    statuses: list[KeyQuotaStatus] = []
    for k in keys:
        try:
            st = checker(k)
        except Exception as e:
            st = KeyQuotaStatus(key=k, provider=provider, error_message=str(e))
        statuses.append(st)

    statuses.sort(key=lambda s: s.sort_key)
    best = statuses[0]
    logger.info(
        "Selected best API key for %s (valid=%s, has_quota=%s, reset=%s)",
        provider,
        best.valid,
        best.has_quota,
        best.reset_time,
    )
    return best.key
