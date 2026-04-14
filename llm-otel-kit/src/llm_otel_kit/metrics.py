"""GenAI semantic-convention + operational metric instruments."""

from dataclasses import dataclass, field

from opentelemetry.metrics import Counter, Histogram, Meter, UpDownCounter


@dataclass
class GenAIMetrics:
    """Pre-created OTel metric instruments for LLM observability.

    Usage::

        from llm_otel_kit import GenAIMetrics, init_observability

        otel = init_observability("my-app")
        m = GenAIMetrics(otel.meter)
        m.request_count.add(1, {"model": "gpt-4o"})
    """

    _meter: Meter = field(repr=False)

    # GenAI semconv
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

    def __post_init__(self) -> None:
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
