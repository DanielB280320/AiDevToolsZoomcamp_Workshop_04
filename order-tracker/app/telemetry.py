import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


SERVICE_NAME = "order-tracker"


def setup_telemetry():
    """Configure traces, metrics, and logs to be printed to stdout."""
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    resource = Resource.create({"service.name": SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    export_interval_ms = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "10000"))
    reader = PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=export_interval_ms)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(ConsoleLogExporter()))
    set_logger_provider(logger_provider)

    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.INFO)
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))
