"""Verify runtime imports, GPU libraries, and Redis cache integrations inside built Ray ML container images."""

from __future__ import annotations

import importlib
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

EXPECTED_REDIS_PORT = 6379
EXPECTED_REDIS_DB = 0


def _verify_remote_cache(
    redis_mod: ModuleType, codecache: ModuleType, remote_cache: ModuleType
) -> None:
    cache = codecache.FxGraphCache.get_remote_cache()
    if not isinstance(cache, remote_cache.RemoteFxGraphCache):
        raise TypeError("TorchInductor did not construct its Redis remote cache")
    backend = cache.backend
    if not isinstance(backend, remote_cache.RedisRemoteCacheBackend):
        raise TypeError("TorchInductor did not construct its Redis backend")

    client = getattr(backend, "_redis", None)
    if not isinstance(client, redis_mod.Redis):
        raise TypeError("TorchInductor Redis backend has no redis-py client")
    connection = client.connection_pool.connection_kwargs
    if (connection.get("host"), connection.get("port"), connection.get("db")) != (
        "127.0.0.1",
        EXPECTED_REDIS_PORT,
        EXPECTED_REDIS_DB,
    ):
        raise RuntimeError("TorchInductor Redis backend did not parse its configured URL")


def _verify_model_compilation(torch_mod: ModuleType) -> None:
    class TinyModel(torch_mod.nn.Module):
        def forward(self, features: torch_mod.Tensor) -> torch_mod.Tensor:
            return torch_mod.sin(features) + 1

    features = torch_mod.tensor([0.0, 1.0], dtype=torch_mod.float32)
    expected = TinyModel()(features)
    compiled = torch_mod.compile(TinyModel(), backend="inductor", fullgraph=True)
    torch_mod.testing.assert_close(compiled(features), expected)


def main() -> None:
    os.environ["TORCHINDUCTOR_FX_GRAPH_REMOTE_CACHE"] = "1"
    os.environ["TORCHINDUCTOR_REDIS_URL"] = "redis://127.0.0.1:6379/0"

    importlib.import_module("ray")
    redis_mod = importlib.import_module("redis")
    torch_mod = importlib.import_module("torch")
    importlib.import_module(sys.argv[1])

    codecache = importlib.import_module("torch._inductor.codecache")
    remote_cache = importlib.import_module("torch._inductor.remote_cache")
    inductor_utils = importlib.import_module("torch._inductor.utils")

    if redis_mod.__version__ != "5.2.1":
        raise RuntimeError(f"unexpected redis-py version {redis_mod.__version__}")
    if not inductor_utils.should_use_remote_fx_graph_cache():
        raise RuntimeError("TorchInductor remote FX graph cache is disabled")

    _verify_remote_cache(redis_mod, codecache, remote_cache)
    _verify_model_compilation(torch_mod)


if __name__ == "__main__":
    main()
