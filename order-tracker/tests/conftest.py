import os

# Keep test output clean: console exporters would print after pytest closes stdout.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
