#!/usr/bin/env python3
"""Exports Antigravity CLI telemetry to an OpenTelemetry collector.

A standard-library-only reimplementation of the SigNoz Antigravity CLI hook:
https://signoz.io/docs/antigravity-cli-monitoring/
Preserves SigNoz span names, attribute keys, and quota metric formats.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ENDPOINT: str = "http://otel-collector.otel-system.svc.cluster.local:4318"
SERVICE_NAME: str = "antigravity-cli"
QUOTA_INTERVAL: int = 300

SIGNOZ_HOOK_CONFIG: dict[str, object] = {
    "enabled": True,
    "PostToolUse": [
        {
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": "/usr/bin/python3 /usr/local/lib/agy-otel/hook.py PostToolUse",
                    "timeout": 10,
                }
            ],
        }
    ],
    "PostInvocation": [
        {
            "type": "command",
            "command": "/usr/bin/python3 /usr/local/lib/agy-otel/hook.py PostInvocation",
            "timeout": 10,
        }
    ],
    "Stop": [
        {
            "type": "command",
            "command": "/usr/bin/python3 /usr/local/lib/agy-otel/hook.py Stop",
            "timeout": 10,
        }
    ],
}


def get_endpoint(subpath: str) -> str:
    return f"{ENDPOINT}/{subpath}"


def get_stamp_path() -> Path:
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    base_dir = (
        Path(xdg_runtime)
        if xdg_runtime and Path(xdg_runtime).is_dir()
        else Path(tempfile.gettempdir())
    )
    return base_dir / "agy-otel" / ".quota-last"


def quota_due(stamp_path: Path | str | None = None) -> bool:
    target_path = Path(stamp_path) if stamp_path is not None else get_stamp_path()
    try:
        if time.time() - target_path.stat().st_mtime < QUOTA_INTERVAL:
            return False
    except OSError:
        pass
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.touch()
    except OSError:
        pass
    return True


def _format_any_value(val: object) -> dict[str, object]:
    if isinstance(val, bool):
        return {"boolValue": val}
    if isinstance(val, int):
        return {"intValue": str(val)}
    if isinstance(val, float):
        return {"doubleValue": float(val)}
    if isinstance(val, str):
        return {"stringValue": val}
    return {"stringValue": str(val)}


def _format_attributes(attrs: dict[str, object]) -> list[dict[str, object]]:
    return [{"key": k, "value": _format_any_value(v)} for k, v in attrs.items() if v is not None]


def build_trace_payload(
    event: str,
    payload: dict[str, object],
    now: float | None = None,
    span_id: str | None = None,
) -> dict[str, object]:
    ts = now if now is not None else time.time()
    sid = span_id if span_id is not None else os.urandom(8).hex()

    conv = str(payload.get("conversationId") or "unknown")
    digest = hashlib.sha256(conv.encode("utf-8")).hexdigest()
    trace_id = digest[:32]
    parent_span_id = digest[32:48]

    tool_call = payload.get("toolCall")
    tool_call_name = tool_call.get("name") if isinstance(tool_call, dict) else None
    tool = payload.get("toolName") or tool_call_name
    if event == "PostToolUse":
        name = f"execute_tool {tool or 'unknown'}"
        op = "execute_tool"
    elif event == "Stop":
        name = "agy stop"
        op = "invoke_agent"
    else:
        name = "agy invocation"
        op = "invoke_agent"

    attrs: dict[str, object] = {
        "gen_ai.operation.name": op,
        "gen_ai.provider.name": "gcp.gemini",
        "gen_ai.conversation.id": conv,
        "agy.hook.event": event,
    }
    if payload.get("modelName"):
        attrs["gen_ai.request.model"] = payload["modelName"]

    for key, attr in (
        ("stepIdx", "agy.step.index"),
        ("invocationNum", "agy.invocation.num"),
        ("initialNumSteps", "agy.initial_num_steps"),
        ("executionNum", "agy.execution.num"),
        ("terminationReason", "agy.termination_reason"),
        ("fullyIdle", "agy.fully_idle"),
    ):
        if payload.get(key) is not None:
            attrs[attr] = payload[key]

    if tool:
        attrs["gen_ai.tool.name"] = tool

    err = payload.get("error")
    status: dict[str, object]
    if err:
        err_str = str(err)[:200]
        attrs["error.type"] = err_str
        status = {"code": 2, "message": err_str}
    else:
        status = {"code": 1}

    time_ns = str(int(ts * 1e9))

    span = {
        "traceId": trace_id,
        "spanId": sid,
        "parentSpanId": parent_span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": time_ns,
        "endTimeUnixNano": time_ns,
        "attributes": _format_attributes(attrs),
        "status": status,
    }

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": _format_attributes({"service.name": SERVICE_NAME}),
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "antigravity-cli-hooks",
                        },
                        "spans": [span],
                    }
                ],
            }
        ]
    }


def _extract_bucket_data_points(
    bucket: dict[str, object],
    group_name: str,
    time_ns: str,
    now: float,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    attrs: dict[str, object] = {
        "agy.quota.group": group_name,
        "agy.quota.bucket": bucket.get("id", ""),
        "agy.quota.window": bucket.get("window", ""),
    }
    formatted_attrs = _format_attributes(attrs)

    rem_dp: dict[str, object] | None = None
    frac = bucket.get("remaining_fraction")
    if isinstance(frac, (int, float, str)):
        with contextlib.suppress(ValueError):
            rem_dp = {
                "timeUnixNano": time_ns,
                "asDouble": float(frac),
                "attributes": formatted_attrs,
            }

    reset_dp: dict[str, object] | None = None
    reset = bucket.get("reset_time")
    if isinstance(reset, str) and reset:
        with contextlib.suppress(Exception):
            ts = datetime.datetime.fromisoformat(reset)
            seconds_left = max(0.0, ts.timestamp() - now)
            reset_dp = {
                "timeUnixNano": time_ns,
                "asDouble": float(seconds_left),
                "attributes": formatted_attrs,
            }

    return rem_dp, reset_dp


def _collect_quota_data_points(
    groups: object,
    time_ns: str,
    now: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    remaining_dps: list[dict[str, object]] = []
    reset_dps: list[dict[str, object]] = []
    if not isinstance(groups, list):
        return remaining_dps, reset_dps

    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "")
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            rem_dp, reset_dp = _extract_bucket_data_points(bucket, group_name, time_ns, now)
            if rem_dp is not None:
                remaining_dps.append(rem_dp)
            if reset_dp is not None:
                reset_dps.append(reset_dp)

    return remaining_dps, reset_dps


def build_metrics_payload(
    usage_data: dict[str, object],
    now: float | None = None,
) -> dict[str, object] | None:
    ts = now if now is not None else time.time()
    time_ns = str(int(ts * 1e9))

    remaining_dps, reset_dps = _collect_quota_data_points(usage_data.get("groups"), time_ns, ts)

    metrics: list[dict[str, object]] = []
    if remaining_dps:
        metrics.append({
            "name": "agy.quota.remaining_fraction",
            "description": "Fraction of the Antigravity weekly quota still available",
            "unit": "1",
            "gauge": {
                "dataPoints": remaining_dps,
            },
        })
    if reset_dps:
        metrics.append({
            "name": "agy.quota.seconds_to_reset",
            "description": "Seconds until the Antigravity quota window resets",
            "unit": "s",
            "gauge": {
                "dataPoints": reset_dps,
            },
        })

    if not metrics:
        return None

    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": _format_attributes({"service.name": SERVICE_NAME}),
                },
                "scopeMetrics": [
                    {
                        "scope": {
                            "name": "antigravity-cli-quota",
                        },
                        "metrics": metrics,
                    }
                ],
            }
        ]
    }


def send_otlp(url: str, payload: dict[str, object]) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


def emit_quota() -> None:
    try:
        proc = subprocess.run(
            ["agy", "-p", "/usage", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            return
        parsed = json.loads(proc.stdout)
        data = parsed.get("command", {}).get("data", {})
        payload = build_metrics_payload(data)
        if payload is not None:
            url = get_endpoint("v1/metrics")
            send_otlp(url, payload)
    except Exception:
        pass


def emit(event: str, payload: dict[str, object]) -> None:
    try:
        trace_payload = build_trace_payload(event, payload)
        traces_url = get_endpoint("v1/traces")
        send_otlp(traces_url, trace_payload)

        if event == "Stop" and quota_due():
            emit_quota()
    except Exception:
        pass


def install_hooks(hooks_path: Path | str | None = None) -> int:
    try:
        target_path = (
            Path(hooks_path)
            if hooks_path is not None
            else Path("~/.gemini/config/hooks.json").expanduser()
        )

        existing_data: dict[str, object] = {}
        if target_path.is_file():
            try:
                content = target_path.read_text(encoding="utf-8")
                parsed = json.loads(content) if content.strip() else {}
                if not isinstance(parsed, dict):
                    sys.stderr.write(
                        f"Warning: {target_path} is not a JSON object, leaving untouched\n"
                    )
                    return 0
                existing_data = {str(key): value for key, value in parsed.items()}
            except Exception as e:
                sys.stderr.write(
                    f"Warning: failed reading existing hooks file {target_path}, leaving untouched: {e}\n"
                )
                return 0

        existing_data["signoz-otel"] = SIGNOZ_HOOK_CONFIG

        target_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = target_path.with_name(f"{target_path.name}.tmp.{os.getpid()}")
        tmp_path.write_text(json.dumps(existing_data, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(target_path)
    except Exception as e:
        sys.stderr.write(f"Warning: failed to install hooks: {e}\n")
    return 0


def main() -> None:
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "install":
            install_hooks()
            sys.exit(0)

        event = sys.argv[1] if len(sys.argv) > 1 else "PostInvocation"
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        try:
            pid = os.fork()
            if pid == 0:
                os.setsid()
                pid2 = os.fork()
                if pid2 == 0:
                    try:
                        emit(event, payload)
                    except Exception:
                        pass
                    finally:
                        os._exit(0)
                os._exit(0)
            else:
                os.waitpid(pid, 0)
        except Exception:
            pass

        print("{}")
        sys.exit(0)
    except Exception:
        with contextlib.suppress(Exception):
            print("{}")
        sys.exit(0)


if __name__ == "__main__":
    main()
