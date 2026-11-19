"""Unit tests for the task manager (run with: pytest --cov=. from the app/ folder)."""

import pytest

import app as app_module


@pytest.fixture
def client(monkeypatch):
    # Fresh app (and empty task store) for every test; chaos endpoint enabled.
    monkeypatch.setenv("ENABLE_CHAOS", "true")
    monkeypatch.setenv("MAX_TASKS", "3")
    return app_module.create_app().test_client()


def metric_value(client, line_prefix):
    """Return the numeric value of the first /metrics line starting with line_prefix."""
    body = client.get("/metrics").get_data(as_text=True)
    for line in body.splitlines():
        if line.startswith(line_prefix):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_health_and_readiness(client):
    assert client.get("/healthz").get_json() == {"status": "ok"}
    assert client.get("/readyz").get_json()["status"] == "ready"


def test_index_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"SRE Task Manager" in response.data


def test_security_headers(client):
    headers = client.get("/").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"


def test_task_crud_flow(client):
    created = client.post("/api/tasks", json={"title": "  write docs  "})
    assert created.status_code == 201
    task = created.get_json()
    assert task["title"] == "write docs" and task["done"] is False

    updated = client.put(f"/api/tasks/{task['id']}", json={"done": True})
    assert updated.get_json()["done"] is True

    assert len(client.get("/api/tasks").get_json()["tasks"]) == 1
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 204
    assert client.get("/api/tasks").get_json()["tasks"] == []


@pytest.mark.parametrize(
    "payload", [{}, {"title": ""}, {"title": "   "}, {"title": 5}, {"title": "x" * 101}]
)
def test_create_rejects_invalid_title(client, payload):
    assert client.post("/api/tasks", json=payload).status_code == 400


def test_create_rejects_non_json(client):
    assert client.post("/api/tasks", data="nope").status_code == 400


def test_update_validation_and_not_found(client):
    task_id = client.post("/api/tasks", json={"title": "a"}).get_json()["id"]
    assert client.put(f"/api/tasks/{task_id}", json={"done": "yes"}).status_code == 400
    assert client.put(f"/api/tasks/{task_id}", json={"title": ""}).status_code == 400
    assert client.put(f"/api/tasks/{task_id}", json={"title": "b"}).get_json()["title"] == "b"
    assert client.put("/api/tasks/999", json={"done": True}).status_code == 404
    assert client.put(f"/api/tasks/{task_id}", data="x").status_code == 400
    assert client.delete("/api/tasks/999").status_code == 404


def test_task_limit(client):
    for i in range(3):
        assert client.post("/api/tasks", json={"title": f"t{i}"}).status_code == 201
    assert client.post("/api/tasks", json={"title": "over"}).status_code == 409


def test_http_metrics_are_recorded_and_probes_excluded(client):
    client.get("/healthz")
    client.get("/readyz")
    before = metric_value(
        client, 'http_requests_total{endpoint="/api/tasks",method="GET",status="200"}'
    )
    client.get("/api/tasks")
    after = metric_value(
        client, 'http_requests_total{endpoint="/api/tasks",method="GET",status="200"}'
    )
    assert after == before + 1
    body = client.get("/metrics").get_data(as_text=True)
    assert 'endpoint="/healthz"' not in body
    assert 'endpoint="/metrics"' not in body
    assert "http_request_duration_seconds_bucket" in body


def test_business_metrics(client):
    task_id = client.post("/api/tasks", json={"title": "a"}).get_json()["id"]
    client.put(f"/api/tasks/{task_id}", json={"done": True})
    assert metric_value(client, 'tasks_current{status="done"}') == 1
    assert metric_value(client, 'tasks_current{status="open"}') == 0
    client.post("/api/tasks", json={"title": ""})
    assert metric_value(client, 'task_validation_errors_total{reason="invalid_title"}') >= 1
    assert metric_value(client, 'task_operations_total{operation="created"}') >= 1


def test_unknown_route_returns_json_404(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.get_json()["error"]


def test_chaos_error_injection(client):
    assert client.post("/api/chaos", json={"error_rate": 1}).status_code == 200
    assert client.get("/api/tasks").status_code == 500
    # Probes and metrics are never affected by chaos.
    assert client.get("/healthz").status_code == 200
    assert metric_value(client, "chaos_error_rate") == 1.0
    client.post("/api/chaos", json={"error_rate": 0})
    assert client.get("/api/tasks").status_code == 200


def test_chaos_latency_injection(client):
    client.post("/api/chaos", json={"latency_ms": 20})
    assert client.get("/api/chaos").get_json()["latency_ms"] == 20
    assert client.get("/api/tasks").status_code == 200


@pytest.mark.parametrize(
    "payload", [{"error_rate": 2}, {"latency_ms": 99999}, {"error_rate": "x"}, {"error_rate": True}]
)
def test_chaos_rejects_invalid_values(client, payload):
    assert client.post("/api/chaos", json=payload).status_code == 400


def test_chaos_endpoint_absent_when_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_CHAOS", "false")
    assert app_module.create_app().test_client().get("/api/chaos").status_code == 404


def test_unhandled_exception_returns_generic_500(monkeypatch):
    monkeypatch.setenv("ENABLE_CHAOS", "false")
    flask_app = app_module.create_app()
    flask_app.config["PROPAGATE_EXCEPTIONS"] = False

    @flask_app.get("/boom")
    def boom():
        raise RuntimeError("secret detail")

    response = flask_app.test_client().get("/boom")
    assert response.status_code == 500
    assert b"secret detail" not in response.data
