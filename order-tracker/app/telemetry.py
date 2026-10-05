import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


SERVICE_NAME = "order-tracker"


def setup_telemetry():
    """Configure traces, metrics, and logs.

    Signals go to the OTLP endpoint in OTEL_EXPORTER_OTLP_ENDPOINT (the Collector)
    when it is set, and to stdout otherwise.
    """
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    resource = Resource.create({"service.name": SERVICE_NAME})
    use_otlp = bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))

    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter() if use_otlp else ConsoleSpanExporter()
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    export_interval_ms = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "10000"))
    metric_exporter = OTLPMetricExporter() if use_otlp else ConsoleMetricExporter()
    reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=export_interval_ms)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    logger_provider = LoggerProvider(resource=resource)
    log_exporter = OTLPLogExporter() if use_otlp else ConsoleLogExporter()
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(logger_provider)

    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.INFO)
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))
