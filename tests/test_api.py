from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


def test_express_order_lookup_estimates_delivery(client):
    response = client.get("/api/orders/express-1002")
    assert response.status_code == 200
    assert "estimated_delivery" in response.json()


def test_express_order_month_end_does_not_crash(client):
    with main.connect() as db:
        now = datetime.now(timezone.utc)
        last_day = (now.replace(day=1) + timedelta(days=31)).replace(day=1) - timedelta(days=1)
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            ("express-month-end", "Kim", "Keyboard", "express", "received",
             last_day.isoformat()),
        )
    response = client.get("/api/orders/express-month-end")
    assert response.status_code == 200
