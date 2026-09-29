"""OpenTelemetry setup for Order Tracker.

Exports the app's traces, metrics, and logs. When OTEL_EXPORTER_OTLP_ENDPOINT
is set (Docker Compose points it at the OpenTelemetry Collector) the signals
are sent there via OTLP; without it they are printed to the console, which is
what the homework inspects with `docker compose logs app`.
"""

import atexit
import logging
import os
import sys

from opentelemetry import metrics, trace
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

LOOKUP_LOGGER_NAME = "order_tracker"

_configured = False


class _StdoutWriter:
    """Console exporter output that resolves sys.stdout at write time.

    The exporters capture sys.stdout when they are created (during a test
    run that is pytest's capture stream, which is closed before our atexit
    flush), so hand them an object that looks up the stream on each write.
    """

    def write(self, text):
        try:
            sys.stdout.write(text)
        except Exception:
            pass

    def flush(self):
        try:
            sys.stdout.flush()
        except Exception:
            pass


def setup_telemetry() -> None:
    """Wire up trace, metric, and log exporters (once per process)."""
    global _configured
    if _configured:
        return
    _configured = True

    resource = Resource.create({"service.name": "order-tracker"})

    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        span_processor = BatchSpanProcessor(OTLPSpanExporter())
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(), export_interval_millis=5000
        )
        log_processor = BatchLogRecordProcessor(OTLPLogExporter())
    else:
        span_processor = SimpleSpanProcessor(ConsoleSpanExporter())
        metric_reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(out=_StdoutWriter()), export_interval_millis=5000
        )
        log_processor = SimpleLogRecordProcessor(ConsoleLogExporter())

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(span_processor)
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        shutdown_on_exit=False,
        metric_readers=[metric_reader],
    )
    metrics.set_meter_provider(meter_provider)
    atexit.register(_shutdown_quietly, meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(log_processor)
    lookup_logger = logging.getLogger(LOOKUP_LOGGER_NAME)
    lookup_logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    lookup_logger.setLevel(logging.INFO)
    lookup_logger.propagate = False


def _shutdown_quietly(provider) -> None:
    try:
        provider.shutdown()
    except Exception:
        pass
