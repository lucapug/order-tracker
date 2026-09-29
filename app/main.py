import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, Field

from app.telemetry import LOOKUP_LOGGER_NAME, setup_telemetry


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}

logger = logging.getLogger(LOOKUP_LOGGER_NAME)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)
lookup_requests = meter.create_counter(
    "order_lookup_requests_total",
    unit="{request}",
    description="Number of order lookup requests",
)

ORDER_LOOKUP_PATH = re.compile(r"^/api/orders/([^/]+)$")
ORDER_LOOKUP_ROUTE = "/api/orders/{order_id}"


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        estimated_at = placed_at + timedelta(days=2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_telemetry()
    init_db()
    yield


app = FastAPI(title="Order Tracker", lifespan=lifespan)


def _record_lookup(span, order_id: str, status_code: int, exc: Exception | None = None):
    attributes = {
        "http.route": ORDER_LOOKUP_ROUTE,
        "http.response.status_code": status_code,
    }
    span.set_attribute("http.route", ORDER_LOOKUP_ROUTE)
    span.set_attribute("http.response.status_code", status_code)
    if exc is not None:
        span.record_exception(exc)
    if status_code >= 500:
        span.set_status(Status(StatusCode.ERROR))
    lookup_requests.add(1, attributes)
    trace_id = format(span.get_span_context().trace_id, "032x")
    logger.info(
        "order lookup %s -> %s trace_id=%s",
        order_id,
        status_code,
        trace_id,
        extra={"order.id": order_id, **attributes},
    )


@app.middleware("http")
async def track_order_lookup(request: Request, call_next):
    match = (
        ORDER_LOOKUP_PATH.fullmatch(request.url.path)
        if request.method == "GET"
        else None
    )
    if match is None:
        return await call_next(request)
    order_id = match.group(1)
    with tracer.start_as_current_span(
        "GET /api/orders/{order_id}", kind=SpanKind.SERVER
    ) as span:
        span.set_attribute("http.request.method", "GET")
        span.set_attribute("url.path", request.url.path)
        span.set_attribute("order.id", order_id)
        try:
            response = await call_next(request)
        except Exception as exc:
            _record_lookup(span, order_id, 500, exc)
            raise
        _record_lookup(span, order_id, response.status_code)
        return response


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return [as_dict(row) for row in rows]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Order not found")
    return order_detail(row)


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    return get_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Order not found")
    return get_order(order_id)
