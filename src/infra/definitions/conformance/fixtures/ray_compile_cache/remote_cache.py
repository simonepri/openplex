"""Exercises remote FX graph caching across Ray Train processes to defend distributed compilation artifact sharing and fallback behavior against cache miss faults."""

from __future__ import annotations

import hashlib
import os
import sys


def main() -> None:
    expected_cache_state = sys.argv[1]
    if expected_cache_state not in {"fallback", "reader", "writer"}:
        raise ValueError(f"unknown cache state {expected_cache_state}")

    import torch
    from torch._dynamo.utils import counters
    from torch._inductor import remote_cache

    counters.clear()
    remote_cache.cache_stats._stats.clear()

    token_hash = hashlib.sha256(os.environ["CACHE_TOKEN"].encode()).digest()
    feature_count = 17 + int.from_bytes(token_hash[:2], "big")
    offset = (int.from_bytes(token_hash[2:8], "big") + 1) / (1 << 48)

    class CacheProbe(torch.nn.Module):
        def forward(self, features: torch.Tensor) -> torch.Tensor:
            return torch.sin(features) + offset

    features = torch.linspace(0, 1, feature_count, dtype=torch.float32)
    expected = CacheProbe()(features)
    compiled = torch.compile(CacheProbe(), backend="inductor", fullgraph=True)
    torch.testing.assert_close(compiled(features), expected)

    graph_counters = counters["inductor"]
    backend_stats = remote_cache.cache_stats._stats["backend:RedisRemoteCacheBackend"]
    if expected_cache_state == "reader":
        if graph_counters["fxgraph_cache_hit"] < 1 or backend_stats.hit < 1:
            raise RuntimeError("the second process did not reuse the remote FX graph")
    else:
        if graph_counters["fxgraph_cache_miss"] < 1 or backend_stats.miss < 1:
            raise RuntimeError(f"the {expected_cache_state} process did not cache-miss")
        if expected_cache_state == "writer" and backend_stats.put < 1:
            raise RuntimeError("the first process did not write the remote FX graph")
        if backend_stats.hit:
            raise RuntimeError(f"the {expected_cache_state} process unexpectedly cache-hit")


if __name__ == "__main__":
    main()
