"""
Loki HTTP API client (read-only).

Used endpoint: GET /loki/api/v1/query_range. We always use query_range rather
than the instant `query` endpoint, because "what did this service say in the
five minutes before the alert fired" is inherently a range question.

Verified facts about what is in Loki in this cluster:

  * Log lines are JSON emitted by python-json-logger with fields
    `timestamp`, `level`, `name`, `message`, `service`, and `request_id`.
    Outside a request the `request_id` key is **absent**, not null — so a
    LogQL filter like `| request_id = ""` will not match those lines.
  * Available Loki *labels* (the indexed, streamable ones):
    `app`, `pod`, `container`, `namespace`, `level`, `request_id`.
    Anything else has to come out of the JSON body via `| json`.
  * Access-log lines have `message="http_request"` plus `method`, `path`,
    `status_code`, `duration_ms`.

**The single most important thing in this file**: chaos-injected 503s are
counted in Prometheus (`http_requests_total{status="503"}` and
`chaos_injections_total`) but produce **NO access-log line at all**. The
chaos middleware short-circuits and returns a JSONResponse *before* the
access-log middleware ever runs. So during a chaos-driven error spike you
will see a large 5xx rate in metrics and near-silence in Loki. That absence
is EXPECTED and is itself a strong signal for "chaos fault" rather than
"real application error" — it is not a broken log pipeline, and Sentinel must
not report it as one. See `looks_like_chaos_silence()` below.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Loki label values are matched with Go RE2 in LogQL stream selectors. As with
# PromQL, everything we interpolate comes from Alertmanager labels or our own
# allow-lists, never from model output — but we escape anyway.
_ESCAPES = str.maketrans({'"': None, "\\": None, "\n": " ", "\r": " ", "{": None, "}": None})


def escape_label_value(value: str) -> str:
    return value.translate(_ESCAPES)


class LokiClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def query_range(
        self,
        logql: str,
        start: float,
        end: float,
        limit: int = 200,
        direction: str = "backward",
    ) -> list[dict[str, Any]]:
        """Run a LogQL range query and flatten the streams into log entries.

        Returns a list of ``{"ts": float_epoch, "labels": {...}, "line": str,
        "json": {...}|None}``. Never raises: Loki being unavailable must not
        stop a remediation, it just means the evidence bundle records the
        gap in `Evidence.errors`.

        Loki wants nanosecond timestamps for start/end.
        """
        params = {
            "query": logql,
            "start": str(int(start * 1_000_000_000)),
            "end": str(int(end * 1_000_000_000)),
            "limit": str(limit),
            "direction": direction,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/loki/api/v1/query_range", params=params
                )
            if resp.status_code != 200:
                logger.warning(
                    "loki_query_http_error",
                    extra={"status_code": resp.status_code, "logql": logql[:300]},
                )
                return []
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "loki_unreachable",
                extra={"error_detail": str(exc)[:300], "logql": logql[:300]},
            )
            return []

        entries: list[dict[str, Any]] = []
        for stream in payload.get("data", {}).get("result", []) or []:
            labels = stream.get("stream") or {}
            for value in stream.get("values") or []:
                # Each value is ["<ns epoch as string>", "<raw line>"].
                if not isinstance(value, list) or len(value) < 2:
                    continue
                try:
                    ts = int(value[0]) / 1_000_000_000
                except (TypeError, ValueError):
                    ts = 0.0
                line = value[1]
                parsed: dict[str, Any] | None
                try:
                    candidate = json.loads(line)
                    parsed = candidate if isinstance(candidate, dict) else None
                except (ValueError, TypeError):
                    # Not every line is our JSON: uvicorn's startup banner and
                    # any third-party library that bypasses our handler emit
                    # plain text. Keep the raw line; do not drop it.
                    parsed = None
                entries.append(
                    {"ts": ts, "labels": labels, "line": line, "json": parsed}
                )
        entries.sort(key=lambda e: e["ts"])
        return entries

    # ---- domain queries -------------------------------------------------
    async def recent_errors(
        self, app: str | None, namespace: str, start: float, end: float, limit: int = 100
    ) -> list[dict[str, Any]]:
        """WARN/ERROR/CRITICAL lines for a service.

        `level` is an indexed Loki label here, so this is a cheap stream
        selector rather than a line filter — important because Sentinel runs
        this during an active incident when Loki is already under load.
        """
        parts = [f'namespace="{escape_label_value(namespace)}"']
        if app:
            parts.append(f'app="{escape_label_value(app)}"')
        parts.append('level=~"WARNING|ERROR|CRITICAL"')
        logql = "{" + ",".join(parts) + "}"
        return await self.query_range(logql, start, end, limit=limit)

    async def recent_lines(
        self, app: str | None, namespace: str, start: float, end: float, limit: int = 200
    ) -> list[dict[str, Any]]:
        """All lines for a service, any level."""
        parts = [f'namespace="{escape_label_value(namespace)}"']
        if app:
            parts.append(f'app="{escape_label_value(app)}"')
        logql = "{" + ",".join(parts) + "}"
        return await self.query_range(logql, start, end, limit=limit)

    async def access_log_errors(
        self, app: str | None, namespace: str, start: float, end: float, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Access-log lines with a 5xx status_code.

        `status_code` is inside the JSON body, not a Loki label, so we have to
        `| json` and then filter. Note the comparison is numeric here
        (`status_code >= 500`) because python-json-logger writes it as a JSON
        number; if it were a string this would need `=~"5.."` instead.

        Expect this to return NOTHING during a chaos 5xx storm — see the
        module docstring. That is not a bug.
        """
        parts = [f'namespace="{escape_label_value(namespace)}"']
        if app:
            parts.append(f'app="{escape_label_value(app)}"')
        selector_str = "{" + ",".join(parts) + "}"
        logql = (
            f'{selector_str} | json | message="http_request" | status_code >= 500'
        )
        return await self.query_range(logql, start, end, limit=limit)


def summarise(entries: list[dict[str, Any]], max_samples: int = 10) -> list[str]:
    """Compress log entries into a short list of distinct messages.

    Deduplicated on the `message` field, keeping first-seen order and a count,
    because a leak or a crash loop produces the same line thousands of times
    and neither a human nor an LLM prompt benefits from all of them.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        body = entry.get("json") or {}
        message = body.get("message") or entry.get("line") or ""
        if not isinstance(message, str):
            message = str(message)
        key = message.strip()[:200]
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    ordered = list(counts.items())[:max_samples]
    return [f"{msg} (x{count})" for msg, count in ordered]


def looks_like_chaos_silence(
    error_rate: float | None, access_log_5xx_count: int, threshold: float = 0.05
) -> bool:
    """True when metrics show a 5xx spike but Loki has no matching 5xx lines.

    This is the fingerprint of chaos-injected failures: the chaos middleware
    returns before the access-log middleware, so the request is counted but
    never logged. A *real* application 5xx always produces an access-log line
    with status_code >= 500.

    So: high error rate + zero 5xx access-log lines is strong evidence of a
    deliberate chaos fault rather than an application regression. It is also
    the single most misleading thing in this stack if you do not know about
    it, which is why it gets its own named function instead of being buried
    in an `if` inside rca.py.
    """
    if error_rate is None:
        return False
    return error_rate > threshold and access_log_5xx_count == 0
