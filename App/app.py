import json
import logging
import os
import secrets
import sys
import threading
import time

from flask import Flask, Response, g, jsonify, render_template, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from opentelemetry import trace

APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "sre-task-manager")

# Endpoints that must not be counted in the HTTP metrics (probes, scrapes).
UNMEASURED_PATHS = {"/healthz", "/readyz", "/metrics"}

TITLE_MAX_LENGTH = 100

# --------------------------------------------------------------------------
# Prometheus metrics (module level: one registry per process)
# --------------------------------------------------------------------------
# Traffic + Errors: split by status so 5xx ratio can be computed.
REQUEST_COUNT = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "endpoint", "status"]
)
# Latency: histogram so p50/p95/p99 can be computed with histogram_quantile().
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=(
        0.005,
        0.010,
        0.015,
        0.020,
        0.025,
        0.030,
        0.035,
        0.040,
        0.045,
        0.050,
        0.075,
        0.100,
        0.250,
        0.500,
        1.0,
        2.5,
        5.0,
        10.0,
    ),
)
ENDPOINT_DELAY = Histogram(
    "endpoint_delay_seconds",
    "Random processing delay per endpoint in seconds",
    ["endpoint"],
    buckets=(0.010, 0.015, 0.020, 0.025, 0.030, 0.035, 0.040, 0.045, 0.050),
)
# Saturation: how many requests are being handled right now.
REQUESTS_IN_PROGRESS = Gauge("http_requests_in_progress", "HTTP requests currently being served")
# Business metrics.
TASKS_CURRENT = Gauge("tasks_current", "Tasks currently stored, by status", ["status"])
TASK_OPERATIONS = Counter("task_operations_total", "Task operations", ["operation"])
VALIDATION_ERRORS = Counter("task_validation_errors_total", "Rejected task requests", ["reason"])
# Metadata + chaos state (so dashboards can show when faults are active).
APP_INFO = Gauge("app_info", "Application build information", ["version"])
CHAOS_ERROR_RATE = Gauge("chaos_error_rate", "Injected fraction of failing requests (0-1)")
CHAOS_LATENCY = Gauge("chaos_latency_seconds", "Injected extra latency per request")

tracer = trace.get_tracer(__name__)


# --------------------------------------------------------------------------
# Structured logging
# --------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    """Render every log record as a single JSON line (easy to ship to Loki/ELK)."""

    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


logger = logging.getLogger("task-manager")


# --------------------------------------------------------------------------
# Tracing (optional)
# --------------------------------------------------------------------------
def setup_tracing(app):
    """Enable OpenTelemetry only when an OTLP endpoint is configured."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    # Jaeger's OTLP receiver inside the cluster is plain gRPC (no TLS).
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
    FlaskInstrumentor().instrument_app(app, excluded_urls="healthz,readyz,metrics")
    logger.info("tracing enabled", extra={"fields": {"otlp_endpoint": endpoint}})


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------
def create_app():
    configure_logging()
    app = Flask(__name__)

    max_tasks = int(os.environ.get("MAX_TASKS", "200"))
    chaos_enabled = os.environ.get("ENABLE_CHAOS", "false").lower() == "true"

    store = {"tasks": {}, "next_id": 1}
    lock = threading.Lock()
    chaos = {"error_rate": 0.0, "latency_ms": 0}

    APP_INFO.labels(version=APP_VERSION).set(1)
    CHAOS_ERROR_RATE.set(0)
    CHAOS_LATENCY.set(0)

    def refresh_task_gauges():
        """Must be called while holding `lock`."""
        done = sum(1 for t in store["tasks"].values() if t["done"])
        TASKS_CURRENT.labels(status="done").set(done)
        TASKS_CURRENT.labels(status="open").set(len(store["tasks"]) - done)

    refresh_task_gauges()
    setup_tracing(app)

    # ---------------- request lifecycle: metrics, logs, chaos ----------------
    @app.before_request
    def start_timer():
        g.start = time.perf_counter()
        g.measured = request.path not in UNMEASURED_PATHS
        if not g.measured:
            return None
        REQUESTS_IN_PROGRESS.inc()
        if request.path.startswith("/api/tasks"):
            if chaos["latency_ms"]:
                time.sleep(chaos["latency_ms"] / 1000)
            if chaos["error_rate"] and secrets.randbelow(1000) < chaos["error_rate"] * 1000:
                return jsonify(error="injected failure"), 500
        return None

    @app.after_request
    def record_request(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        if not getattr(g, "measured", False):
            return response
        duration = time.perf_counter() - g.start
        # url_rule keeps cardinality low: "/api/tasks/<int:task_id>", not "/api/tasks/17".
        endpoint = request.url_rule.rule if request.url_rule else "unmatched"
        REQUEST_COUNT.labels(request.method, endpoint, response.status_code).inc()
        REQUEST_LATENCY.labels(request.method, endpoint).observe(duration)
        REQUESTS_IN_PROGRESS.dec()
        span_ctx = trace.get_current_span().get_span_context()
        logger.info(
            "request",
            extra={
                "fields": {
                    "method": request.method,
                    "endpoint": endpoint,
                    "status": response.status_code,
                    "duration_ms": round(duration * 1000, 2),
                    "trace_id": format(span_ctx.trace_id, "032x") if span_ctx.is_valid else None,
                }
            },
        )
        return response

    @app.errorhandler(Exception)
    def handle_unexpected(exc):
        # Let Flask render HTTP errors (404, 405, ...) normally.
        if hasattr(exc, "code") and exc.code and exc.code < 500:
            return jsonify(error=exc.name), exc.code
        # Details go to the logs only, never to the client.
        logger.error("unhandled exception", exc_info=exc)
        return jsonify(error="internal server error"), 500

    # ---------------- helpers ----------------
    def reject(reason, message, status=400):
        VALIDATION_ERRORS.labels(reason=reason).inc()
        logger.warning("validation failed", extra={"fields": {"reason": reason}})
        return jsonify(error=message), status

    def parse_title(data):
        title = data.get("title") if isinstance(data, dict) else None
        if not isinstance(title, str) or not title.strip():
            return None
        title = title.strip()
        return title if len(title) <= TITLE_MAX_LENGTH else None

    # ---------------- UI + operational endpoints ----------------
    @app.get("/")
    def index():
        return render_template("index.html", version=APP_VERSION)

    @app.get("/healthz")
    def healthz():
        """Liveness: the process is up and able to answer."""
        return jsonify(status="ok")

    @app.get("/readyz")
    def readyz():
        """Readiness: the task store is usable."""
        with lock:
            return jsonify(status="ready", tasks=len(store["tasks"]))

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

    # ---------------- task API ----------------
    def simulate_random_delay(endpoint):
        delay = secrets.SystemRandom().uniform(0.010, 0.050)
        ENDPOINT_DELAY.labels(endpoint=endpoint).observe(delay)
        time.sleep(delay)

    @app.get("/api/tasks")
    def list_tasks():
        with tracer.start_as_current_span("tasks.list"):
            simulate_random_delay("GET /api/tasks")
            with lock:
                tasks = sorted(store["tasks"].values(), key=lambda t: t["id"])
            return jsonify(tasks=tasks)

    @app.post("/api/tasks")
    def create_task():
        data = request.get_json(silent=True)
        if data is None:
            return reject("invalid_json", "body must be valid JSON")
        title = parse_title(data)
        if title is None:
            return reject("invalid_title", f"title must be 1-{TITLE_MAX_LENGTH} characters")
        with tracer.start_as_current_span("tasks.create"):
            simulate_random_delay("POST /api/tasks")
            with lock:
                if len(store["tasks"]) >= max_tasks:
                    return reject("limit_reached", "task limit reached", 409)
                task = {"id": store["next_id"], "title": title, "done": False}
                store["tasks"][task["id"]] = task
                store["next_id"] += 1
                refresh_task_gauges()
        TASK_OPERATIONS.labels(operation="created").inc()
        return jsonify(task), 201

    @app.put("/api/tasks/<int:task_id>")
    def update_task(task_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return reject("invalid_json", "body must be a JSON object")
        with tracer.start_as_current_span("tasks.update"):
            simulate_random_delay("PUT /api/tasks/<id>")
            with lock:
                task = store["tasks"].get(task_id)
                if task is None:
                    return jsonify(error="task not found"), 404
                if "title" in data:
                    title = parse_title(data)
                    if title is None:
                        return reject(
                            "invalid_title", f"title must be 1-{TITLE_MAX_LENGTH} characters"
                        )
                    task["title"] = title
                if "done" in data:
                    if not isinstance(data["done"], bool):
                        return reject("invalid_done", "done must be true or false")
                    if data["done"] != task["done"]:
                        operation = "completed" if data["done"] else "reopened"
                        TASK_OPERATIONS.labels(operation=operation).inc()
                    task["done"] = data["done"]
                refresh_task_gauges()
                return jsonify(task)

    @app.delete("/api/tasks/<int:task_id>")
    def delete_task(task_id):
        with tracer.start_as_current_span("tasks.delete"):
            simulate_random_delay("DELETE /api/tasks/<id>")
            with lock:
                if store["tasks"].pop(task_id, None) is None:
                    return jsonify(error="task not found"), 404
                refresh_task_gauges()
        TASK_OPERATIONS.labels(operation="deleted").inc()
        return "", 204

    # ---------------- fault injection (demo only) ----------------
    if chaos_enabled:

        @app.route("/api/chaos", methods=["GET", "POST"])
        def chaos_control():
            if request.method == "POST":
                data = request.get_json(silent=True) or {}
                rate, latency = data.get("error_rate", 0), data.get("latency_ms", 0)
                numeric = (int, float)
                valid = (
                    isinstance(rate, numeric)
                    and not isinstance(rate, bool)
                    and isinstance(latency, numeric)
                    and not isinstance(latency, bool)
                    and 0 <= rate <= 1
                    and 0 <= latency <= 5000
                )
                if not valid:
                    return jsonify(error="error_rate must be 0-1, latency_ms 0-5000"), 400
                chaos["error_rate"], chaos["latency_ms"] = float(rate), float(latency)
                CHAOS_ERROR_RATE.set(chaos["error_rate"])
                CHAOS_LATENCY.set(chaos["latency_ms"] / 1000)
                logger.warning("chaos updated", extra={"fields": dict(chaos)})
            return jsonify(chaos)

    logger.info("app started", extra={"fields": {"version": APP_VERSION, "chaos": chaos_enabled}})
    return app


app = create_app()

if __name__ == "__main__":
    # Local development; the container runs `flask run` (see Dockerfile).
    app.run(host="127.0.0.1", port=5000)
