"""Defend workspace healer miss tracking, heal rate limiting, and state backends."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from workspace_healer_state import (
    DEFAULT_CONFIGMAP_NAME,
    DEFAULT_NAMESPACE,
    ConfigMapBackend,
    FileBackend,
    HealerStateError,
    HealerStateTracker,
    InMemoryBackend,
    StateBackendError,
    evaluate_healing_rate_limits,
)


class FakeKubernetesClient:
    """In-memory Kubernetes API client simulating ConfigMap GET, POST, and PATCH operations."""

    def __init__(self, initial_resources: dict[str, dict[str, object]] | None = None) -> None:
        self.resources: dict[str, dict[str, object]] = (
            copy.deepcopy(initial_resources) if initial_resources is not None else {}
        )
        self.calls: list[tuple[str, str, object | None]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        content_type: str = "application/json",
        accepted: set[int] | None = None,
    ) -> tuple[int, object | None]:
        del content_type
        expected = accepted if accepted is not None else {200}
        self.calls.append((method, path, copy.deepcopy(payload)))

        if method == "GET":
            if path in self.resources:
                return 200, copy.deepcopy(self.resources[path])
            if 404 in expected:
                return 404, None
            raise StateBackendError(f"HTTP 404 Not Found: {path}")

        if method == "PATCH":
            if path in self.resources:
                assert isinstance(payload, dict)
                target = self.resources[path]
                for key, val in payload.items():
                    if (
                        key == "data"
                        and isinstance(val, dict)
                        and "data" in target
                        and isinstance(target["data"], dict)
                    ):
                        target["data"].update(val)
                    else:
                        target[key] = val
                return 200, copy.deepcopy(self.resources[path])
            if 404 in expected:
                return 404, None
            raise StateBackendError(f"HTTP 404 Not Found for PATCH: {path}")

        if method == "POST":
            assert isinstance(payload, dict)
            metadata = payload.get("metadata")
            assert isinstance(metadata, dict)
            name = metadata.get("name")
            target_path = f"{path.rstrip('/')}/{name}"
            self.resources[target_path] = copy.deepcopy(payload)
            return 201, copy.deepcopy(payload)

        raise AssertionError(f"Unexpected method in FakeKubernetesClient: {method} {path}")


class HealerStateTrackerTest(unittest.TestCase):
    """Defend miss counting, rate limiting rules, and persistence across backends."""

    def test_record_miss_increments_count_and_preserves_first_timestamp(self) -> None:
        tracker = HealerStateTracker()

        first_count = tracker.record_miss("ws-1", timestamp=100.0)
        self.assertEqual(first_count, 1)
        self.assertEqual(tracker.get_miss_count("ws-1"), 1)
        self.assertEqual(tracker.get_miss_timestamps("ws-1"), (100.0, 100.0))

        second_count = tracker.record_miss("ws-1", timestamp=160.0)
        self.assertEqual(second_count, 2)
        self.assertEqual(tracker.get_miss_count("ws-1"), 2)
        self.assertEqual(tracker.get_miss_timestamps("ws-1"), (100.0, 160.0))

    def test_record_connected_resets_miss_tracking_for_workspace(self) -> None:
        tracker = HealerStateTracker()
        tracker.record_miss("ws-1", timestamp=100.0)
        tracker.record_miss("ws-1", timestamp=120.0)
        self.assertEqual(tracker.get_miss_count("ws-1"), 2)

        tracker.record_connected("ws-1")
        self.assertEqual(tracker.get_miss_count("ws-1"), 0)
        self.assertEqual(tracker.get_misses("ws-1"), 0)
        self.assertEqual(tracker.get_miss_timestamps("ws-1"), (None, None))

        tracker.record_miss("ws-2", timestamp=100.0)
        tracker.record_healthy("ws-2")
        self.assertEqual(tracker.get_miss_count("ws-2"), 0)

        tracker.record_connected("ws-unknown")
        self.assertEqual(tracker.get_miss_count("ws-unknown"), 0)

    def test_prune_inactive_workspaces_preserves_healing_history(self) -> None:
        tracker = HealerStateTracker()
        tracker.record_miss("ws-deleted", timestamp=100.0)
        tracker.record_miss("ws-active", timestamp=100.0)
        tracker.record_heal("ws-with-heals", timestamp=100.0)

        pruned = tracker.prune_inactive_workspaces({"ws-active"})
        self.assertEqual(pruned, 1)
        self.assertEqual(tracker.get_miss_count("ws-deleted"), 0)
        self.assertEqual(tracker.get_miss_count("ws-active"), 1)
        self.assertEqual(len(tracker.get_heal_history("ws-with-heals")), 1)

    def test_can_heal_rejects_when_workspace_rolling_window_limit_reached(self) -> None:
        tracker = HealerStateTracker(
            max_heals_per_window=2,
            rolling_window_seconds=6 * 3600.0,
            max_fleet_heals_per_run=10,
        )
        base_time = 1000.0

        self.assertTrue(tracker.can_heal("ws-1", timestamp=base_time))

        tracker.record_heal("ws-1", timestamp=base_time)
        self.assertTrue(tracker.can_heal("ws-1", timestamp=base_time + 3600.0))

        tracker.record_heal("ws-1", timestamp=base_time + 3600.0)

        can_heal, reason = tracker.can_heal_with_reason("ws-1", timestamp=base_time + 7200.0)
        self.assertFalse(can_heal)
        self.assertIn("workspace heal rate limit reached", reason)

        self.assertTrue(tracker.can_heal("ws-2", timestamp=base_time + 7200.0))

    def test_can_heal_allows_heal_after_rolling_window_elapses(self) -> None:
        window_seconds = 6 * 3600.0
        tracker = HealerStateTracker(
            max_heals_per_window=2,
            rolling_window_seconds=window_seconds,
            max_fleet_heals_per_run=10,
        )
        t0 = 1000.0
        tracker.record_heal("ws-1", timestamp=t0)
        tracker.record_heal("ws-1", timestamp=t0 + 3600.0)

        eval_time = t0 + window_seconds + 1.0
        self.assertTrue(tracker.can_heal("ws-1", timestamp=eval_time))

    def test_can_heal_rejects_when_fleet_concurrency_limit_reached(self) -> None:
        tracker = HealerStateTracker(
            max_heals_per_window=5,
            rolling_window_seconds=6 * 3600.0,
            max_fleet_heals_per_run=2,
        )
        t0 = 5000.0

        self.assertTrue(tracker.can_heal("ws-1", timestamp=t0))
        tracker.record_heal("ws-1", timestamp=t0)
        self.assertEqual(tracker.current_run_heals, 1)

        self.assertTrue(tracker.can_heal("ws-2", timestamp=t0))
        tracker.record_heal("ws-2", timestamp=t0)
        self.assertEqual(tracker.current_run_heals, 2)

        can_heal, reason = tracker.can_heal_with_reason("ws-3", timestamp=t0)
        self.assertFalse(can_heal)
        self.assertIn("fleet concurrency limit reached", reason)

        tracker.reset_run_counters()
        self.assertEqual(tracker.current_run_heals, 0)
        self.assertTrue(tracker.can_heal("ws-3", timestamp=t0))

    def test_prune_heal_history_removes_expired_timestamps(self) -> None:
        tracker = HealerStateTracker(rolling_window_seconds=3600.0)
        tracker.record_heal("ws-1", timestamp=100.0)
        tracker.record_heal("ws-1", timestamp=2000.0)
        tracker.record_heal("ws-1", timestamp=4500.0)

        pruned = tracker.prune_heal_history(older_than_seconds=3600.0, timestamp=5000.0)
        self.assertEqual(pruned, 1)
        self.assertEqual(tracker.get_heal_history("ws-1"), [2000.0, 4500.0])

    def test_in_memory_backend_persistence_roundtrip(self) -> None:
        backend = InMemoryBackend()
        tracker1 = HealerStateTracker(backend=backend)
        tracker1.record_miss("ws-a", timestamp=100.0)
        tracker1.record_heal("ws-a", timestamp=150.0)
        tracker1.save(timestamp=200.0)

        tracker2 = HealerStateTracker(backend=backend)
        tracker2.load()

        self.assertEqual(tracker2.get_miss_count("ws-a"), 1)
        self.assertEqual(tracker2.get_heal_history("ws-a"), [150.0])
        self.assertEqual(tracker2.get_miss_timestamps("ws-a"), (100.0, 100.0))

    def test_file_backend_saves_and_loads_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "state.json"
            tracker1 = HealerStateTracker(backend=FileBackend(file_path))
            tracker1.record_miss("ws-disk", timestamp=300.0)
            tracker1.record_heal("ws-disk", timestamp=350.0)
            tracker1.save(timestamp=400.0)

            self.assertTrue(file_path.exists())
            raw_content = file_path.read_text(encoding="utf-8")
            self.assertIn("ws-disk", raw_content)

            tracker2 = HealerStateTracker.load_from_file(file_path)
            self.assertEqual(tracker2.get_miss_count("ws-disk"), 1)
            self.assertEqual(tracker2.get_heal_history("ws-disk"), [350.0])

    def test_file_backend_rejects_corrupted_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "corrupted.json"
            file_path.write_text("{broken json", encoding="utf-8")

            backend = FileBackend(file_path)
            with self.assertRaises(StateBackendError):
                backend.load()

    def test_configmap_backend_creates_configmap_on_first_save(self) -> None:
        client = FakeKubernetesClient()
        backend = ConfigMapBackend(
            name=DEFAULT_CONFIGMAP_NAME,
            namespace=DEFAULT_NAMESPACE,
            client=client,
        )

        self.assertIsNone(backend.load())

        test_state = {"version": 1, "workspaces": {"ws-1": {"miss_count": 3}}}
        backend.save(test_state)

        cm_path = f"/api/v1/namespaces/{DEFAULT_NAMESPACE}/configmaps/{DEFAULT_CONFIGMAP_NAME}"
        self.assertIn(cm_path, client.resources)

        loaded = backend.load()
        self.assertEqual(loaded, test_state)

    def test_configmap_backend_updates_existing_configmap(self) -> None:
        client = FakeKubernetesClient()
        backend = ConfigMapBackend(
            name=DEFAULT_CONFIGMAP_NAME,
            namespace=DEFAULT_NAMESPACE,
            client=client,
        )

        backend.save({"version": 1, "workspaces": {}})
        updated_state = {
            "version": 1,
            "workspaces": {"ws-updated": {"heal_history": [10.0], "miss_count": 5}},
        }
        backend.save(updated_state)

        loaded = backend.load()
        self.assertEqual(loaded, updated_state)

    def test_to_json_and_from_json_serialization_roundtrip(self) -> None:
        tracker1 = HealerStateTracker()
        tracker1.record_miss("ws-json", timestamp=100.0)
        tracker1.record_heal("ws-json", timestamp=200.0)

        json_repr = tracker1.to_json(timestamp=300.0)
        tracker2 = HealerStateTracker.from_json(json_repr)

        self.assertEqual(tracker2.get_miss_count("ws-json"), 1)
        self.assertEqual(tracker2.get_heal_history("ws-json"), [200.0])
        self.assertEqual(tracker2.get_miss_timestamps("ws-json"), (100.0, 100.0))

    def test_from_json_rejects_non_object(self) -> None:
        with self.assertRaises(HealerStateError):
            HealerStateTracker.from_json('["an", "array"]')

    def test_evaluate_healing_rate_limits_pure_function(self) -> None:
        now = 10000.0
        window = 3600.0

        can, reason = evaluate_healing_rate_limits(
            workspace_heals=[],
            current_run_heals=3,
            max_fleet_heals=3,
            max_workspace_heals=2,
            window_seconds=window,
            now=now,
        )
        self.assertFalse(can)
        self.assertIn("fleet concurrency limit reached", reason)

        can, reason = evaluate_healing_rate_limits(
            workspace_heals=[now - 100, now - 50],
            current_run_heals=0,
            max_fleet_heals=5,
            max_workspace_heals=2,
            window_seconds=window,
            now=now,
        )
        self.assertFalse(can)
        self.assertIn("workspace heal rate limit reached", reason)

        can, reason = evaluate_healing_rate_limits(
            workspace_heals=[now - 4000],
            current_run_heals=0,
            max_fleet_heals=5,
            max_workspace_heals=2,
            window_seconds=window,
            now=now,
        )
        self.assertTrue(can)
        self.assertEqual(reason, "permitted")


if __name__ == "__main__":
    unittest.main()
