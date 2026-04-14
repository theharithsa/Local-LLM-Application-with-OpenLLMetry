"""
Dynatrace-compatible OpenTelemetry bootstrap for GenAI applications.

Handles the critical init order: MeterProvider → Logs → Traceloop.
"""

import logging
import os
from typing import NamedTuple

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
from traceloop.sdk import Traceloop

# ---------------------------------------------------------------------------
# OTel GenAI semantic-convention histogram bucket boundaries
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
    """Tuple returned by init_observability()."""
    meter: metrics.Meter
    tracer: trace.Tracer
    logger: logging.Logger


def _init_metrics(app_name: str, otlp_endpoint: str, otlp_token: str) -> metrics.Meter:
    headers: dict[str, str] = {}
    if otlp_token:
        headers["Authorization"] = f"Api-Token {otlp_token}"

    exporter = OTLPMetricExporter(
        endpoint=f"{otlp_endpoint}/v1/metrics",
        headers=headers,
        preferred_temporality={
            Counter: AggregationTemporality.DELTA,
            Histogram: AggregationTemporality.DELTA,
            UpDownCounter: AggregationTemporality.CUMULATIVE,
        },
    )
    provider = MeterProvider(
        metric_readers=[
            PeriodicExportingMetricReader(exporter, export_interval_millis=30_000),
        ],
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


def _init_logs(otlp_endpoint: str, otlp_token: str) -> None:
    headers: dict[str, str] = {}
    if otlp_token:
        headers["Authorization"] = f"Api-Token {otlp_token}"

    exporter = OTLPLogExporter(
        endpoint=f"{otlp_endpoint}/v1/logs",
        headers=headers,
    )
    provider = LoggerProvider()
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)
    handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def init_observability(
    app_name: str,
    otlp_endpoint: str = "",
    otlp_token: str = "",
) -> OTelComponents:
    """
    One-call OTel bootstrap: metrics → logs → tracing (order matters).

    If *otlp_endpoint* / *otlp_token* are empty, falls back to env vars
    ``TRACELOOP_BASE_URL`` and ``DT_OTLP_TOKEN``.
    """
    otlp_endpoint = otlp_endpoint or os.getenv("TRACELOOP_BASE_URL", "")
    otlp_token = otlp_token or os.getenv("DT_OTLP_TOKEN", "")

    if otlp_endpoint:
        meter = _init_metrics(app_name, otlp_endpoint, otlp_token)
        _init_logs(otlp_endpoint, otlp_token)
    else:
        meter = metrics.get_meter(app_name, "1.0.0")

    # Traceloop MUST init after MeterProvider to avoid conflicts
    Traceloop.init(app_name=app_name, disable_batch=False)

    tracer = trace.get_tracer(app_name, "1.0.0")
    logger = logging.getLogger(app_name)

    return OTelComponents(meter=meter, tracer=tracer, logger=logger)
