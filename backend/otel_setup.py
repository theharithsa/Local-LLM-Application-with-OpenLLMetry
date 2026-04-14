"""
Reusable Dynatrace-compatible OpenTelemetry bootstrap for GenAI applications.

Usage:
    from otel_setup import init_observability, GenAIMetrics

    otel = init_observability("my-app")
    metrics = GenAIMetrics(otel.meter)
"""

import logging
import os
from dataclasses import dataclass, field
from typing import NamedTuple

from traceloop.sdk import Traceloop

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.metrics.view import (
    ExplicitBucketHistogramAggregation,
    View,
)

# ---------------------------------------------------------------------------
# OTel GenAI semantic convention histogram bucket boundaries
# ---------------------------------------------------------------------------
DURATION_BUCKETS = [
    0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56,
    5.12, 10.24, 20.48, 40.96, 81.92,
]
TOKEN_BUCKETS = [
    1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144,
    1048576, 4194304, 16777216, 67108864,
]
TTFT_BUCKETS = [
    0.001, 0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.25,
    0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0,
]
TPOT_BUCKETS = [
    0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5,
    0.75, 1.0, 2.5,
]


class OTelComponents(NamedTuple):
    meter: metrics.Meter
    tracer: trace.Tracer
    logger: logging.Logger


@dataclass
class GenAIMetrics:
    """GenAI semconv + operational metric instruments."""

    # Initialized from a Meter in __post_init__
    _meter: metrics.Meter = field(repr=False)

    # GenAI semconv (created in __post_init__)
    operation_duration: Histogram = field(init=False)
    token_usage: Histogram = field(init=False)
    ttft: Histogram = field(init=False)
    tpot: Histogram = field(init=False)

    # Operational
    request_count: Counter = field(init=False)
    error_count: Counter = field(init=False)
    active_requests: UpDownCounter = field(init=False)
    stream_chunks: Counter = field(init=False)
    token_throughput: Histogram = field(init=False)
    message_count: Histogram = field(init=False)

    def __post_init__(self):
        m = self._meter
        self.operation_duration = m.create_histogram(
            "gen_ai.client.operation.duration", "GenAI operation duration", "s")
        self.token_usage = m.create_histogram(
            "gen_ai.client.token.usage", "Input and output token counts", "{token}")
        self.ttft = m.create_histogram(
            "gen_ai.server.time_to_first_token", "Time to first token", "s")
        self.tpot = m.create_histogram(
            "gen_ai.server.time_per_output_token", "Time per output token", "s")
        self.request_count = m.create_counter(
            "llm.request.count", "Total LLM requests", "1")
        self.error_count = m.create_counter(
            "llm.request.errors", "Failed LLM requests", "1")
        self.active_requests = m.create_up_down_counter(
            "llm.request.active", "In-flight LLM requests", "1")
        self.stream_chunks = m.create_counter(
            "llm.stream.chunks", "Streaming chunks sent", "1")
        self.token_throughput = m.create_histogram(
            "llm.token.throughput", "Output token throughput", "{token}/s")
        self.message_count = m.create_histogram(
            "llm.request.message_count", "Messages in prompt", "1")


def _init_metrics(app_name: str, otlp_base: str, otlp_token: str) -> metrics.Meter:
    """Configure MeterProvider with Dynatrace-required DELTA temporality."""
    exporter = OTLPMetricExporter(
        endpoint=f"{otlp_base}/v1/metrics",
        headers={"Authorization": f"Api-Token {otlp_token}"},
        preferred_temporality={
            Counter: AggregationTemporality.DELTA,
            Histogram: AggregationTemporality.DELTA,
            UpDownCounter: AggregationTemporality.CUMULATIVE,
        },
    )
    provider = MeterProvider(
        metric_readers=[PeriodicExportingMetricReader(exporter, export_interval_millis=30_000)],
        views=[
            View(instrument_name="gen_ai.client.operation.duration",
                 aggregation=ExplicitBucketHistogramAggregation(boundaries=DURATION_BUCKETS)),
            View(instrument_name="gen_ai.client.token.usage",
                 aggregation=ExplicitBucketHistogramAggregation(boundaries=TOKEN_BUCKETS)),
            View(instrument_name="gen_ai.server.time_to_first_token",
                 aggregation=ExplicitBucketHistogramAggregation(boundaries=TTFT_BUCKETS)),
            View(instrument_name="gen_ai.server.time_per_output_token",
                 aggregation=ExplicitBucketHistogramAggregation(boundaries=TPOT_BUCKETS)),
        ],
    )
    metrics.set_meter_provider(provider)
    return metrics.get_meter(app_name, "1.0.0")


def _init_logs(otlp_base: str, otlp_token: str) -> None:
    """Configure OTLP log export to Dynatrace with trace-id correlation."""
    exporter = OTLPLogExporter(
        endpoint=f"{otlp_base}/v1/logs",
        headers={"Authorization": f"Api-Token {otlp_token}"},
    )
    provider = LoggerProvider()
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)
    handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def init_observability(app_name: str) -> OTelComponents:
    """
    One-call bootstrap: metrics → logs → tracing (order matters).

    Reads config from env vars:
      - TRACELOOP_BASE_URL: OTLP base URL (e.g. https://<env>.live.dynatrace.com/api/v2/otlp)
      - DT_OTLP_TOKEN: Dynatrace API token
    """
    otlp_base = os.getenv("TRACELOOP_BASE_URL", "")
    otlp_token = os.getenv("DT_OTLP_TOKEN", "")

    if otlp_base:
        meter = _init_metrics(app_name, otlp_base, otlp_token)
        _init_logs(otlp_base, otlp_token)
    else:
        meter = metrics.get_meter(app_name, "1.0.0")

    # Traceloop MUST init after MeterProvider to avoid conflicts
    Traceloop.init(app_name=app_name, disable_batch=False)

    tracer = trace.get_tracer(app_name, "1.0.0")
    logger = logging.getLogger(app_name)

    return OTelComponents(meter=meter, tracer=tracer, logger=logger)
