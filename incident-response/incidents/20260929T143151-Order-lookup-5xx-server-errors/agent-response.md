This is a real incident, not a test: metrics show two 500s on `/api/orders/{order_id}`, logs show `order lookup express-1002 -> 500`, and both traces have errors. Let me investigate the code.
Root cause found. In `order_detail` (app/main.py:76), express orders compute the estimated delivery with `placed_at.replace(day=placed_at.day + 2)`, which raises `ValueError: day is out of range for month` when day+2 overflows the month. The seed data places `express-1002` on the last day of the previous month, so every lookup of it 500s — exactly matching the evidence. Let me check the tests, then fix.
The bug: `datetime.replace(day=day+2)` throws `ValueError` when day+2 exceeds the month's length. The seed data puts express order `express-1002` on the last day of the previous month, so every lookup raises and FastAPI returns 500. No test covers express lookups, which is why it slipped through. Fixing both:
Now running the test suite:
All 5 tests pass. Writing the incident report:
**Incident report** (saved at `incident-response/incidents/20260929T143151-Order-lookup-5xx-server-errors/report.md`):

- **What happened:** Two 500s on `GET /api/orders/express-1002` (metrics, logs, and traces all confirm the same failing order).
- **Root cause:** `order_detail()` in app/main.py:76 used `placed_at.replace(day=placed_at.day + 2)` for express orders; when day+2 overflowed the month (`express-1002` is seeded on the last day of the previous month), it raised `ValueError` → 500.
- **Fix:** app/main.py now uses `placed_at + timedelta(days=2)`, which rolls over correctly. Added two regression tests in tests/test_api.py for express lookups and month-end dates.
- **Verification:** `uv run --frozen pytest -q` → 5 passed. Confirm `GET /api/orders/express-1002` returns 200 with `estimated_delivery` after redeploy.

RESOLVED: Fixed month-overflow in express delivery estimate (app/main.py) by replacing day+2 with timedelta(days=2); added regression tests.
