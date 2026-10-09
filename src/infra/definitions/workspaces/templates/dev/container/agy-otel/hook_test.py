#!/usr/bin/env python3
"""Validates Antigravity CLI telemetry hook payload transformation, installation idempotency, and error tolerance."""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import hook


def _first_span(trace: dict[str, object]) -> dict[str, object]:
    res_spans = trace["resourceSpans"]
    assert isinstance(res_spans, list)
    first_res = res_spans[0]
    assert isinstance(first_res, dict)
    scope_spans = first_res["scopeSpans"]
    assert isinstance(scope_spans, list)
    first_scope = scope_spans[0]
    assert isinstance(first_scope, dict)
    spans = first_scope["spans"]
    assert isinstance(spans, list)
    span = spans[0]
    assert isinstance(span, dict)
    return {str(k): v for k, v in span.items()}


def _scope_metrics(metrics_payload: dict[str, object]) -> list[dict[str, object]]:
    res_metrics = metrics_payload["resourceMetrics"]
    assert isinstance(res_metrics, list)
    first_res = res_metrics[0]
    assert isinstance(first_res, dict)
    scope_metrics = first_res["scopeMetrics"]
    assert isinstance(scope_metrics, list)
    return [
        {str(k): v for k, v in item.items()} for item in scope_metrics if isinstance(item, dict)
    ]


class HookTest(unittest.TestCase):
    def test_post_tool_use_payload_shape(self) -> None:
        payload: dict[str, object] = {
            "conversationId": "test-conversation-42",
            "modelName": "gemini-3.1-pro-high",
            "toolName": "read_file",
            "stepIdx": 2,
            "invocationNum": 1,
        }
        trace = hook.build_trace_payload(
            "PostToolUse",
            payload,
            now=1700000000.123,
            span_id="0123456789abcdef",
        )

        res_spans = trace["resourceSpans"]
        assert isinstance(res_spans, list)
        self.assertEqual(len(res_spans), 1)

        first_res = res_spans[0]
        assert isinstance(first_res, dict)
        resource = first_res["resource"]
        assert isinstance(resource, dict)
        res_attrs_list = resource["attributes"]
        assert isinstance(res_attrs_list, list)

        res_attrs: dict[str, object] = {}
        for item in res_attrs_list:
            if isinstance(item, dict):
                key = str(item.get("key"))
                res_attrs[key] = item.get("value")
        self.assertEqual(res_attrs["service.name"], {"stringValue": "antigravity-cli"})

        scope_spans = first_res["scopeSpans"]
        assert isinstance(scope_spans, list)
        self.assertEqual(len(scope_spans), 1)
        first_scope = scope_spans[0]
        assert isinstance(first_scope, dict)
        scope = first_scope["scope"]
        assert isinstance(scope, dict)
        self.assertEqual(scope["name"], "antigravity-cli-hooks")

        span = _first_span(trace)

        # Verify traceId: 32-hex character string
        trace_id = str(span["traceId"])
        self.assertEqual(len(trace_id), 32)
        int(trace_id, 16)

        # Verify spanId and parentSpanId: 16-hex character strings
        span_id = str(span["spanId"])
        parent_span_id = str(span["parentSpanId"])
        self.assertEqual(len(span_id), 16)
        int(span_id, 16)
        self.assertEqual(len(parent_span_id), 16)
        int(parent_span_id, 16)

        self.assertEqual(span["name"], "execute_tool read_file")
        self.assertEqual(span["kind"], 1)

        # Verify timestamps are strings of digits
        start_time = str(span["startTimeUnixNano"])
        end_time = str(span["endTimeUnixNano"])
        self.assertTrue(start_time.isdigit())
        self.assertEqual(start_time, end_time)

        raw_attrs = span["attributes"]
        assert isinstance(raw_attrs, list)
        span_attrs: dict[str, object] = {}
        for item in raw_attrs:
            if isinstance(item, dict):
                key = str(item.get("key"))
                span_attrs[key] = item.get("value")

        self.assertEqual(span_attrs["gen_ai.operation.name"], {"stringValue": "execute_tool"})
        self.assertEqual(span_attrs["gen_ai.provider.name"], {"stringValue": "gcp.gemini"})
        self.assertEqual(
            span_attrs["gen_ai.conversation.id"], {"stringValue": "test-conversation-42"}
        )
        self.assertEqual(span_attrs["gen_ai.request.model"], {"stringValue": "gemini-3.1-pro-high"})
        self.assertEqual(span_attrs["gen_ai.tool.name"], {"stringValue": "read_file"})
        self.assertEqual(span_attrs["agy.hook.event"], {"stringValue": "PostToolUse"})
        self.assertEqual(span_attrs["agy.step.index"], {"intValue": "2"})
        self.assertEqual(span_attrs["agy.invocation.num"], {"intValue": "1"})

    def test_post_invocation_payload_shape(self) -> None:
        payload: dict[str, object] = {
            "conversationId": "test-conv-invocation",
            "modelName": "gemini-flash",
            "invocationNum": 3,
            "stepIdx": 5,
        }
        trace = hook.build_trace_payload(
            "PostInvocation",
            payload,
            now=1700000000.0,
        )
        span = _first_span(trace)
        self.assertEqual(span["name"], "agy invocation")

        raw_attrs = span["attributes"]
        assert isinstance(raw_attrs, list)
        span_attrs: dict[str, object] = {}
        for item in raw_attrs:
            if isinstance(item, dict):
                key = str(item.get("key"))
                span_attrs[key] = item.get("value")

        self.assertEqual(span_attrs["gen_ai.operation.name"], {"stringValue": "invoke_agent"})
        self.assertEqual(span_attrs["agy.hook.event"], {"stringValue": "PostInvocation"})
        self.assertEqual(span_attrs["agy.invocation.num"], {"intValue": "3"})

    def test_stop_payload_shape(self) -> None:
        payload: dict[str, object] = {
            "conversationId": "test-conv-stop",
            "terminationReason": "NO_TOOL_CALL",
            "fullyIdle": True,
            "executionNum": 7,
            "initialNumSteps": 10,
        }
        trace = hook.build_trace_payload(
            "Stop",
            payload,
            now=1700000000.0,
        )
        span = _first_span(trace)
        self.assertEqual(span["name"], "agy stop")

        raw_attrs = span["attributes"]
        assert isinstance(raw_attrs, list)
        span_attrs: dict[str, object] = {}
        for item in raw_attrs:
            if isinstance(item, dict):
                key = str(item.get("key"))
                span_attrs[key] = item.get("value")

        self.assertEqual(span_attrs["gen_ai.operation.name"], {"stringValue": "invoke_agent"})
        self.assertEqual(span_attrs["agy.hook.event"], {"stringValue": "Stop"})
        self.assertEqual(span_attrs["agy.termination_reason"], {"stringValue": "NO_TOOL_CALL"})
        self.assertEqual(span_attrs["agy.fully_idle"], {"boolValue": True})
        self.assertEqual(span_attrs["agy.execution.num"], {"intValue": "7"})
        self.assertEqual(span_attrs["agy.initial_num_steps"], {"intValue": "10"})

    def test_error_attribute_and_status(self) -> None:
        payload: dict[str, object] = {
            "conversationId": "test-err",
            "error": "Process timed out after 30 seconds",
        }
        trace = hook.build_trace_payload("PostToolUse", payload)
        span = _first_span(trace)

        raw_attrs = span["attributes"]
        assert isinstance(raw_attrs, list)
        span_attrs: dict[str, object] = {}
        for item in raw_attrs:
            if isinstance(item, dict):
                key = str(item.get("key"))
                span_attrs[key] = item.get("value")

        self.assertEqual(
            span_attrs["error.type"],
            {"stringValue": "Process timed out after 30 seconds"},
        )
        status = span["status"]
        assert isinstance(status, dict)
        self.assertEqual(status["code"], 2)
        self.assertEqual(status["message"], "Process timed out after 30 seconds")

    def test_metrics_payload_shape(self) -> None:
        usage_data: dict[str, object] = {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "window": "weekly",
                            "remaining_fraction": 0.85,
                            "reset_time": "2026-10-14T06:51:12Z",
                        }
                    ],
                }
            ]
        }
        now_ts = 1791960000.0
        metrics_payload = hook.build_metrics_payload(
            usage_data,
            now=now_ts,
        )
        self.assertIsNotNone(metrics_payload)
        assert metrics_payload is not None

        scope_metrics = _scope_metrics(metrics_payload)
        self.assertEqual(len(scope_metrics), 1)
        first_scope = scope_metrics[0]
        assert isinstance(first_scope, dict)
        scope = first_scope["scope"]
        assert isinstance(scope, dict)
        self.assertEqual(scope["name"], "antigravity-cli-quota")

        raw_metrics = first_scope["metrics"]
        assert isinstance(raw_metrics, list)
        metrics_by_name: dict[str, dict[str, object]] = {}
        for item in raw_metrics:
            if isinstance(item, dict):
                metrics_by_name[str(item.get("name"))] = item

        self.assertIn("agy.quota.remaining_fraction", metrics_by_name)
        self.assertIn("agy.quota.seconds_to_reset", metrics_by_name)

        remaining = metrics_by_name["agy.quota.remaining_fraction"]
        self.assertEqual(remaining["unit"], "1")
        gauge_rem = remaining["gauge"]
        assert isinstance(gauge_rem, dict)
        dps_rem = gauge_rem["dataPoints"]
        assert isinstance(dps_rem, list)
        rem_dp = dps_rem[0]
        assert isinstance(rem_dp, dict)
        self.assertEqual(rem_dp["asDouble"], 0.85)
        self.assertTrue(isinstance(rem_dp["timeUnixNano"], str))

        attrs_rem_list = rem_dp["attributes"]
        assert isinstance(attrs_rem_list, list)
        attrs: dict[str, object] = {}
        for a in attrs_rem_list:
            if isinstance(a, dict):
                attrs[str(a.get("key"))] = a.get("value")

        self.assertEqual(attrs["agy.quota.group"], {"stringValue": "Gemini Models"})
        self.assertEqual(attrs["agy.quota.bucket"], {"stringValue": "gemini-weekly"})
        self.assertEqual(attrs["agy.quota.window"], {"stringValue": "weekly"})

        reset_metric = metrics_by_name["agy.quota.seconds_to_reset"]
        self.assertEqual(reset_metric["unit"], "s")
        gauge_reset = reset_metric["gauge"]
        assert isinstance(gauge_reset, dict)
        dps_reset = gauge_reset["dataPoints"]
        assert isinstance(dps_reset, list)
        reset_dp = dps_reset[0]
        assert isinstance(reset_dp, dict)
        as_double = reset_dp["asDouble"]
        assert isinstance(as_double, float)
        self.assertGreaterEqual(as_double, 0.0)

    def test_install_merge_idempotent_and_keeps_foreign_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            hooks_file = Path(tmp_dir) / "hooks.json"
            initial_data = {
                "user-custom-hook": {
                    "enabled": True,
                    "command": "/usr/bin/custom-script",
                }
            }
            hooks_file.write_text(json.dumps(initial_data), encoding="utf-8")

            # First install
            code = hook.install_hooks(hooks_file)
            self.assertEqual(code, 0)

            installed_data = json.loads(hooks_file.read_text(encoding="utf-8"))

            # Must preserve foreign hooks
            self.assertIn("user-custom-hook", installed_data)
            self.assertEqual(installed_data["user-custom-hook"], initial_data["user-custom-hook"])

            # Must contain our signoz-otel hook configuration
            self.assertIn("signoz-otel", installed_data)
            signoz_cfg = installed_data["signoz-otel"]
            assert isinstance(signoz_cfg, dict)
            self.assertTrue(signoz_cfg["enabled"])
            post_tool = signoz_cfg["PostToolUse"]
            assert isinstance(post_tool, list)
            self.assertEqual(
                post_tool[0]["hooks"][0]["command"],
                "/usr/bin/python3 /usr/local/lib/agy-otel/hook.py PostToolUse",
            )
            post_inv = signoz_cfg["PostInvocation"]
            assert isinstance(post_inv, list)
            self.assertEqual(
                post_inv[0]["command"],
                "/usr/bin/python3 /usr/local/lib/agy-otel/hook.py PostInvocation",
            )
            stop_cfg = signoz_cfg["Stop"]
            assert isinstance(stop_cfg, list)
            self.assertEqual(
                stop_cfg[0]["command"],
                "/usr/bin/python3 /usr/local/lib/agy-otel/hook.py Stop",
            )

            # Second install to verify idempotency
            code2 = hook.install_hooks(hooks_file)
            self.assertEqual(code2, 0)

            second_data = json.loads(hooks_file.read_text(encoding="utf-8"))
            self.assertEqual(installed_data, second_data)

    def test_install_leaves_unparseable_or_non_object_hooks_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            unparseable_inputs = [
                "{ not valid json at all",
                "[1, 2, 3]",
                '"just a string"',
            ]
            for content in unparseable_inputs:
                hooks_file = Path(tmp_dir) / "hooks.json"
                hooks_file.write_text(content, encoding="utf-8")

                with mock.patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                    code = hook.install_hooks(hooks_file)
                    self.assertEqual(code, 0)
                    self.assertIn("Warning:", mock_stderr.getvalue())

                # The file must remain completely untouched
                self.assertEqual(hooks_file.read_text(encoding="utf-8"), content)

    def test_main_never_raises_on_garbage_stdin(self) -> None:
        garbage_inputs = [
            "",
            "   \n",
            "{ not valid json",
            "[1, 2, 3]",
            "null",
            "true",
            "12345",
        ]
        for garbage in garbage_inputs:
            with mock.patch("sys.stdin", io.StringIO(garbage)):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    with mock.patch("os.fork", return_value=123):
                        with mock.patch("os.waitpid", return_value=(123, 0)):
                            with self.assertRaises(SystemExit) as cm:
                                hook.main()
                            self.assertEqual(cm.exception.code, 0)
                            self.assertEqual(mock_stdout.getvalue().strip(), "{}")

    def test_quota_due_rate_limiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stamp_file = Path(tmp_dir) / ".quota-last"

            # Stamp doesn't exist: should return True and create stamp
            self.assertTrue(hook.quota_due(stamp_file))
            self.assertTrue(stamp_file.is_file())

            # Immediate second call: mtime is fresh, should return False
            self.assertFalse(hook.quota_due(stamp_file))

            # Set mtime to 301 seconds ago
            old_time = time.time() - 301.0
            os.utime(stamp_file, (old_time, old_time))

            # Past quota interval: should return True
            self.assertTrue(hook.quota_due(stamp_file))


if __name__ == "__main__":
    unittest.main()
