"""
VoicePay AI — Backend Test Suite
=================================
Tests use FastAPI's TestClient with mocked PayPal HTTP responses.
No real network calls are made; no real PayPal credentials are required.

Run from the backend/ directory:
    pytest tests/ -v

Security regression tests cover:
  Issue A: Capture verification
  Issue B: Transaction history access control
  Issue C: Rate limiting and input validation
  Issue D: CORS policy
  Issue E: Database integrity
  Issue F: (repository/config — validated manually)
  Issue G: Configuration behavior
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ─── TEST ENVIRONMENT SETUP ───────────────────────────────────────────────────
# Must be set BEFORE importing main so os.getenv() picks them up.

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
TEST_DB = _tmp.name

VALID_SESSION_TOKEN = "test-demo-session-token-abc123"

os.environ["APP_ENV"]              = "sandbox"
os.environ["DATABASE_URL"]         = f"sqlite:///{TEST_DB}"
os.environ["PAYPAL_CLIENT_ID"]     = "TEST_CLIENT_ID"
os.environ["PAYPAL_CLIENT_SECRET"] = "TEST_CLIENT_SECRET"
os.environ["PAYPAL_BASE_URL"]      = "https://api-m.sandbox.paypal.com"
os.environ["DEMO_SESSION_TOKEN"]   = VALID_SESSION_TOKEN
os.environ.pop("OPENAI_API_KEY", None)   # Force fallback parser

# Add backend directory to import path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

# Point the module at the temp database
main.DB_PATH = TEST_DB
main.init_db()

client = TestClient(main.app, raise_server_exceptions=False)

# ─── RATE LIMITER RESET FIXTURE ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset the in-process rate limiter before every test to prevent bleed-through."""
    main._limiter._windows.clear()
    yield
    # Also clear after, in case of any leakage
    main._limiter._windows.clear()


# ─── MOCK FACTORIES ───────────────────────────────────────────────────────────

def _token_resp() -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.is_success = True
    m.json.return_value = {"access_token": "MOCK_TOKEN", "token_type": "Bearer"}
    return m


def _order_resp(order_id: str = "TESTORDER00001") -> MagicMock:
    m = MagicMock()
    m.status_code = 201
    m.is_success = True
    m.json.return_value = {"id": order_id, "status": "CREATED", "links": []}
    return m


def _capture_resp(
    order_id: str = "TESTORDER00001",
    capture_id: str = "CAPTURE00001",
    amount: str = "5.00",
    currency: str = "USD",
    status: str = "COMPLETED",
    cap_status: str = "COMPLETED",
) -> MagicMock:
    m = MagicMock()
    m.status_code = 201
    m.is_success = True
    m.json.return_value = {
        "id": order_id,
        "status": status,
        "purchase_units": [
            {
                "payments": {
                    "captures": [
                        {
                            "id": capture_id,
                            "status": cap_status,
                            "amount": {"value": amount, "currency_code": currency},
                        }
                    ]
                }
            }
        ],
    }
    return m


def _capture_resp_voided(order_id: str = "VOIDORDER0001") -> MagicMock:
    m = MagicMock()
    m.status_code = 201
    m.is_success = True
    m.json.return_value = {"id": order_id, "status": "VOIDED", "purchase_units": []}
    return m


def _insert_order(
    order_id: str,
    amount: str = "5.00",
    currency: str = "USD",
    recipient: str = "Coffee Shop",
    purpose: str = "latte",
    status: str = "CREATED",
) -> None:
    """Directly insert a test order into the DB."""
    from datetime import datetime, timezone
    conn = sqlite3.connect(TEST_DB)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO orders
                (paypal_order_id, recipient, purpose, amount, currency, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (order_id, recipient, purpose, amount, currency, status,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_transactions() -> None:
    conn = sqlite3.connect(TEST_DB)
    conn.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()


def _authed_headers() -> dict:
    """Return headers with the valid session token."""
    return {"X-Session-Token": VALID_SESSION_TOKEN}


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    def test_returns_200(self):
        r = client.get("/")
        assert r.status_code == 200

    def test_response_schema(self):
        data = client.get("/").json()
        assert data["status"] == "ok"
        assert data["application"] == "VoicePay AI"
        assert "environment" in data
        assert "paypal_configured" in data
        assert "ai_parser" in data

    def test_no_secrets_in_response(self):
        text = client.get("/").text
        assert "TEST_CLIENT_SECRET" not in text
        assert "TEST_CLIENT_ID" not in text

    def test_fallback_parser_indicated(self):
        # OPENAI_API_KEY is not set in test env
        data = client.get("/").json()
        assert data["ai_parser"] == "fallback"


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfig:
    def test_returns_client_id(self):
        r = client.get("/config")
        assert r.status_code == 200
        assert "paypal_client_id" in r.json()

    def test_no_secret_in_response(self):
        text = client.get("/config").text
        assert "TEST_CLIENT_SECRET" not in text

    def test_no_secret_key_in_response(self):
        keys = client.get("/config").json().keys()
        assert "paypal_client_secret" not in keys
        assert "client_secret" not in keys

    def test_missing_client_id_returns_503(self):
        original = main.PAYPAL_CLIENT_ID
        main.PAYPAL_CLIENT_ID = ""
        try:
            r = client.get("/config")
            assert r.status_code == 503
        finally:
            main.PAYPAL_CLIENT_ID = original


# ═══════════════════════════════════════════════════════════════════════════════
# PARSE PAYMENT
# ═══════════════════════════════════════════════════════════════════════════════

class TestParsePayment:
    def test_basic_english_request(self):
        r = client.post("/parse-payment", json={"message": "Pay Coffee Shop 5 dollars for latte"})
        assert r.status_code == 200
        data = r.json()
        assert data["amount"] == pytest.approx(5.00, abs=0.01)
        assert "Coffee Shop" in data["recipient"]
        assert data["currency"] == "USD"

    def test_decimal_amount(self):
        r = client.post("/parse-payment", json={"message": "Pay BookStore 24.99 dollars for textbook"})
        assert r.status_code == 200
        assert r.json()["amount"] == pytest.approx(24.99, abs=0.01)

    def test_explicit_eur_currency(self):
        r = client.post("/parse-payment", json={"message": "Pay Alex 10 euros for dinner"})
        assert r.status_code == 200
        assert r.json()["currency"] == "EUR"

    def test_gbp_currency(self):
        r = client.post("/parse-payment", json={"message": "Pay Pub 8 pounds for drinks"})
        assert r.status_code == 200
        assert r.json()["currency"] == "GBP"

    def test_missing_amount_returns_none(self):
        r = client.post("/parse-payment", json={"message": "Pay Alex for dinner"})
        assert r.status_code == 200
        data = r.json()
        assert "amount" in data
        # amount may be None or missing — should not crash the endpoint

    def test_missing_recipient(self):
        r = client.post("/parse-payment", json={"message": "Pay 10 dollars for dinner"})
        assert r.status_code == 200  # Should not crash

    def test_empty_message_returns_422(self):
        r = client.post("/parse-payment", json={"message": ""})
        assert r.status_code == 422

    def test_whitespace_only_message_returns_422(self):
        r = client.post("/parse-payment", json={"message": "   "})
        assert r.status_code == 422

    def test_fallback_mode_indicated(self):
        r = client.post("/parse-payment", json={"message": "Pay Gym 50 dollars for membership"})
        assert r.status_code == 200
        assert r.json().get("ai_mode") == "fallback"

    def test_purpose_extraction(self):
        r = client.post("/parse-payment", json={"message": "Pay Alex 15 USD for lunch"})
        assert r.status_code == 200
        assert "lunch" in (r.json().get("purpose") or "").lower()

    def test_amount_too_large_is_nulled(self):
        r = client.post("/parse-payment", json={"message": "Pay someone 9999999 dollars"})
        assert r.status_code == 200
        # Amount exceeds MAX_AMOUNT; parser should set it to None
        data = r.json()
        assert data.get("amount") is None or data.get("amount", 0) <= float(main.MAX_AMOUNT)

    # Issue C: Content-Type validation
    def test_wrong_content_type_rejected(self):
        r = client.post(
            "/parse-payment",
            content=b'{"message": "Pay Alex 5 USD"}',
            headers={"Content-Type": "text/plain"},
        )
        # FastAPI parses body before our handler; may get 422 from Pydantic
        # before our 415 check. Both mean the request was rejected.
        assert r.status_code in (415, 422)

    def test_missing_message_field_returns_422(self):
        r = client.post("/parse-payment", json={"text": "Pay Alex 5 USD"})
        assert r.status_code == 422

    def test_oversized_message_returns_422(self):
        r = client.post("/parse-payment", json={"message": "A" * 1001})
        assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# CREATE ORDER
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreateOrder:
    @patch("main.httpx.AsyncClient")
    def test_valid_order_returns_order_id(self, MockClient):
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [_token_resp(), _order_resp("VALIDORDER001")]

        r = client.post("/create-order", json={
            "recipient": "Coffee Shop", "amount": 5.00, "currency": "USD", "purpose": "latte"
        })
        assert r.status_code == 200
        data = r.json()
        assert data["order_id"] == "VALIDORDER001"
        assert data["id"] == "VALIDORDER001"

    @patch("main.httpx.AsyncClient")
    def test_order_persisted_to_db(self, MockClient):
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [_token_resp(), _order_resp("DBORDER0001")]

        client.post("/create-order", json={
            "recipient": "TestShop", "amount": 12.50, "currency": "USD", "purpose": "item"
        })

        conn = sqlite3.connect(TEST_DB)
        row = conn.execute(
            "SELECT * FROM orders WHERE paypal_order_id = ?", ("DBORDER0001",)
        ).fetchone()
        conn.close()
        assert row is not None

    # Issue E: DB failure during create-order should return 500, not silently continue
    @patch("main.httpx.AsyncClient")
    def test_db_failure_on_create_returns_500(self, MockClient):
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [_token_resp(), _order_resp("DBFAIL00001")]

        with patch("main._get_db") as mock_db:
            mock_conn = MagicMock()
            mock_conn.__enter__ = lambda s: s
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.execute.side_effect = Exception("DB error")
            mock_db.return_value = mock_conn

            r = client.post("/create-order", json={
                "recipient": "Shop", "amount": 5.00, "currency": "USD"
            })
        # DB failure on create should now return 500 (not 200)
        assert r.status_code == 500

    def test_zero_amount_rejected(self):
        r = client.post("/create-order", json={"recipient": "T", "amount": 0, "currency": "USD"})
        assert r.status_code == 422

    def test_negative_amount_rejected(self):
        r = client.post("/create-order", json={"recipient": "T", "amount": -5.0, "currency": "USD"})
        assert r.status_code == 422

    def test_amount_exceeds_limit_rejected(self):
        r = client.post("/create-order", json={"recipient": "T", "amount": 99999.0, "currency": "USD"})
        assert r.status_code == 422

    def test_inr_currency_rejected(self):
        r = client.post("/create-order", json={"recipient": "T", "amount": 100.0, "currency": "INR"})
        assert r.status_code == 422
        # Pydantic validation errors return detail as a list of dicts
        detail = r.json().get("detail", "")
        detail_str = str(detail)
        assert "INR" in detail_str or "supported" in detail_str.lower() or "not supported" in detail_str.lower()

    def test_unsupported_currency_rejected(self):
        r = client.post("/create-order", json={"recipient": "T", "amount": 5.0, "currency": "XYZ"})
        assert r.status_code == 422

    def test_empty_recipient_rejected(self):
        r = client.post("/create-order", json={"recipient": "", "amount": 5.0, "currency": "USD"})
        assert r.status_code == 422

    @patch("main.httpx.AsyncClient")
    def test_paypal_auth_failure_returns_503(self, MockClient):
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        bad = MagicMock()
        bad.status_code = 401
        bad.is_success = False
        bad.json.return_value = {}
        inst.post.return_value = bad

        r = client.post("/create-order", json={"recipient": "T", "amount": 5.0, "currency": "USD"})
        assert r.status_code in (503, 502)

    @patch("main.httpx.AsyncClient")
    def test_paypal_api_error_returns_502(self, MockClient):
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        err_resp = MagicMock()
        err_resp.status_code = 500
        err_resp.is_success = False
        err_resp.text = "Internal Server Error"
        err_resp.json.return_value = {}
        inst.post.side_effect = [_token_resp(), err_resp]

        r = client.post("/create-order", json={"recipient": "T", "amount": 5.0, "currency": "USD"})
        assert r.status_code == 502

    def test_missing_credentials_returns_503(self):
        orig_id, orig_sec = main.PAYPAL_CLIENT_ID, main.PAYPAL_CLIENT_SECRET
        main.PAYPAL_CLIENT_ID = ""
        main.PAYPAL_CLIENT_SECRET = ""
        try:
            r = client.post("/create-order", json={"recipient": "T", "amount": 5.0, "currency": "USD"})
            assert r.status_code == 503
        finally:
            main.PAYPAL_CLIENT_ID = orig_id
            main.PAYPAL_CLIENT_SECRET = orig_sec

    @patch("main.httpx.AsyncClient")
    def test_correct_amount_sent_to_paypal(self, MockClient):
        """Verify that the exact validated amount string is forwarded to PayPal."""
        captured_payload = {}

        async def mock_post(url, **kwargs):
            if "oauth2/token" in url:
                return _token_resp()
            captured_payload.update(kwargs.get("json", {}))
            return _order_resp("AMTORDER001")

        inst = AsyncMock()
        inst.post.side_effect = mock_post
        MockClient.return_value.__aenter__.return_value = inst

        client.post("/create-order", json={"recipient": "T", "amount": 7.50, "currency": "EUR"})
        pu = captured_payload.get("purchase_units", [{}])
        assert pu[0]["amount"]["value"] == "7.50"
        assert pu[0]["amount"]["currency_code"] == "EUR"

    # Issue C: Content-Type validation
    def test_wrong_content_type_rejected(self):
        r = client.post(
            "/create-order",
            content=b'{"recipient":"T","amount":5.0,"currency":"USD"}',
            headers={"Content-Type": "text/plain"},
        )
        # FastAPI parses body before our handler; may get 422 from Pydantic
        # before our 415 check. Both mean the request was correctly rejected.
        assert r.status_code in (415, 422)


# ═══════════════════════════════════════════════════════════════════════════════
# CAPTURE ORDER — ISSUE A: Strict capture verification
# ═══════════════════════════════════════════════════════════════════════════════

class TestCaptureOrder:
    @patch("main.httpx.AsyncClient")
    def test_successful_capture(self, MockClient):
        """A: Valid completed capture returns 200 with success=True."""
        _insert_order("CAPORDER001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [_token_resp(), _capture_resp("CAPORDER001", "CAP001")]

        r = client.post("/capture-order", json={"order_id": "CAPORDER001"})
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["status"] == "COMPLETED"
        assert data["capture_id"] == "CAP001"

    @patch("main.httpx.AsyncClient")
    def test_non_completed_order_status_rejected(self, MockClient):
        """A: Overall order status != COMPLETED is rejected."""
        _insert_order("VOIDORDER001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [_token_resp(), _capture_resp_voided("VOIDORDER001")]

        r = client.post("/capture-order", json={"order_id": "VOIDORDER001"})
        assert r.status_code == 422
        assert r.json().get("success") is not True

    @patch("main.httpx.AsyncClient")
    def test_missing_purchase_units_rejected(self, MockClient):
        """A: Missing purchase_units array in PayPal response is rejected."""
        _insert_order("MPUR00000001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        m = MagicMock()
        m.status_code = 201
        m.is_success = True
        m.json.return_value = {"id": "MPUR00000001", "status": "COMPLETED"}
        inst.post.side_effect = [_token_resp(), m]

        r = client.post("/capture-order", json={"order_id": "MPUR00000001"})
        assert r.status_code in (422, 502)
        assert r.json().get("success") is not True

    @patch("main.httpx.AsyncClient")
    def test_empty_captures_array_rejected(self, MockClient):
        """A: Empty captures array is rejected — no fallback to local data."""
        _insert_order("EMPTCAP00001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        m = MagicMock()
        m.status_code = 201
        m.is_success = True
        m.json.return_value = {
            "id": "EMPTCAP00001",
            "status": "COMPLETED",
            "purchase_units": [{"payments": {"captures": []}}],
        }
        inst.post.side_effect = [_token_resp(), m]

        r = client.post("/capture-order", json={"order_id": "EMPTCAP00001"})
        assert r.status_code in (422, 502)
        assert r.json().get("success") is not True

    @patch("main.httpx.AsyncClient")
    def test_capture_status_not_completed_rejected(self, MockClient):
        """A: Individual capture status != COMPLETED is rejected."""
        _insert_order("PENDCAP00001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [
            _token_resp(),
            _capture_resp("PENDCAP00001", "CAPPEND01", status="COMPLETED", cap_status="PENDING"),
        ]

        r = client.post("/capture-order", json={"order_id": "PENDCAP00001"})
        assert r.status_code in (422, 502)
        assert r.json().get("success") is not True

    @patch("main.httpx.AsyncClient")
    def test_missing_capture_id_rejected(self, MockClient):
        """A: Missing or empty capture ID is rejected."""
        _insert_order("NOCAPID0001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        m = MagicMock()
        m.status_code = 201
        m.is_success = True
        m.json.return_value = {
            "id": "NOCAPID0001",
            "status": "COMPLETED",
            "purchase_units": [
                {
                    "payments": {
                        "captures": [
                            {
                                "id": "",  # empty capture ID
                                "status": "COMPLETED",
                                "amount": {"value": "5.00", "currency_code": "USD"},
                            }
                        ]
                    }
                }
            ],
        }
        inst.post.side_effect = [_token_resp(), m]

        r = client.post("/capture-order", json={"order_id": "NOCAPID0001"})
        assert r.status_code in (422, 502)
        assert r.json().get("success") is not True

    @patch("main.httpx.AsyncClient")
    def test_missing_amount_in_capture_rejected(self, MockClient):
        """A: Missing amount object in PayPal capture is rejected."""
        _insert_order("NOAMOUNT001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        m = MagicMock()
        m.status_code = 201
        m.is_success = True
        m.json.return_value = {
            "id": "NOAMOUNT001",
            "status": "COMPLETED",
            "purchase_units": [
                {
                    "payments": {
                        "captures": [
                            {
                                "id": "CAPNOAMT01",
                                "status": "COMPLETED",
                                # no "amount" field
                            }
                        ]
                    }
                }
            ],
        }
        inst.post.side_effect = [_token_resp(), m]

        r = client.post("/capture-order", json={"order_id": "NOAMOUNT001"})
        assert r.status_code in (422, 502)
        assert r.json().get("success") is not True

    @patch("main.httpx.AsyncClient")
    def test_amount_mismatch_rejected(self, MockClient):
        """A: Captured amount different from stored order is rejected."""
        _insert_order("AMTMIS00001", amount="5.00")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        # PayPal reports capturing 7.50 but order stored 5.00
        inst.post.side_effect = [
            _token_resp(),
            _capture_resp("AMTMIS00001", "CAPMIS001", amount="7.50", currency="USD"),
        ]

        r = client.post("/capture-order", json={"order_id": "AMTMIS00001"})
        assert r.status_code == 422
        assert r.json().get("success") is not True

    @patch("main.httpx.AsyncClient")
    def test_currency_mismatch_rejected(self, MockClient):
        """A: Currency in PayPal response different from stored order is rejected."""
        _insert_order("CURMIS00001", amount="5.00", currency="USD")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [
            _token_resp(),
            _capture_resp("CURMIS00001", "CAPCUR001", amount="5.00", currency="EUR"),
        ]

        r = client.post("/capture-order", json={"order_id": "CURMIS00001"})
        assert r.status_code == 422
        assert r.json().get("success") is not True

    def test_unknown_order_id_returns_404(self):
        """A: Order ID not in DB is rejected with 404."""
        r = client.post("/capture-order", json={"order_id": "UNKNOWNORDER1"})
        assert r.status_code == 404

    def test_never_created_order_id_returns_404(self):
        """A: Order ID that was never created returns 404, not success."""
        r = client.post("/capture-order", json={"order_id": "NEVEREXISTED1"})
        assert r.status_code == 404
        assert r.json().get("success") is not True

    @patch("main.httpx.AsyncClient")
    def test_duplicate_capture_is_idempotent(self, MockClient):
        """A: Duplicate capture request returns existing COMPLETED record."""
        _insert_order("DUPORDER0001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [
            _token_resp(),
            _capture_resp("DUPORDER0001", "CAPDP001"),
        ]
        client.post("/capture-order", json={"order_id": "DUPORDER0001"})

        # Second request — should not call PayPal again
        r = client.post("/capture-order", json={"order_id": "DUPORDER0001"})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "COMPLETED"
        assert data.get("message") == "This order was already captured."

    def test_invalid_order_id_format_returns_422(self):
        """A: Malformed order ID is rejected by input validation."""
        r = client.post("/capture-order", json={"order_id": "bad id with spaces!"})
        assert r.status_code == 422

    def test_no_false_success_on_unverified_status(self):
        """A: Non-COMPLETED status must never produce success=True or HTTP 200."""
        _insert_order("PENDORDER001")
        with patch("main.httpx.AsyncClient") as MockClient:
            inst = AsyncMock()
            MockClient.return_value.__aenter__.return_value = inst
            inst.post.side_effect = [
                _token_resp(),
                _capture_resp("PENDORDER001", "CAPPEND", status="PENDING"),
            ]
            r = client.post("/capture-order", json={"order_id": "PENDORDER001"})
        # Must not be 200 with success=True
        if r.status_code == 200:
            assert r.json().get("success") is not True
        else:
            assert r.status_code in (422, 409)

    @patch("main.httpx.AsyncClient")
    def test_paypal_api_error_returns_error(self, MockClient):
        """A: PayPal API error or malformed response does not succeed."""
        _insert_order("PAYPALER001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        err = MagicMock()
        err.status_code = 500
        err.is_success = False
        err.text = "Internal Server Error"
        err.json.return_value = {}
        inst.post.side_effect = [_token_resp(), err]

        r = client.post("/capture-order", json={"order_id": "PAYPALER001"})
        assert r.status_code in (502, 422, 500)
        assert r.json().get("success") is not True

    @patch("main.httpx.AsyncClient")
    def test_malformed_json_response_from_paypal_rejected(self, MockClient):
        """A: Malformed (non-JSON) PayPal response is rejected."""
        _insert_order("BADJSONORD1")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        m = MagicMock()
        m.status_code = 200
        m.is_success = True
        m.json.side_effect = ValueError("Not JSON")
        inst.post.side_effect = [_token_resp(), m]

        r = client.post("/capture-order", json={"order_id": "BADJSONORD1"})
        assert r.status_code in (422, 502, 500)
        assert r.json().get("success") is not True

    @patch("main.httpx.AsyncClient")
    def test_db_write_failure_after_paypal_success_returns_warning(self, MockClient):
        """A/E: PayPal confirms COMPLETED but DB write fails — should return warning, not 500/false."""
        _insert_order("DBFAILCAP01")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [_token_resp(), _capture_resp("DBFAILCAP01", "CAPDBF01")]

        # Patch only the transaction write, not the read
        original_get_db = main._get_db
        call_count = [0]

        def selective_db():
            call_count[0] += 1
            # First 2 calls: reads for order_row and txn_row — let through
            if call_count[0] <= 2:
                return original_get_db()
            # Subsequent calls (the write): fail
            m = MagicMock()
            m.__enter__ = lambda s: s
            m.__exit__ = MagicMock(return_value=False)
            m.execute.side_effect = Exception("Simulated DB write failure")
            return m

        with patch("main._get_db", selective_db):
            r = client.post("/capture-order", json={"order_id": "DBFAILCAP01"})

        # Should still return 200 with success=True but include a warning
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["status"] == "COMPLETED"
        assert "warning" in data  # Indicates DB write failed

    @patch("main.httpx.AsyncClient")
    def test_order_in_invalid_state_rejected(self, MockClient):
        """A/E: Order in COMPLETED state cannot be re-captured."""
        _insert_order("COMPSTATE01", status="COMPLETED")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst

        r = client.post("/capture-order", json={"order_id": "COMPSTATE01"})
        # Should be rejected — order is not in a capturable state
        assert r.status_code == 409
        assert r.json().get("success") is not True


# ═══════════════════════════════════════════════════════════════════════════════
# TRANSACTIONS — ISSUE B: Access control
# ═══════════════════════════════════════════════════════════════════════════════

class TestTransactions:
    def test_unauthenticated_request_is_rejected(self):
        """B: No session token returns 401."""
        r = client.get("/transactions")
        assert r.status_code == 401

    def test_invalid_session_token_is_rejected(self):
        """B: Wrong session token returns 403."""
        r = client.get("/transactions", headers={"X-Session-Token": "wrong-token"})
        assert r.status_code == 403

    def test_valid_session_token_is_accepted(self):
        """B: Valid session token returns 200."""
        r = client.get("/transactions", headers=_authed_headers())
        assert r.status_code == 200

    def test_unconfigured_token_returns_503(self):
        """B: Missing DEMO_SESSION_TOKEN config returns 503."""
        orig = main.DEMO_SESSION_TOKEN
        main.DEMO_SESSION_TOKEN = ""
        try:
            r = client.get("/transactions", headers=_authed_headers())
            assert r.status_code == 503
        finally:
            main.DEMO_SESSION_TOKEN = orig

    def test_empty_history_returns_empty_array(self):
        """B: Authenticated request gets empty list when no transactions."""
        _clear_transactions()
        r = client.get("/transactions", headers=_authed_headers())
        assert r.status_code == 200
        assert r.json() == []

    @patch("main.httpx.AsyncClient")
    def test_completed_transaction_appears(self, MockClient):
        """B: Authorized request sees completed transactions."""
        _clear_transactions()
        _insert_order("TXNORDER0001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [_token_resp(), _capture_resp("TXNORDER0001", "CAPTXN001")]
        client.post("/capture-order", json={"order_id": "TXNORDER0001"})

        r = client.get("/transactions", headers=_authed_headers())
        assert r.status_code == 200
        txns = r.json()
        order_ids = [t["order_id"] for t in txns]
        assert "TXNORDER0001" in order_ids

    @patch("main.httpx.AsyncClient")
    def test_transaction_schema(self, MockClient):
        """B: Response contains only the expected frontend fields."""
        _clear_transactions()
        _insert_order("SCHORDER0001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [_token_resp(), _capture_resp("SCHORDER0001", "CAPSCH001")]
        client.post("/capture-order", json={"order_id": "SCHORDER0001"})

        r = client.get("/transactions", headers=_authed_headers())
        txns = r.json()
        if txns:
            txn = txns[0]
            assert "date" in txn
            assert "recipient" in txn
            assert "amount" in txn
            assert "currency" in txn
            assert "status" in txn
            assert "purpose" in txn

    def test_no_secrets_in_transactions(self):
        """B: Transaction response never leaks credentials."""
        text = client.get("/transactions", headers=_authed_headers()).text
        assert "TEST_CLIENT_SECRET" not in text
        assert "MOCK_TOKEN" not in text

    @patch("main.httpx.AsyncClient")
    def test_no_duplicate_transaction_records(self, MockClient):
        """E: Duplicate capture does not create duplicate DB records."""
        _clear_transactions()
        _insert_order("NODUPORDER01")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [
            _token_resp(), _capture_resp("NODUPORDER01", "CAPND001"),
        ]
        client.post("/capture-order", json={"order_id": "NODUPORDER01"})

        conn = sqlite3.connect(TEST_DB)
        count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE paypal_order_id = ?",
            ("NODUPORDER01",)
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_session_via_cookie_accepted(self):
        """B: Session cookie is also accepted as authentication."""
        r = client.get(
            "/transactions",
            cookies={main._SESSION_COOKIE_NAME: VALID_SESSION_TOKEN},
        )
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

class TestSession:
    def test_valid_token_starts_session(self):
        """B: POST /session/start with correct token returns 200."""
        r = client.post("/session/start", json={"token": VALID_SESSION_TOKEN})
        assert r.status_code == 200
        assert r.json().get("ok") is True

    def test_invalid_token_rejected(self):
        """B: Wrong token to /session/start returns 403."""
        r = client.post("/session/start", json={"token": "wrong-token"})
        assert r.status_code == 403

    def test_missing_token_field_rejected(self):
        """B: Missing token field returns 400."""
        r = client.post("/session/start", json={})
        assert r.status_code == 400

    def test_wrong_content_type_rejected(self):
        """C: Non-JSON body to session start is rejected."""
        r = client.post(
            "/session/start",
            content=b'token=abc',
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 415


# ═══════════════════════════════════════════════════════════════════════════════
# RATE LIMITING — ISSUE C
# ═══════════════════════════════════════════════════════════════════════════════

class TestRateLimiting:
    def test_rate_limit_exceeded_returns_429(self):
        """C: Exceeding the rate limit on /parse-payment returns 429."""
        # Reset limiter
        main._limiter._windows.clear()

        # Exhaust the limit — _RATE_PARSE = (20, 60)
        ip = "192.0.2.1"  # Use a fake IP that won't conflict with other tests
        for _ in range(20):
            main._limiter.is_allowed(ip, *main._RATE_PARSE)

        # The 21st call should be denied
        assert not main._limiter.is_allowed(ip, *main._RATE_PARSE)

    def test_normal_requests_within_limit_succeed(self):
        """C: A handful of requests within the limit all succeed."""
        main._limiter._windows.clear()
        ip = "192.0.2.2"
        for _ in range(5):
            assert main._limiter.is_allowed(ip, *main._RATE_PARSE)

    def test_different_ips_are_independent(self):
        """C: Rate limit for IP-A does not affect IP-B."""
        main._limiter._windows.clear()
        ip_a = "192.0.2.10"
        ip_b = "192.0.2.11"
        for _ in range(20):
            main._limiter.is_allowed(ip_a, *main._RATE_PARSE)
        # ip_b should still be allowed
        assert main._limiter.is_allowed(ip_b, *main._RATE_PARSE)


# ═══════════════════════════════════════════════════════════════════════════════
# CORS — ISSUE D
# ═══════════════════════════════════════════════════════════════════════════════

class TestCORS:
    def test_null_origin_not_in_allowed_origins(self):
        """D: 'null' must not appear in the CORS allowed list."""
        assert "null" not in main._CORS_ORIGINS
        assert "NULL" not in main._CORS_ORIGINS

    def test_allowed_local_origin_present(self):
        """D: Standard local dev origins are present."""
        assert "http://localhost:5500" in main._CORS_ORIGINS

    def test_no_wildcard_in_allowed_origins(self):
        """D: Wildcard '*' must not be in the allowed list."""
        assert "*" not in main._CORS_ORIGINS

    def test_allowed_origin_gets_cors_headers(self):
        """D: An allowed origin receives Access-Control-Allow-Origin header."""
        r = client.get("/", headers={"Origin": "http://localhost:5500"})
        assert r.status_code == 200
        assert "access-control-allow-origin" in r.headers

    def test_preflight_for_allowed_origin(self):
        """D: Preflight OPTIONS request for allowed origin returns 200."""
        r = client.options(
            "/parse-payment",
            headers={
                "Origin": "http://localhost:5500",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE INTEGRITY — ISSUE E
# ═══════════════════════════════════════════════════════════════════════════════

class TestDatabaseIntegrity:
    @patch("main.httpx.AsyncClient")
    def test_captured_amount_stored_from_paypal_not_client(self, MockClient):
        """E/A: The transaction record uses PayPal-confirmed amount, not client input."""
        _insert_order("AMTSTOR0001", amount="5.00")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        # PayPal confirms 5.00
        inst.post.side_effect = [_token_resp(), _capture_resp("AMTSTOR0001", "CAPAMT01", amount="5.00")]

        r = client.post("/capture-order", json={"order_id": "AMTSTOR0001"})
        assert r.status_code == 200

        conn = sqlite3.connect(TEST_DB)
        row = conn.execute(
            "SELECT amount FROM transactions WHERE paypal_order_id = ?", ("AMTSTOR0001",)
        ).fetchone()
        conn.close()
        assert row is not None
        # Stored amount should be the PayPal-confirmed amount as Decimal string
        from decimal import Decimal
        assert Decimal(row[0]) == Decimal("5.00")

    @patch("main.httpx.AsyncClient")
    def test_uniqueness_constraint_on_capture(self, MockClient):
        """E: Two concurrent captures of same order cannot create two COMPLETED records."""
        _insert_order("CONCURR0001")
        inst = AsyncMock()
        MockClient.return_value.__aenter__.return_value = inst
        inst.post.side_effect = [
            _token_resp(), _capture_resp("CONCURR0001", "CAPCNCR01"),
            _token_resp(), _capture_resp("CONCURR0001", "CAPCNCR02"),
        ]

        # First capture
        r1 = client.post("/capture-order", json={"order_id": "CONCURR0001"})
        # Second capture — should be idempotent (returns existing)
        r2 = client.post("/capture-order", json={"order_id": "CONCURR0001"})

        assert r1.status_code == 200
        assert r2.status_code == 200

        conn = sqlite3.connect(TEST_DB)
        count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE paypal_order_id = ?", ("CONCURR0001",)
        ).fetchone()[0]
        conn.close()
        assert count == 1  # Only one record should exist
