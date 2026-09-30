"""State and rate limiting tracker for the Coder workspace healer."""

from __future__ import annotations

import abc
import copy
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol, override

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from http.client import HTTPMessage

DEFAULT_ROLLING_WINDOW_SECONDS: float = 6 * 3600.0
DEFAULT_MAX_HEALS_PER_WINDOW: int = 2
DEFAULT_MAX_FLEET_HEALS_PER_RUN: int = 5
DEFAULT_NAMESPACE: str = "coder"
DEFAULT_CONFIGMAP_NAME: str = "workspace-healer-state"
DEFAULT_DATA_KEY: str = "state.json"
SERVICE_ACCOUNT_TOKEN_PATH: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
SERVICE_ACCOUNT_CA_PATH: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


class HealerStateError(Exception):
    """Base error for workspace healer state operations."""


class StateBackendError(HealerStateError):
    """Error during state backend persistence or loading."""


class RejectAPIRedirects(urllib.request.HTTPRedirectHandler):
    """Prevent HTTP redirects from leaking authorization tokens."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl


@dataclass
class WorkspaceState:
    """State record for a single workspace."""

    miss_count: int = 0
    first_miss_at: float | None = None
    last_miss_at: float | None = None
    heal_history: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Convert workspace state to a JSON-compatible dictionary."""
        return {
            "first_miss_at": self.first_miss_at,
            "heal_history": list(self.heal_history),
            "last_miss_at": self.last_miss_at,
            "miss_count": self.miss_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> WorkspaceState:
        """Construct workspace state from a dictionary."""
        first_miss = data.get("first_miss_at")
        last_miss = data.get("last_miss_at")
        history_raw = data.get("heal_history", [])
        heal_history: list[float] = []
        if isinstance(history_raw, list):
            for item in history_raw:
                if isinstance(item, (int, float)):
                    heal_history.append(float(item))
        raw_miss = data.get("miss_count", 0)
        miss_count = int(raw_miss) if isinstance(raw_miss, (int, str)) else 0
        return cls(
            miss_count=miss_count,
            first_miss_at=float(first_miss) if isinstance(first_miss, (int, float)) else None,
            last_miss_at=float(last_miss) if isinstance(last_miss, (int, float)) else None,
            heal_history=heal_history,
        )


def count_heals_in_window(
    heal_timestamps: Sequence[float],
    window_seconds: float,
    now: float,
) -> int:
    """Count heal events occurring within the rolling window ending at now."""
    cutoff = now - window_seconds
    return sum(1 for ts in heal_timestamps if cutoff <= ts <= now)


def evaluate_healing_rate_limits(
    workspace_heals: Sequence[float],
    current_run_heals: int,
    max_fleet_heals: int,
    max_workspace_heals: int,
    window_seconds: float,
    now: float,
) -> tuple[bool, str]:
    """Pure evaluation of fleet concurrency and per-workspace rolling window limits."""
    if current_run_heals >= max_fleet_heals:
        return False, (
            f"fleet concurrency limit reached ({current_run_heals}/{max_fleet_heals} "
            "heals in current run)"
        )

    recent_heals = count_heals_in_window(workspace_heals, window_seconds, now)
    if recent_heals >= max_workspace_heals:
        window_hours = window_seconds / 3600.0
        return False, (
            f"workspace heal rate limit reached ({recent_heals}/{max_workspace_heals} "
            f"heals in rolling {window_hours:g}h window)"
        )

    return True, "permitted"


def serialize_state(
    workspaces: Mapping[str, WorkspaceState],
    *,
    now: float,
) -> dict[str, object]:
    """Pure serialization of workspace states to dictionary."""
    return {
        "updated_at": now,
        "version": 1,
        "workspaces": {ws_id: ws_state.to_dict() for ws_id, ws_state in sorted(workspaces.items())},
    }


def deserialize_state(
    data: Mapping[str, object],
) -> dict[str, WorkspaceState]:
    """Pure deserialization of state dictionary to workspace state mapping."""
    workspaces_raw = data.get("workspaces") if "workspaces" in data else data
    if not isinstance(workspaces_raw, dict):
        raise HealerStateError(
            f"Invalid state data structure: expected dict, got {type(workspaces_raw).__name__}"
        )

    result: dict[str, WorkspaceState] = {}
    for key, value in workspaces_raw.items():
        if isinstance(value, dict):
            result[str(key)] = WorkspaceState.from_dict(value)
    return result


class StateBackend(abc.ABC):
    """Abstract interface for healer state persistence backends."""

    @abc.abstractmethod
    def load(self) -> dict[str, object] | None:
        """Load state dictionary from the backend, or None if no state is stored."""

    @abc.abstractmethod
    def save(self, data: Mapping[str, object]) -> None:
        """Persist state dictionary to the backend."""


class InMemoryBackend(StateBackend):
    """In-memory backend storing state in a dictionary (for testing and dry runs)."""

    def __init__(self, initial_data: Mapping[str, object] | None = None) -> None:
        self._data: dict[str, object] = (
            copy.deepcopy(dict(initial_data)) if initial_data is not None else {}
        )

    @override
    def load(self) -> dict[str, object] | None:
        return copy.deepcopy(self._data)

    @override
    def save(self, data: Mapping[str, object]) -> None:
        self._data = copy.deepcopy(dict(data))


class FileBackend(StateBackend):
    """JSON file storage backend with atomic file replacement."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @override
    def load(self) -> dict[str, object] | None:
        if not self.path.exists():
            return None
        content = self.path.read_text(encoding="utf-8")
        if not content.strip():
            return None
        try:
            data = json.loads(content)
        except json.JSONDecodeError as error:
            raise StateBackendError(f"Corrupted JSON in state file {self.path}: {error}") from error
        if not isinstance(data, dict):
            raise StateBackendError(f"State file {self.path} must contain a JSON object")
        return {str(k): v for k, v in data.items()}

    @override
    def save(self, data: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
        serialized = json.dumps(data, indent=2, sort_keys=True)
        try:
            temp_file.write_text(serialized, encoding="utf-8")
            temp_file.replace(self.path)
        except OSError as error:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise StateBackendError(f"Failed to write state file {self.path}: {error}") from error


class KubernetesAPIClient(Protocol):
    """Protocol defining Kubernetes API interactions for ConfigMap management."""

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        content_type: str = "application/json",
        accepted: set[int] | None = None,
    ) -> tuple[int, object | None]: ...


class DefaultKubernetesAPIClient:
    """In-cluster Kubernetes API client using projected service account tokens."""

    def __init__(
        self,
        *,
        host: str | None = None,
        port: str | int | None = None,
        token: str | None = None,
        token_path: Path | str = SERVICE_ACCOUNT_TOKEN_PATH,
        ca_cert_path: Path | str = SERVICE_ACCOUNT_CA_PATH,
        timeout: float = 20.0,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        service_host = host or os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        service_port = port or os.environ.get(
            "KUBERNETES_SERVICE_PORT_HTTPS",
            os.environ.get("KUBERNETES_SERVICE_PORT", "443"),
        )
        self.url = f"https://{service_host}:{service_port}"
        self.timeout = timeout

        if token is not None:
            self._token = token.strip()
        else:
            tpath = Path(token_path)
            self._token = tpath.read_text(encoding="utf-8").strip() if tpath.is_file() else ""

        if opener is not None:
            self.opener = opener
        else:
            cpath = Path(ca_cert_path)
            ssl_context = (
                ssl.create_default_context(cafile=str(cpath))
                if cpath.is_file()
                else ssl.create_default_context()
            )
            self.opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ssl_context),
                RejectAPIRedirects(),
            )

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        content_type: str = "application/json",
        accepted: set[int] | None = None,
    ) -> tuple[int, object | None]:
        expected = accepted if accepted is not None else {200}
        url = urllib.parse.urljoin(f"{self.url.rstrip('/')}/", path.lstrip("/"))
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Accept": "application/json",
            "Content-Type": content_type,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                status = response.status
                raw = response.read()
                data = json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as error:
            if error.code in expected:
                raw = error.read()
                try:
                    data = json.loads(raw.decode("utf-8")) if raw else None
                except (ValueError, UnicodeDecodeError):
                    data = None
                return error.code, data
            raise StateBackendError(
                f"Kubernetes API returned HTTP {error.code} for {method} {path}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise StateBackendError(
                f"Kubernetes API communication error for {method} {path}: {error}"
            ) from error

        if status not in expected:
            raise StateBackendError(
                f"Kubernetes API returned unexpected status {status} for {method} {path}"
            )
        return int(status), data


class ConfigMapBackend(StateBackend):
    """Persists state to a Kubernetes ConfigMap in the specified namespace."""

    def __init__(
        self,
        name: str = DEFAULT_CONFIGMAP_NAME,
        namespace: str = DEFAULT_NAMESPACE,
        *,
        data_key: str = DEFAULT_DATA_KEY,
        client: KubernetesAPIClient | None = None,
    ) -> None:
        self.name = name
        self.namespace = namespace
        self.data_key = data_key
        self.client: KubernetesAPIClient = (
            client if client is not None else DefaultKubernetesAPIClient()
        )

    @override
    def load(self) -> dict[str, object] | None:
        path = f"/api/v1/namespaces/{self.namespace}/configmaps/{self.name}"
        status, response = self.client.request("GET", path, accepted={200, 404})
        if status == 404 or not isinstance(response, dict):
            return None

        data = response.get("data")
        if not isinstance(data, dict) or self.data_key not in data:
            return None

        raw_content = data[self.data_key]
        if not isinstance(raw_content, str):
            raise StateBackendError(f"ConfigMap key '{self.data_key}' is not a string")

        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as error:
            raise StateBackendError(
                f"Failed to decode JSON from ConfigMap '{self.name}': {error}"
            ) from error

        if not isinstance(parsed, dict):
            raise StateBackendError(
                f"Expected JSON object in ConfigMap data, got {type(parsed).__name__}"
            )
        return {str(k): v for k, v in parsed.items()}

    @override
    def save(self, data: Mapping[str, object]) -> None:
        serialized = json.dumps(data, indent=2, sort_keys=True)
        path = f"/api/v1/namespaces/{self.namespace}/configmaps/{self.name}"
        patch_payload = {
            "apiVersion": "v1",
            "data": {
                self.data_key: serialized,
            },
            "kind": "ConfigMap",
            "metadata": {
                "name": self.name,
                "namespace": self.namespace,
            },
        }
        status, _ = self.client.request(
            "PATCH",
            path,
            payload=patch_payload,
            content_type="application/merge-patch+json",
            accepted={200, 404},
        )
        if status == 404:
            create_path = f"/api/v1/namespaces/{self.namespace}/configmaps"
            create_payload = {
                "apiVersion": "v1",
                "data": {
                    self.data_key: serialized,
                },
                "kind": "ConfigMap",
                "metadata": {
                    "name": self.name,
                    "namespace": self.namespace,
                },
            }
            self.client.request(
                "POST",
                create_path,
                payload=create_payload,
                content_type="application/json",
                accepted={200, 201},
            )


class HealerStateTracker:
    """Tracks workspace health misses, heal history, and enforces rate limits."""

    def __init__(
        self,
        backend: StateBackend | None = None,
        *,
        max_heals_per_window: int = DEFAULT_MAX_HEALS_PER_WINDOW,
        rolling_window_seconds: float = DEFAULT_ROLLING_WINDOW_SECONDS,
        max_fleet_heals_per_run: int = DEFAULT_MAX_FLEET_HEALS_PER_RUN,
    ) -> None:
        self.backend: StateBackend = backend if backend is not None else InMemoryBackend()
        self.max_heals_per_window = max_heals_per_window
        self.rolling_window_seconds = rolling_window_seconds
        self.max_fleet_heals_per_run = max_fleet_heals_per_run
        self._workspaces: dict[str, WorkspaceState] = {}
        self._current_run_heals: int = 0

    @property
    def current_run_heals(self) -> int:
        """Number of heals executed in the current execution run across the fleet."""
        return self._current_run_heals

    def reset_run_counters(self) -> None:
        """Reset execution run counters for a new execution cycle."""
        self._current_run_heals = 0

    def _get_or_create(self, workspace_id: str) -> WorkspaceState:
        if workspace_id not in self._workspaces:
            self._workspaces[workspace_id] = WorkspaceState()
        return self._workspaces[workspace_id]

    def record_miss(self, workspace_id: str, timestamp: float | None = None) -> int:
        """Record a consecutive miss for a workspace and return the updated miss count."""
        ts = time.time() if timestamp is None else timestamp
        state = self._get_or_create(workspace_id)
        state.miss_count += 1
        if state.first_miss_at is None:
            state.first_miss_at = ts
        state.last_miss_at = ts
        return state.miss_count

    def record_connected(self, workspace_id: str) -> None:
        """Reset consecutive miss count when workspace agent is observed connected."""
        state = self._workspaces.get(workspace_id)
        if state is not None:
            state.miss_count = 0
            state.first_miss_at = None
            state.last_miss_at = None

    def record_healthy(self, workspace_id: str) -> None:
        """Alias for record_connected."""
        self.record_connected(workspace_id)

    def get_miss_count(self, workspace_id: str) -> int:
        """Get the consecutive miss count for a workspace."""
        state = self._workspaces.get(workspace_id)
        return state.miss_count if state is not None else 0

    def get_misses(self, workspace_id: str) -> int:
        """Alias for get_miss_count."""
        return self.get_miss_count(workspace_id)

    def prune_inactive_workspaces(self, active_workspace_ids: set[str]) -> int:
        """Remove state for workspaces that no longer exist and have no heal history."""
        stale_ids = [
            wid
            for wid, st in self._workspaces.items()
            if wid not in active_workspace_ids and not st.heal_history
        ]
        for wid in stale_ids:
            del self._workspaces[wid]
        return len(stale_ids)

    def get_miss_timestamps(self, workspace_id: str) -> tuple[float | None, float | None]:
        """Get (first_miss_at, last_miss_at) for a workspace."""
        state = self._workspaces.get(workspace_id)
        if state is None:
            return None, None
        return state.first_miss_at, state.last_miss_at

    def get_heal_history(self, workspace_id: str) -> list[float]:
        """Get the list of heal timestamps recorded for a workspace."""
        state = self._workspaces.get(workspace_id)
        return list(state.heal_history) if state is not None else []

    def can_heal(self, workspace_id: str, timestamp: float | None = None) -> bool:
        """Check if workspace can be healed under fleet concurrency and rolling window limits."""
        can, _ = self.can_heal_with_reason(workspace_id, timestamp)
        return can

    def can_heal_with_reason(
        self,
        workspace_id: str,
        timestamp: float | None = None,
    ) -> tuple[bool, str]:
        """Evaluate rate limits and return permission status with descriptive message."""
        ts = time.time() if timestamp is None else timestamp
        state = self._workspaces.get(workspace_id)
        history = state.heal_history if state is not None else ()
        return evaluate_healing_rate_limits(
            workspace_heals=history,
            current_run_heals=self._current_run_heals,
            max_fleet_heals=self.max_fleet_heals_per_run,
            max_workspace_heals=self.max_heals_per_window,
            window_seconds=self.rolling_window_seconds,
            now=ts,
        )

    def record_heal(self, workspace_id: str, timestamp: float | None = None) -> None:
        """Record a heal execution for a workspace and increment the run heal counter."""
        ts = time.time() if timestamp is None else timestamp
        state = self._get_or_create(workspace_id)
        state.heal_history.append(ts)
        self._current_run_heals += 1

    def prune_heal_history(
        self,
        older_than_seconds: float | None = None,
        timestamp: float | None = None,
    ) -> int:
        """Prune heal events older than the retention threshold across all workspaces."""
        ts = time.time() if timestamp is None else timestamp
        retention = (
            older_than_seconds if older_than_seconds is not None else self.rolling_window_seconds
        )
        cutoff = ts - retention
        pruned_count = 0
        for state in self._workspaces.values():
            initial_count = len(state.heal_history)
            state.heal_history = [t for t in state.heal_history if t >= cutoff]
            pruned_count += initial_count - len(state.heal_history)
        return pruned_count

    def load(self) -> None:
        """Load state from configured backend."""
        data = self.backend.load()
        if data is not None:
            self._workspaces = deserialize_state(data)

    def save(self, timestamp: float | None = None) -> None:
        """Persist state to configured backend."""
        self.backend.save(self.to_dict(timestamp=timestamp))

    def to_dict(self, timestamp: float | None = None) -> dict[str, object]:
        """Serialize state to a JSON-compatible dictionary."""
        ts = time.time() if timestamp is None else timestamp
        return serialize_state(self._workspaces, now=ts)

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, object],
        *,
        backend: StateBackend | None = None,
        max_heals_per_window: int = DEFAULT_MAX_HEALS_PER_WINDOW,
        rolling_window_seconds: float = DEFAULT_ROLLING_WINDOW_SECONDS,
        max_fleet_heals_per_run: int = DEFAULT_MAX_FLEET_HEALS_PER_RUN,
    ) -> HealerStateTracker:
        """Instantiate tracker populated with dictionary data."""
        tracker = cls(
            backend=backend,
            max_heals_per_window=max_heals_per_window,
            rolling_window_seconds=rolling_window_seconds,
            max_fleet_heals_per_run=max_fleet_heals_per_run,
        )
        tracker._workspaces = deserialize_state(data)
        return tracker

    def to_json(self, *, indent: int | None = 2, timestamp: float | None = None) -> str:
        """Serialize state to formatted JSON string."""
        return json.dumps(self.to_dict(timestamp=timestamp), indent=indent, sort_keys=True)

    @classmethod
    def from_json(
        cls,
        json_str: str,
        *,
        backend: StateBackend | None = None,
        max_heals_per_window: int = DEFAULT_MAX_HEALS_PER_WINDOW,
        rolling_window_seconds: float = DEFAULT_ROLLING_WINDOW_SECONDS,
        max_fleet_heals_per_run: int = DEFAULT_MAX_FLEET_HEALS_PER_RUN,
    ) -> HealerStateTracker:
        """Instantiate tracker parsed from a JSON string."""
        data = json.loads(json_str)
        if not isinstance(data, dict):
            raise HealerStateError(
                f"Expected JSON object in state string, got {type(data).__name__}"
            )
        return cls.from_dict(
            data,
            backend=backend,
            max_heals_per_window=max_heals_per_window,
            rolling_window_seconds=rolling_window_seconds,
            max_fleet_heals_per_run=max_fleet_heals_per_run,
        )

    def save_to_file(self, path: Path | str, *, timestamp: float | None = None) -> None:
        """Save state to a local JSON file."""
        FileBackend(path).save(self.to_dict(timestamp=timestamp))

    @classmethod
    def load_from_file(
        cls,
        path: Path | str,
        *,
        backend: StateBackend | None = None,
        max_heals_per_window: int = DEFAULT_MAX_HEALS_PER_WINDOW,
        rolling_window_seconds: float = DEFAULT_ROLLING_WINDOW_SECONDS,
        max_fleet_heals_per_run: int = DEFAULT_MAX_FLEET_HEALS_PER_RUN,
    ) -> HealerStateTracker:
        """Load tracker directly from a local JSON file."""
        file_backend = FileBackend(path)
        data = file_backend.load()
        if data is None:
            raise FileNotFoundError(f"State file not found or empty: {path}")
        return cls.from_dict(
            data,
            backend=backend or file_backend,
            max_heals_per_window=max_heals_per_window,
            rolling_window_seconds=rolling_window_seconds,
            max_fleet_heals_per_run=max_fleet_heals_per_run,
        )
