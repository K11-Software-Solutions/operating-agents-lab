"""OpenTelemetry for agents: traces, metrics and cost, using the GenAI semantic conventions.

    invoke_agent triage               one span per request (gen_ai.operation.name = invoke_agent)
      ├─ chat claude-opus-5-5          one span per model call: model, tokens, finish reason, cost
      ├─ execute_tool search_backlog   one span per tool call
      └─ chat claude-opus-5-5

Metrics:
    gen_ai.client.token.usage         tokens, by gen_ai.token.type (input/output), model, route, variant
    gen_ai.client.operation.duration  seconds per model call
    kestrel.agent.cost_usd            estimated USD, by route, variant and tenant
    kestrel.agent.requests            requests, by outcome (ok, fallback, degraded, budget_exceeded, error)
    kestrel.agent.request.duration    seconds per request, with a 10 s bucket for the latency SLO
    kestrel.agent.expensive_requests  requests that cost more than $0.05 (the cost SLO)

Exported to Prometheus, dots become underscores, counters gain _total and seconds become
_seconds: kestrel_agent_requests_total{kestrel_outcome="..."}. slo.prometheus_rules() uses those names.

`setup()` returns in-memory exporters for tests. In production, swap in OTLP exporters
(opentelemetry-exporter-otlp) pointing at a collector; the instrumentation doesn't change.
"""
from __future__ import annotations

from dataclasses import dataclass

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SERVICE = "kestrel-agents"


@dataclass
class Telemetry:
    tracer: object
    meter: object
    spans: InMemorySpanExporter | None
    metrics: InMemoryMetricReader | None
    tokens: object
    duration: object
    cost: object
    requests: object
    request_duration: object
    expensive: object

    def finished_spans(self):
        return list(self.spans.get_finished_spans()) if self.spans else []

    def metric_points(self) -> dict[str, list]:
        """{metric name: [(attributes, value)]} from the in-memory reader."""
        out: dict[str, list] = {}
        data = self.metrics.get_metrics_data() if self.metrics else None
        for rm in (data.resource_metrics if data else []):
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    for p in m.data.data_points:
                        value = getattr(p, "value", None)
                        if value is None:
                            value = {"count": p.count, "sum": p.sum}
                        out.setdefault(m.name, []).append((dict(p.attributes), value))
        return out


def setup(span_exporter: SpanExporter | None = None, metric_reader: MetricReader | None = None, version: str = "dev") -> Telemetry:
    resource = Resource.create({"service.name": SERVICE, "service.version": version})
    spans = span_exporter or InMemorySpanExporter()
    reader = metric_reader or InMemoryMetricReader()
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(SimpleSpanProcessor(spans))
    mp = MeterProvider(resource=resource, metric_readers=[reader])
    meter = mp.get_meter("kestrel.agents")
    return Telemetry(
        tracer=tp.get_tracer("kestrel.agents"),
        meter=meter,
        spans=spans if isinstance(spans, InMemorySpanExporter) else None,
        metrics=reader if isinstance(reader, InMemoryMetricReader) else None,
        tokens=meter.create_counter("gen_ai.client.token.usage", unit="{token}", description="Tokens used"),
        duration=meter.create_histogram("gen_ai.client.operation.duration", unit="s", description="Model call duration"),
        cost=meter.create_counter("kestrel.agent.cost_usd", description="Estimated cost in USD"),
        requests=meter.create_counter("kestrel.agent.requests", unit="{request}", description="Agent requests by outcome"),
        request_duration=meter.create_histogram("kestrel.agent.request.duration", unit="s", description="Request duration",
                                                explicit_bucket_boundaries_advisory=[0.5, 1, 2.5, 5, 10, 20, 30, 60]),
        expensive=meter.create_counter("kestrel.agent.expensive_requests", unit="{request}", description="Requests over $0.05"),
    )
