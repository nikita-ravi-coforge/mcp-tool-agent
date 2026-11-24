"""OpenTelemetry setup. The MCP SDK already creates a span for every request it sends or serves and
carries the trace ID from client to server; this module only decides where those spans go.

    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   send to a collector / Jaeger / Phoenix
    EQUIPMENT_TRACE=console                             one line per span on stderr
    (neither)                                           tracing off
"""

import os
import sys

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter


def one_line(span: ReadableSpan) -> str:
    millis = (span.end_time - span.start_time) / 1e6
    return f"[trace {span.context.trace_id:032x}] {span.name}  {millis:.1f}ms\n"


def default_exporter() -> SpanExporter | None:
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter()
    if os.environ.get("EQUIPMENT_TRACE") == "console":
        # stderr, because an MCP stdio server's stdout is the protocol channel.
        return ConsoleSpanExporter(out=sys.stderr, formatter=one_line)
    return None


def setup_tracing(service_name: str, processor: SpanProcessor | None = None) -> None:
    """Install a tracer provider for this process. Call once at startup; a no-op when tracing is off."""
    if processor is None:
        exporter = default_exporter()
        if exporter is None:
            return
        processor = BatchSpanProcessor(exporter)
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
