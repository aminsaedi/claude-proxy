"""Prometheus metrics."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "proxy_requests_total",
    "Total proxy requests by virtual key, model, and HTTP status",
    ["key_name", "model", "status"],
)
INPUT_TOKENS = Counter(
    "proxy_input_tokens_total",
    "Input tokens consumed per virtual key and model",
    ["key_name", "model"],
)
OUTPUT_TOKENS = Counter(
    "proxy_output_tokens_total",
    "Output tokens consumed per virtual key and model",
    ["key_name", "model"],
)
CACHE_READ_TOKENS = Counter(
    "proxy_cache_read_input_tokens_total",
    "Cache-read input tokens per virtual key and model",
    ["key_name", "model"],
)
CACHE_CREATION_TOKENS = Counter(
    "proxy_cache_creation_input_tokens_total",
    "Cache-creation input tokens per virtual key and model",
    ["key_name", "model"],
)
UPSTREAM_UTIL_5H = Gauge(
    "proxy_upstream_utilization_5h_ratio",
    "Upstream OAuth token 5-hour utilization ratio",
    ["token_name"],
)
UPSTREAM_UTIL_7D = Gauge(
    "proxy_upstream_utilization_7d_ratio",
    "Upstream OAuth token 7-day utilization ratio",
    ["token_name"],
)
TOKEN_HEALTHY = Gauge(
    "proxy_token_healthy",
    "Whether an upstream OAuth token is healthy (1=healthy, 0=unhealthy/rate-limited)",
    ["token_name"],
)
AUTO_ROTATIONS = Counter(
    "proxy_auto_rotations_total",
    "Number of automatic token rotations performed",
)
FAILOVERS = Counter(
    "proxy_failovers_total",
    "Per-request failovers to an alternate token after a retryable upstream error",
)
REQUEST_LATENCY = Histogram(
    "proxy_request_latency_seconds",
    "Upstream request latency in seconds (time to first response)",
    ["model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64),
)


def update_util_gauges(token_name: str, headers: dict[str, str]) -> None:
    try:
        u5h = headers.get("anthropic-ratelimit-unified-5h-utilization")
        if u5h is not None:
            UPSTREAM_UTIL_5H.labels(token_name=token_name).set(float(u5h))
        u7d = headers.get("anthropic-ratelimit-unified-7d-utilization")
        if u7d is not None:
            UPSTREAM_UTIL_7D.labels(token_name=token_name).set(float(u7d))
    except (TypeError, ValueError):
        pass
