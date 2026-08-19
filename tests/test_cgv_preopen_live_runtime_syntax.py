from __future__ import annotations

from engines.cgv_engine_funnel_runtime import CgvEngine as FunnelCgvEngine
from engines.cgv_engine_preopen_live_runtime import CgvEngine as LiveCgvEngine
from engines.cgv_engine_preopen_sentinel_runtime import CgvEngine as SentinelCgvEngine


def test_live_runtime_keeps_existing_funnel_and_sentinel_layers():
    assert issubclass(SentinelCgvEngine, FunnelCgvEngine)
    assert issubclass(LiveCgvEngine, SentinelCgvEngine)
