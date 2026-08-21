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
COST_USD = Counter(
    "proxy_cost_usd_total",
    "Estimated spend in USD per virtual key and model, priced at request time",
    ["key_name", "model"],
)
LIMIT_BLOCKS = Counter(
    "proxy_limit_blocks_total",
    "Requests rejected because a virtual key was over one of its spend limits",
    ["key_name", "period"],
)
KEY_SPEND_USD = Gauge(
    "proxy_key_window_spend_usd",
    "Spend in the current limit window, per virtual key and period",
    ["key_name", "period"],
)
KEY_LIMIT_USD = Gauge(
    "proxy_key_window_limit_usd",
    "Configured spend cap for the current window, per virtual key and period",
    ["key_name", "period"],
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
    "End-to-end request duration in seconds (for streams, until the last byte)",
    ["model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128, 300),
)
UPSTREAM_TTFB = Histogram(
    "proxy_upstream_ttfb_seconds",
    "Time to the upstream response headers — the part of latency the proxy "
    "and upstream control, isolated from how long a completion takes to generate",
    ["model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32),
)
AUDIT_QUEUED = Gauge(
    "proxy_audit_queue_depth",
    "Audit records waiting to be written by the background writer thread",
)
AUDIT_WRITTEN = Gauge(
    "proxy_audit_records_written",
    "Audit records written to disk since this process started",
)
AUDIT_DROPPED = Gauge(
    "proxy_audit_records_dropped",
    "Audit records discarded because the queue was full (never blocks a request)",
)
AUDIT_BYTES = Gauge(
    "proxy_audit_db_bytes",
    "On-disk size of the audit database",
)
AUDIT_ROWS = Gauge(
    "proxy_audit_rows",
    "Number of requests currently retained in the audit database",
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
