"""OpenTelemetry setup: one call wires traces and metrics to the Collector over OTLP/HTTP.

The application only knows one endpoint (OTEL_EXPORTER_OTLP_ENDPOINT = the Collector). Backends, keys,
redaction and sampling are the Collector's job (stack/otel-collector/config.yaml).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.processor.baggage import BaggageSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


@dataclass
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider

    def shutdown(self) -> None:
        """Flush everything before the process exits; short-lived CLIs lose spans without this."""
        self.tracer_provider.force_flush()
        self.meter_provider.force_flush()
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def setup_telemetry(*, service_name: str | None = None, environment: str = "development",
                    extra_processors: list[SpanProcessor] | None = None) -> Telemetry:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:34318").rstrip("/")
    resource = Resource.create({
        "service.name": service_name or os.environ.get("OTEL_SERVICE_NAME", "ec2-inventory-agent"),
        "service.version": os.environ.get("SERVICE_VERSION", "0.1.0"),
        "deployment.environment.name": environment,   # Langfuse maps this to its "environment" filter
    })

    tp = TracerProvider(resource=resource)
    # Copy W3C baggage entries onto every span started under them. This is how trace-wide attributes
    # (session id, user id, experiment id) reach child spans created by libraries we don't control.
    tp.add_span_processor(BaggageSpanProcessor(lambda key: key.startswith(("langfuse.", "session.", "user."))))
    for p in extra_processors or []:
        tp.add_span_processor(p)
    # Batch + background export: the agent never waits on the network to record a span.
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"),
                                             schedule_delay_millis=1000))
    trace.set_tracer_provider(tp)

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
                                           export_interval_millis=10_000)
    mp = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(mp)
    return Telemetry(tp, mp)
