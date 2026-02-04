from __future__ import annotations

import os
from typing import Any, Dict, Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


_INITIALIZED = False


def init_tracer(service_name: str) -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318/v1/traces")
    resolved_service_name = os.environ.get("OTEL_SERVICE_NAME", service_name)
    resource = Resource(attributes={SERVICE_NAME: resolved_service_name})

    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint)
    span_processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(span_processor)

    trace.set_tracer_provider(provider)
    _INITIALIZED = True


def get_tracer(service_name: str):
    init_tracer(service_name)
    resolved_service_name = os.environ.get("OTEL_SERVICE_NAME", service_name)
    return trace.get_tracer(resolved_service_name)


def _format_trace_id(trace_id: int) -> str:
    # 32-hex lowercase, matches common trace UIs (Jaeger).
    return f"{trace_id:032x}"


def _format_span_id(span_id: int) -> str:
    # 16-hex lowercase.
    return f"{span_id:016x}"


def span_to_metadata(span: Any) -> Dict[str, Any]:
    """Best-effort extraction of OpenTelemetry span metadata for JSON.

    This is intentionally permissive and tries to expose:
    - trace_id/span_id for correlating with Jaeger
    - any span attributes currently present (when available)

    It is designed so that new tracing attributes automatically show up in the
    raw JSON without UI code changes.
    """
    meta: Dict[str, Any] = {}
    try:
        ctx = span.get_span_context()
        meta["trace_id"] = _format_trace_id(int(ctx.trace_id))
        meta["span_id"] = _format_span_id(int(ctx.span_id))
        meta["trace_flags"] = int(getattr(ctx, "trace_flags", 0))
        meta["is_remote"] = bool(getattr(ctx, "is_remote", False))
    except Exception:
        # Keep metadata best-effort.
        pass

    # Span attributes are SDK-specific; try common attribute locations.
    attrs: Dict[str, Any] = {}
    for attr_name in ("attributes", "_attributes"):
        try:
            raw = getattr(span, attr_name, None)
            if raw and isinstance(raw, dict):
                attrs.update(raw)
        except Exception:
            continue
    if attrs:
        meta["attributes"] = attrs

    return meta


