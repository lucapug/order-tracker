# Incident Report: Order lookup 5xx server errors

**Alert:** Order lookup 5xx server errors — `GET /api/orders/{order_id}` (5m window, started 2026-09-29T14:31:50Z)
**Status:** Resolved

## What happened

`GET /api/orders/express-1002` returned HTTP 500 twice (metrics: `order_lookup_requests_total{status="500"} = 2` on `/api/orders/{order_id}`). Loki logs for both failing requests (`trace_id=77b1ce02b463018b3b775c5283221862`, `trace_id=cf43d600d632dce99d9a80291b869089`) point at `_record_lookup` in `app/main.py`, and both Tempo traces show a single errored `order-tracker` server span for the lookup.

## Root cause

`order_detail()` in `app/main.py` computed the estimated delivery date for express orders with `placed_at.replace(day=placed_at.day + 2)`. When `day + 2` exceeds the number of days in the month, `datetime.replace()` raises `ValueError: day is out of range for month`, which propagated as an unhandled exception → HTTP 500.

The seed data creates express order `express-1002` with `created_at` at the **end of the previous month**, so every lookup of it overflowed and 500ed. Standard orders were unaffected, matching the evidence (only `express-1002` failed; the 404 series is the unrelated not-found lookup).

## What changed

- `app/main.py` — `order_detail()` now uses `placed_at + timedelta(days=2)` instead of `replace(day=day + 2)`, so date arithmetic rolls over correctly across month boundaries.
- `tests/test_api.py` — added regression tests: express order lookup returns 200 with `estimated_delivery`, and an express order created on the last day of a month does not crash.

## Verification

`uv run --frozen pytest -q` → 5 passed (previously the new month-end test reproduced the 500).

Manual check: `curl http://localhost:8000/api/orders/express-1002` should return 200 with an `estimated_delivery` field instead of 500. After redeploying, the Grafana alert on `order_lookup_requests_total{status="500"}` should stop firing.
