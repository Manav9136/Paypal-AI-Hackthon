"""
VoicePay AI — FastAPI Backend
==============================
Endpoints:
  GET  /                  Health check / backend availability
  GET  /config            Public config (PayPal Client ID only — no secret)
  POST /parse-payment     Parse natural-language payment request (AI or fallback)
  POST /create-order      Create a PayPal Sandbox order
  POST /capture-order     Capture an approved PayPal order
  GET  /transactions      List persisted verified transactions

Security notes:
  - PayPal Client Secret never leaves this file or API responses.
  - OpenAI key never leaves this file or API responses.
  - All DB queries use parameterised statements.
  - Amount arithmetic uses Python Decimal, not float.
  - Model output is validated server-side before use.
  - Rate limiting applied per-IP to all endpoints.
  - CORS restricted to explicit allowed origins (no "null").
  - Capture verification requires COMPLETED status, valid capture ID,
    and amount/currency match against DB-stored order — never falls back
    to local data when PayPal capture details are absent.
  - /transactions requires a demo-session token (X-Session-Token header
    or __vp_session cookie) for access control.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# ─── ENV LOADING ──────────────────────────────────────────────────────────────
# Load .env from the same directory as this file (backend/).
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_env_path)

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("voicepay")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
APP_ENV              = os.getenv("APP_ENV", "sandbox")
HOST                 = os.getenv("HOST", "127.0.0.1")
PORT                 = int(os.getenv("PORT", "8000"))
PAYPAL_CLIENT_ID     = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
PAYPAL_BASE_URL      = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").rstrip("/")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL         = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
_DATABASE_URL        = os.getenv("DATABASE_URL", "sqlite:///./voicepay.db")

# ── Security / session ────────────────────────────────────────────────────────
# DEMO_SESSION_TOKEN: a secret token required to access protected endpoints
# (GET /transactions). In local dev, set this to any non-empty value in .env.
# In production, set a long random secret.
# If not set, the endpoint returns 503 with a clear configuration error.
DEMO_SESSION_TOKEN = os.getenv("DEMO_SESSION_TOKEN", "")

# ── CORS ──────────────────────────────────────────────────────────────────────
# Read explicit allowed origins from environment.  Comma-separated list.
# Default: common local dev servers.  Do NOT include "null" here.
_CORS_ORIGINS_RAW = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5500,http://127.0.0.1:5500,"
    "http://localhost:5501,http://127.0.0.1:5501,"
    "http://localhost:3000,http://127.0.0.1:3000,"
    "http://localhost:8080,http://127.0.0.1:8080",
)
_CORS_ORIGINS: list[str] = [
    o.strip() for o in _CORS_ORIGINS_RAW.split(",") if o.strip()
]
# Security: reject "null" — it would allow file:// and sandboxed iframes.
_CORS_ORIGINS = [o for o in _CORS_ORIGINS if o.lower() != "null"]

# ── Production doc visibility ─────────────────────────────────────────────────
# Hide /docs and /openapi.json in production (APP_ENV == "production").
_IS_PRODUCTION = APP_ENV.lower() == "production"
_DOCS_URL   = None if _IS_PRODUCTION else "/docs"
_REDOC_URL  = None if _IS_PRODUCTION else "/redoc"
_OPENAPI_URL = None if _IS_PRODUCTION else "/openapi.json"

# Derive a file-system path from the DATABASE_URL
_db_raw = _DATABASE_URL.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
if _db_raw.startswith("./") or not os.path.isabs(_db_raw):
    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), _db_raw.lstrip("./"))
else:
    DB_PATH = _db_raw

# Financial limits
MIN_AMOUNT = Decimal("0.01")
MAX_AMOUNT = Decimal("10000.00")

# Supported currencies for standard PayPal Checkout Sandbox.
# INR requires special PayPal account configuration and is NOT available
# in a standard sandbox merchant account — reject it clearly rather than
# silently sending it to PayPal and getting an opaque error.
SUPPORTED_CURRENCIES = {"USD", "EUR", "GBP", "CAD", "AUD"}

# ─── IN-PROCESS RATE LIMITER ──────────────────────────────────────────────────
# Lightweight token-bucket per IP.  Works for single-process deployments.
# NOTE: In a multi-worker / multi-instance deployment this limiter is
# per-process only and does NOT coordinate across processes.  For
# production scale, replace _RateLimiter with a Redis-backed store.
# The limits below are generous for a hackathon demo while preventing
# automated abuse.

class _RateLimiter:
    """Simple in-memory sliding-window rate limiter keyed by IP."""

    def __init__(self) -> None:
        # {ip: [timestamp, ...]}
        self._windows: dict[str, list[float]] = {}

    def is_allowed(self, ip: str, max_requests: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        calls = self._windows.get(ip, [])
        # Evict expired entries
        calls = [t for t in calls if t > cutoff]
        if len(calls) >= max_requests:
            self._windows[ip] = calls
            return False
        calls.append(now)
        self._windows[ip] = calls
        return True


_limiter = _RateLimiter()

# Rate-limit configs: (max_requests, window_seconds)
_RATE_PARSE   = (20, 60)   # 20 calls/min  — AI parsing is expensive
_RATE_ORDER   = (20, 60)   # 20 calls/min  — order creation
_RATE_CAPTURE = (30, 60)   # 30 calls/min  — capture (includes retries)
_RATE_TXN     = (60, 60)   # 60 calls/min  — read-only list
_RATE_DEFAULT = (120, 60)  # 120 calls/min — health/config

_SESSION_COOKIE_NAME = "__vp_session"
_SESSION_HEADER_NAME = "X-Session-Token"

# ─── DATABASE ─────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables on first run. Safe to call on every startup."""
    with _get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                paypal_order_id TEXT    NOT NULL UNIQUE,
                recipient       TEXT    NOT NULL,
                purpose         TEXT    DEFAULT '',
                amount          TEXT    NOT NULL,
                currency        TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'CREATED',
                created_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                paypal_order_id TEXT    NOT NULL UNIQUE,
                capture_id      TEXT,
                recipient       TEXT    NOT NULL,
                purpose         TEXT    DEFAULT '',
                amount          TEXT    NOT NULL,
                currency        TEXT    NOT NULL,
                status          TEXT    NOT NULL,
                created_at      TEXT    NOT NULL,
                completed_at    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_orders_ppid
                ON orders(paypal_order_id);
            CREATE INDEX IF NOT EXISTS idx_transactions_ppid
                ON transactions(paypal_order_id);
            """
        )
        conn.commit()
    logger.info("Database ready at %s", DB_PATH)


# ─── PAYPAL HTTP CLIENT ───────────────────────────────────────────────────────

def _require_paypal_credentials() -> None:
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise HTTPException(
            status_code=503,
            detail=(
                "PayPal credentials are not configured. "
                "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in backend/.env "
                "(see backend/.env.example)."
            ),
        )
    # Safety guard: never silently switch to live PayPal in sandbox mode.
    if "live" in PAYPAL_BASE_URL.lower() and APP_ENV.lower() != "live":
        raise HTTPException(
            status_code=500,
            detail="Live PayPal URL detected while APP_ENV is not 'live'. Refusing to proceed.",
        )


async def _get_paypal_token() -> str:
    """Exchange client credentials for a PayPal OAuth 2.0 access token."""
    _require_paypal_credentials()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{PAYPAL_BASE_URL}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                headers={"Accept": "application/json", "Accept-Language": "en_US"},
            )
        if resp.status_code == 401:
            raise HTTPException(
                status_code=503,
                detail="PayPal authentication failed. Verify your Client ID and Secret.",
            )
        if not resp.is_success:
            logger.error("PayPal token error %s: %.200s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=503,
                detail="PayPal authentication failed. Check your credentials.",
            )
        token = resp.json().get("access_token")
        if not token:
            raise HTTPException(status_code=503, detail="PayPal returned no access token.")
        return token
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="PayPal authentication request timed out.")
    except httpx.RequestError as exc:
        logger.error("PayPal network error: %s", exc)
        raise HTTPException(status_code=503, detail="Cannot reach the PayPal API.")


# ─── PAYMENT PARSER ───────────────────────────────────────────────────────────

def _fallback_parse(message: str) -> dict:
    """
    Deterministic rule-based parser used when OPENAI_API_KEY is not configured.
    Extracts recipient, amount, currency, and purpose from plain text.
    Clearly labelled as 'fallback' in the response — never claimed to be AI.
    """
    lower = message.lower()

    # Currency (checked before amount to avoid digit-first matches on codes)
    currency = "USD"
    if re.search(r"\beur(os?)?\b", lower):
        currency = "EUR"
    elif re.search(r"\bgbp\b|\bpound(s)?\b", lower):
        currency = "GBP"
    elif re.search(r"\bcad\b", lower):
        currency = "CAD"
    elif re.search(r"\baud\b", lower):
        currency = "AUD"
    elif re.search(r"\binr\b|\brupee(s)?\b", lower):
        currency = "INR"  # Parseable but create-order will reject it.

    # Amount: "$24.99", "24.99 dollars", "24 usd", or bare number
    amount: Optional[float] = None
    amount_patterns = [
        r"\$\s*([\d]{1,6}(?:\.[\d]{1,2})?)",
        r"([\d]{1,6}(?:\.[\d]{1,2})?)\s*(?:dollars?|usd|eur|euros?|gbp|pounds?|cad|aud|inr|rupees?)",
        r"\b([\d]{1,6}(?:\.[\d]{1,2})?)\b",
    ]
    for pat in amount_patterns:
        m = re.search(pat, lower)
        if m:
            try:
                candidate = float(m.group(1).replace(",", ""))
                if 0 < candidate < 1_000_000:
                    amount = candidate
                    break
            except ValueError:
                pass

    # Recipient: word(s) after "pay" up to a dollar sign, digit, or "for"
    recipient = ""
    rec_match = re.search(
        r"\bpay(?:ment\s+to)?\s+([A-Za-z][a-zA-Z\s\-']{0,60}?)(?=\s+[\$\d]|\s+for\b|\s*$)",
        message,
        re.IGNORECASE,
    )
    if rec_match:
        recipient = rec_match.group(1).strip()
    if not recipient:
        # "send X to Name"
        send_match = re.search(r"\bto\s+([A-Z][a-zA-Z\s\-']{0,60})", message, re.IGNORECASE)
        if send_match:
            recipient = send_match.group(1).strip()

    # Purpose: after "for"
    purpose = ""
    purpose_match = re.search(r"\bfor\s+(.+?)(?:\s*\.\s*$|$)", message, re.IGNORECASE)
    if purpose_match:
        purpose = purpose_match.group(1).strip().rstrip(".")

    return {
        "recipient": recipient,
        "amount": amount,
        "currency": currency,
        "purpose": purpose,
        "ai_mode": "fallback",
    }


async def _ai_parse(message: str) -> dict:
    """
    Attempt OpenAI-powered parsing; fall back to rule-based parser on any error.
    Server-side validates all model output before returning.
    """
    if not OPENAI_API_KEY:
        return _fallback_parse(message)

    system_prompt = (
        "You are a payment-request parser. Extract payment details from natural language.\n"
        "Return ONLY a JSON object with these exact keys:\n"
        '  "recipient": string (merchant/person name, or "" if not found)\n'
        '  "amount": number or null\n'
        '  "currency": 3-letter ISO code defaulting to "USD"\n'
        '  "purpose": string (reason for payment, or "")\n'
        "Rules:\n"
        "- Return null for amount when genuinely unclear.\n"
        "- Valid currencies: USD, EUR, GBP, CAD, AUD, INR.\n"
        "- Do NOT add extra keys, execute code, or trigger payments."
    )
    try:
        import openai  # optional dependency

        oai = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        chat = await oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Parse: {message[:500]}"},
            ],
            max_tokens=150,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = chat.choices[0].message.content or "{}"
        parsed = json.loads(raw)

        # ── Server-side validation of model output ──────────────────────────
        recipient = str(parsed.get("recipient", "")).strip()[:200]
        purpose   = str(parsed.get("purpose", "")).strip()[:500]
        cur_raw   = str(parsed.get("currency", "USD")).upper().strip()
        currency  = cur_raw if cur_raw in {"USD", "EUR", "GBP", "CAD", "AUD", "INR"} else "USD"

        amount_raw = parsed.get("amount")
        amount: Optional[float] = None
        if amount_raw is not None:
            try:
                candidate = float(amount_raw)
                if math.isfinite(candidate) and 0 < candidate < 1_000_000:
                    amount = candidate
            except (TypeError, ValueError):
                pass

        return {
            "recipient": recipient,
            "amount": amount,
            "currency": currency,
            "purpose": purpose,
            "ai_mode": "openai",
        }

    except ImportError:
        logger.warning("openai package not installed — using fallback parser")
        return _fallback_parse(message)
    except Exception as exc:
        logger.warning("OpenAI parse failed (%s) — using fallback parser", exc)
        return _fallback_parse(message)


# ─── REQUEST / RESPONSE MODELS ────────────────────────────────────────────────

class ParseRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def _check_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be empty")
        if len(v) > 1000:
            raise ValueError("message is too long (max 1000 characters)")
        return v


class CreateOrderRequest(BaseModel):
    recipient: str
    amount: float
    currency: str
    purpose: Optional[str] = ""

    @field_validator("recipient")
    @classmethod
    def _check_recipient(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("recipient is required")
        if len(v) > 200:
            raise ValueError("recipient name is too long")
        return v

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, v: str) -> str:
        v = v.upper().strip()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(
                f"Currency '{v}' is not supported. "
                f"Supported currencies: {', '.join(sorted(SUPPORTED_CURRENCIES))}. "
                "Note: INR requires special PayPal account setup unavailable in a "
                "standard Sandbox merchant account."
            )
        return v

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, v: float) -> Decimal:
        if v is None or not math.isfinite(v):
            raise ValueError("amount must be a valid finite number")
        try:
            d = Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except InvalidOperation:
            raise ValueError("amount is not a valid decimal")
        if d < MIN_AMOUNT:
            raise ValueError(f"amount must be at least {MIN_AMOUNT}")
        if d > MAX_AMOUNT:
            raise ValueError(f"amount cannot exceed {MAX_AMOUNT} for sandbox transactions")
        return d

    @field_validator("purpose")
    @classmethod
    def _check_purpose(cls, v: Optional[str]) -> str:
        return (v or "").strip()[:500]


class CaptureOrderRequest(BaseModel):
    order_id: str

    @field_validator("order_id")
    @classmethod
    def _check_order_id(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 50:
            raise ValueError("invalid order_id")
        # PayPal order IDs are uppercase alphanumeric, typically 17 characters
        if not re.match(r"^[A-Z0-9]{8,50}$", v):
            raise ValueError("order_id format is invalid")
        return v


# ─── APP LIFESPAN ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    logger.info("VoicePay AI backend running (env=%s)", APP_ENV)
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        logger.warning(
            "⚠  PayPal credentials not configured — "
            "POST /create-order and /capture-order will return 503"
        )
    if not OPENAI_API_KEY:
        logger.info("ℹ  OPENAI_API_KEY not set — using deterministic fallback parser")
    if not DEMO_SESSION_TOKEN:
        logger.warning(
            "⚠  DEMO_SESSION_TOKEN not set — GET /transactions will return 503. "
            "Add DEMO_SESSION_TOKEN=<random-secret> to backend/.env"
        )
    yield
    logger.info("VoicePay AI backend shutting down")


# ─── FASTAPI APPLICATION ──────────────────────────────────────────────────────

app = FastAPI(
    title="VoicePay AI",
    description="Voice-guided payments — PayPal Sandbox backend",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=_DOCS_URL,
    redoc_url=_REDOC_URL,
    openapi_url=_OPENAPI_URL,
)

# CORS: allow only explicitly configured origins.
# "null" is intentionally NOT included — it would allow file:// and sandboxed iframes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", _SESSION_HEADER_NAME],
)


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    """Extract best-effort client IP from request (may be proxied)."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str, max_req: int, window: int, endpoint: str) -> None:
    """Raise 429 if the IP has exceeded the rate limit for this window."""
    if not _limiter.is_allowed(ip, max_req, window):
        logger.warning("Rate limit exceeded for %s on %s", ip, endpoint)
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Please wait and try again.",
        )


def _check_content_type_json(request: Request) -> None:
    """Reject POST requests that don't declare application/json Content-Type.
    NOTE: This is called explicitly inside each POST handler. FastAPI/Starlette
    parses the request body before our handler runs, so a body with the wrong
    Content-Type may still cause a 422 from Pydantic before we reach this check.
    The middleware-level check below (_content_type_middleware) catches it earlier
    for properly-routed requests.
    """
    ct = request.headers.get("content-type", "")
    if "application/json" not in ct:
        raise HTTPException(
            status_code=415,
            detail="Content-Type must be application/json.",
        )


def _require_session(request: Request) -> None:
    """
    Check that the request carries a valid session token.
    Accepts the token in the X-Session-Token header or the __vp_session cookie.
    Raises 401 if missing or 403 if invalid.
    Raises 503 if DEMO_SESSION_TOKEN is not configured on the server.
    """
    if not DEMO_SESSION_TOKEN:
        raise HTTPException(
            status_code=503,
            detail=(
                "Transaction history is not available: "
                "DEMO_SESSION_TOKEN is not configured on the server. "
                "Add DEMO_SESSION_TOKEN=<random-secret> to backend/.env."
            ),
        )

    token_from_header = request.headers.get(_SESSION_HEADER_NAME, "")
    token_from_cookie = request.cookies.get(_SESSION_COOKIE_NAME, "")
    supplied = token_from_header or token_from_cookie

    if not supplied:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Provide a valid session token.",
        )

    # Use constant-time comparison to prevent timing attacks
    if not secrets.compare_digest(supplied.encode(), DEMO_SESSION_TOKEN.encode()):
        raise HTTPException(
            status_code=403,
            detail="Invalid or expired session token.",
        )


# ─── GLOBAL EXCEPTION HANDLER ─────────────────────────────────────────────────

@app.exception_handler(Exception)
async def _generic_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled error on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "An internal server error occurred."})


# ─── ENDPOINTS ────────────────────────────────────────────────────────────────

@app.get("/")
async def health_check(request: Request):
    """
    Backend health check.
    The frontend's checkBackendAvailability() hits this endpoint.
    Returns HTTP 200 with basic status — no secrets exposed.
    """
    ip = _client_ip(request)
    _check_rate_limit(ip, *_RATE_DEFAULT, "/")
    return {
        "status": "ok",
        "application": "VoicePay AI",
        "environment": APP_ENV,
        "paypal_configured": bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET),
        "ai_parser": "openai" if OPENAI_API_KEY else "fallback",
    }


@app.get("/config")
async def get_public_config(request: Request):
    """
    Return the PayPal public Client ID so the frontend can dynamically
    load the PayPal JS SDK. The Client Secret is NEVER included.
    """
    ip = _client_ip(request)
    _check_rate_limit(ip, *_RATE_DEFAULT, "/config")

    if not PAYPAL_CLIENT_ID:
        raise HTTPException(
            status_code=503,
            detail=(
                "PAYPAL_CLIENT_ID is not configured. "
                "Add it to backend/.env (see backend/.env.example)."
            ),
        )
    return {
        "paypal_client_id": PAYPAL_CLIENT_ID,
        "environment": APP_ENV,
        "ai_parser": "openai" if OPENAI_API_KEY else "fallback",
    }


@app.post("/parse-payment")
async def parse_payment(req: ParseRequest, request: Request):
    """
    Parse a natural-language payment request into structured fields.
    Uses OpenAI when OPENAI_API_KEY is set; deterministic fallback otherwise.
    Does NOT initiate or authorise any payment — parsing only.
    """
    ip = _client_ip(request)
    _check_rate_limit(ip, *_RATE_PARSE, "/parse-payment")
    _check_content_type_json(request)

    result = await _ai_parse(req.message)

    # Independently validate amount regardless of parser source
    raw_amount = result.get("amount")
    if raw_amount is not None:
        try:
            d = Decimal(str(raw_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if d < MIN_AMOUNT or d > MAX_AMOUNT:
                result["amount"] = None
            else:
                result["amount"] = float(d)
        except (InvalidOperation, ValueError):
            result["amount"] = None

    return result


@app.post("/create-order")
async def create_order(req: CreateOrderRequest, request: Request):
    """
    Create a PayPal Checkout order via the PayPal Orders v2 API.
    Persists order metadata locally BEFORE returning — a DB failure here
    aborts the flow so we never proceed with an unrecorded order.

    IMPORTANT: The recipient name is used as metadata/description only.
    This is a standard merchant-checkout flow — funds go to the PayPal
    account associated with the credentials, not to an arbitrary named person.
    """
    ip = _client_ip(request)
    _check_rate_limit(ip, *_RATE_ORDER, "/create-order")
    _check_content_type_json(request)

    amount_str = str(req.amount)

    token = await _get_paypal_token()

    description = f"Payment to {req.recipient}"
    if req.purpose:
        description += f" for {req.purpose}"
    description = description[:127]  # PayPal API max length

    payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "amount": {
                    "currency_code": req.currency,
                    "value": amount_str,
                },
                "description": description,
                "custom_id": req.recipient[:127],
            }
        ],
        "application_context": {
            "brand_name": "VoicePay AI",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{PAYPAL_BASE_URL}/v2/checkout/orders",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Prefer": "return=representation",
                },
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="PayPal order creation timed out.")
    except httpx.RequestError as exc:
        logger.error("PayPal network error (create-order): %s", exc)
        raise HTTPException(status_code=503, detail="Cannot reach the PayPal API.")

    if not resp.is_success:
        logger.error("PayPal create-order %s: %.300s", resp.status_code, resp.text)
        try:
            err = resp.json()
            detail = err.get("message") or err.get("error_description") or "PayPal order creation failed."
        except Exception:
            detail = "PayPal order creation failed."
        raise HTTPException(status_code=502, detail=detail)

    try:
        order_data = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Invalid JSON response from PayPal.")

    paypal_order_id = order_data.get("id")
    if not paypal_order_id:
        raise HTTPException(status_code=502, detail="PayPal did not return an order ID.")

    # Persist order metadata — REQUIRED before returning the order ID.
    # If DB write fails, we abort: the frontend must not proceed with an
    # order we cannot validate during capture.
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _get_db() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO orders
                    (paypal_order_id, recipient, purpose, amount, currency, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'CREATED', ?)
                """,
                (paypal_order_id, req.recipient, req.purpose or "", amount_str, req.currency, now),
            )
            conn.commit()
    except Exception as exc:
        # DB failure after PayPal order creation: log for reconciliation and abort.
        logger.error(
            "RECONCILIATION NEEDED — PayPal order %s created but DB write failed: %s",
            paypal_order_id, exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Order was created with PayPal but could not be recorded. Please contact support.",
        )

    logger.info(
        "Order created: %s  %s %s → %s",
        paypal_order_id, amount_str, req.currency, req.recipient,
    )
    return {
        "order_id": paypal_order_id,
        "id": paypal_order_id,          # also set 'id' for PayPal SDK compatibility
        "status": order_data.get("status"),
    }


@app.post("/capture-order")
async def capture_order(req: CaptureOrderRequest, request: Request):
    """
    Capture an approved PayPal order.

    Security:
    - Verifies the order exists in our own records (prevents arbitrary ID capture).
    - Verifies order is in a capturable state (prevents replaying completed/failed orders).
    - Inspects actual PayPal response status — never trusts browser-supplied status.
    - Requires PayPal status == COMPLETED, a non-empty capture ID, and capture
      status == COMPLETED from the captures array.
    - Validates captured amount and currency against DB-stored order using Decimal.
    - Never substitutes local order data for missing PayPal capture confirmation.
    - Handles duplicate capture requests idempotently.
    - Logs reconciliation info when PayPal succeeds but DB write fails.
    """
    ip = _client_ip(request)
    _check_rate_limit(ip, *_RATE_CAPTURE, "/capture-order")
    _check_content_type_json(request)

    order_id = req.order_id

    # Step 1: Confirm the order was created through this application
    with _get_db() as conn:
        order_row = conn.execute(
            "SELECT * FROM orders WHERE paypal_order_id = ?", (order_id,)
        ).fetchone()

    if not order_row:
        raise HTTPException(
            status_code=404,
            detail="Order not found or was not created through this application.",
        )
    order = dict(order_row)

    # Step 2: Check for an already-completed capture (idempotency)
    with _get_db() as conn:
        txn_row = conn.execute(
            "SELECT * FROM transactions WHERE paypal_order_id = ?", (order_id,)
        ).fetchone()

    if txn_row:
        txn = dict(txn_row)
        if txn["status"] == "COMPLETED":
            logger.info("Duplicate capture request for %s — returning existing record", order_id)
            return {
                "success": True,
                "status": "COMPLETED",
                "order_id": order_id,
                "capture_id": txn["capture_id"],
                "amount": txn["amount"],
                "currency": txn["currency"],
                "message": "This order was already captured.",
            }

    # Step 3: Validate order state transition — only CREATED orders can be captured.
    # This prevents a previously-failed or voided order from being re-attempted here
    # in a way that could produce an inconsistent state.
    order_status = order.get("status", "")
    if order_status not in ("CREATED", "APPROVED"):
        logger.warning(
            "Capture attempt on order %s with status %s — rejecting",
            order_id, order_status,
        )
        raise HTTPException(
            status_code=409,
            detail=f"Order is not in a capturable state (current status: {order_status}).",
        )

    # Step 4: Get a fresh access token and call PayPal
    token = await _get_paypal_token()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{PAYPAL_BASE_URL}/v2/checkout/orders/{order_id}/capture",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={},
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="PayPal capture request timed out.")
    except httpx.RequestError as exc:
        logger.error("PayPal network error (capture-order): %s", exc)
        raise HTTPException(status_code=503, detail="Cannot reach the PayPal API.")

    if not resp.is_success:
        logger.error("PayPal capture %s %s: %.300s", order_id, resp.status_code, resp.text)
        try:
            err = resp.json()
            detail = err.get("message") or err.get("error_description") or "PayPal capture failed."
        except Exception:
            detail = "PayPal capture failed."
        raise HTTPException(
            status_code=422 if resp.status_code in (400, 422) else 502,
            detail=detail,
        )

    try:
        capture_data = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Invalid capture response from PayPal.")

    # ── Step 5: Strict PayPal response validation ─────────────────────────────
    # 5a. Overall order status MUST be COMPLETED
    paypal_status = capture_data.get("status")
    if not isinstance(paypal_status, str) or paypal_status != "COMPLETED":
        logger.warning(
            "Capture %s: PayPal returned non-COMPLETED order status: %s",
            order_id, paypal_status,
        )
        # Persist the non-completed status so we know the attempt was made
        _persist_failed_capture(order_id, order, paypal_status or "UNKNOWN")
        raise HTTPException(
            status_code=422,
            detail=f"Payment capture status: {paypal_status}. The payment was not completed.",
        )

    # 5b. Locate the captures array — must exist and be non-empty
    try:
        purchase_units = capture_data.get("purchase_units")
        if not isinstance(purchase_units, list) or len(purchase_units) == 0:
            raise ValueError("purchase_units missing or empty")
        pu = purchase_units[0]
        if not isinstance(pu, dict):
            raise ValueError("purchase_units[0] is not a dict")
        payments = pu.get("payments")
        if not isinstance(payments, dict):
            raise ValueError("payments field missing")
        captures = payments.get("captures")
        if not isinstance(captures, list) or len(captures) == 0:
            raise ValueError("captures array missing or empty")
        cap = captures[0]
        if not isinstance(cap, dict):
            raise ValueError("captures[0] is not a dict")
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        logger.error(
            "Capture %s: malformed PayPal response structure: %s", order_id, exc
        )
        raise HTTPException(
            status_code=502,
            detail="Unexpected capture response structure from PayPal.",
        )

    # 5c. Individual capture status MUST be COMPLETED
    cap_status = cap.get("status")
    if not isinstance(cap_status, str) or cap_status != "COMPLETED":
        logger.warning(
            "Capture %s: individual capture status is %s, not COMPLETED",
            order_id, cap_status,
        )
        _persist_failed_capture(order_id, order, cap_status or "UNKNOWN")
        raise HTTPException(
            status_code=422,
            detail=f"Individual capture status is '{cap_status}'. Payment not completed.",
        )

    # 5d. Capture ID must be present and non-empty
    capture_id = cap.get("id")
    if not isinstance(capture_id, str) or not capture_id.strip():
        logger.error("Capture %s: PayPal returned empty or missing capture ID", order_id)
        raise HTTPException(
            status_code=502,
            detail="PayPal did not return a valid capture ID.",
        )
    capture_id = capture_id.strip()

    # 5e. Validate captured amount and currency against the STORED order —
    #     do NOT accept client-submitted values and do NOT substitute local
    #     order data for missing PayPal confirmation.
    cap_amount_obj = cap.get("amount")
    if not isinstance(cap_amount_obj, dict):
        logger.error(
            "Capture %s: amount field missing from PayPal capture record", order_id
        )
        raise HTTPException(
            status_code=502,
            detail="PayPal capture response is missing the amount field.",
        )

    cap_value_raw = cap_amount_obj.get("value")
    cap_currency_raw = cap_amount_obj.get("currency_code")

    if not isinstance(cap_value_raw, str) or not cap_value_raw.strip():
        logger.error("Capture %s: captured amount value is missing or not a string", order_id)
        raise HTTPException(
            status_code=502,
            detail="PayPal capture response has an invalid amount value.",
        )
    if not isinstance(cap_currency_raw, str) or not cap_currency_raw.strip():
        logger.error("Capture %s: captured currency_code is missing or not a string", order_id)
        raise HTTPException(
            status_code=502,
            detail="PayPal capture response has an invalid currency code.",
        )

    cap_currency = cap_currency_raw.strip().upper()

    try:
        cap_amount_dec = Decimal(cap_value_raw.strip()).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError):
        logger.error(
            "Capture %s: cannot parse captured amount '%s'", order_id, cap_value_raw
        )
        raise HTTPException(
            status_code=502,
            detail="PayPal capture response contains an unparseable amount.",
        )

    # Compare against the stored order (Decimal arithmetic — no float comparison)
    try:
        stored_amount_dec = Decimal(str(order["amount"])).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError, KeyError):
        logger.error("Capture %s: cannot parse stored order amount", order_id)
        raise HTTPException(status_code=500, detail="Internal order data error.")

    stored_currency = str(order.get("currency", "")).upper()

    if cap_currency != stored_currency:
        logger.error(
            "Capture %s: currency mismatch — captured %s, stored %s",
            order_id, cap_currency, stored_currency,
        )
        raise HTTPException(
            status_code=422,
            detail="Captured currency does not match the original order.",
        )

    if cap_amount_dec != stored_amount_dec:
        logger.error(
            "Capture %s: amount mismatch — captured %s, stored %s",
            order_id, cap_amount_dec, stored_amount_dec,
        )
        raise HTTPException(
            status_code=422,
            detail="Captured amount does not match the original order.",
        )

    # ── Step 6: Persist verified completed transaction ────────────────────────
    # At this point PayPal has confirmed COMPLETED with matching amount and currency.
    # We use the PayPal-confirmed values for the transaction record.
    verified_amount   = str(cap_amount_dec)
    verified_currency = cap_currency
    now = datetime.now(timezone.utc).isoformat()

    paypal_confirmed = True  # PayPal says COMPLETED — track for reconciliation
    db_write_succeeded = False

    try:
        with _get_db() as conn:
            conn.execute(
                "UPDATE orders SET status = 'COMPLETED' WHERE paypal_order_id = ?",
                (order_id,),
            )
            conn.execute(
                """
                INSERT INTO transactions
                    (paypal_order_id, capture_id, recipient, purpose,
                     amount, currency, status, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, 'COMPLETED', ?, ?)
                ON CONFLICT(paypal_order_id) DO UPDATE SET
                    status       = 'COMPLETED',
                    capture_id   = excluded.capture_id,
                    amount       = excluded.amount,
                    currency     = excluded.currency,
                    completed_at = excluded.completed_at
                """,
                (
                    order_id, capture_id,
                    order["recipient"], order.get("purpose", ""),
                    verified_amount, verified_currency,
                    now, now,
                ),
            )
            conn.commit()
        db_write_succeeded = True
    except Exception as exc:
        # CRITICAL: PayPal has confirmed payment but we could not persist it.
        # Log full reconciliation details (no secret values) and surface the
        # discrepancy to the caller so it is not silently lost.
        logger.error(
            "RECONCILIATION NEEDED — PayPal CONFIRMED capture but DB write failed. "
            "order_id=%s capture_id=%s amount=%s %s error=%s",
            order_id, capture_id, verified_amount, verified_currency, exc,
        )

    logger.info(
        "Capture %s: COMPLETED capture_id=%s amount=%s %s db_write=%s",
        order_id, capture_id, verified_amount, verified_currency, db_write_succeeded,
    )

    if not db_write_succeeded:
        # PayPal confirmed the payment but we failed to record it.
        # Return a partial-success response so the frontend knows payment happened
        # but the application could not fully record it.
        return {
            "success": True,
            "status": "COMPLETED",
            "order_id": order_id,
            "capture_id": capture_id,
            "amount": verified_amount,
            "currency": verified_currency,
            "warning": (
                "Payment was confirmed by PayPal but could not be saved to the local "
                "transaction record. Please contact support with your order ID."
            ),
        }

    return {
        "success": True,
        "status": "COMPLETED",
        "order_id": order_id,
        "capture_id": capture_id,
        "amount": verified_amount,
        "currency": verified_currency,
    }


def _persist_failed_capture(order_id: str, order: dict, status: str) -> None:
    """Record a non-COMPLETED capture attempt in the DB (best-effort)."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _get_db() as conn:
            conn.execute(
                "UPDATE orders SET status = ? WHERE paypal_order_id = ?",
                (status, order_id),
            )
            conn.execute(
                """
                INSERT INTO transactions
                    (paypal_order_id, capture_id, recipient, purpose,
                     amount, currency, status, created_at, completed_at)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(paypal_order_id) DO UPDATE SET
                    status = excluded.status
                """,
                (
                    order_id,
                    order["recipient"], order.get("purpose", ""),
                    order["amount"], order["currency"],
                    status, now,
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.error("DB error recording failed capture %s: %s", order_id, exc)


@app.get("/transactions")
async def get_transactions(request: Request):
    """
    Return persisted transaction records, most recent first.
    Requires a valid session token (X-Session-Token header or __vp_session cookie).
    Only includes records written after verified PayPal capture responses.
    Returns only fields needed by the frontend — no internal metadata.
    """
    ip = _client_ip(request)
    _check_rate_limit(ip, *_RATE_TXN, "/transactions")

    # Access control: require a valid session token
    _require_session(request)

    try:
        with _get_db() as conn:
            rows = conn.execute(
                """
                SELECT paypal_order_id, capture_id, recipient, purpose,
                       amount, currency, status, created_at, completed_at
                FROM transactions
                ORDER BY created_at DESC
                LIMIT 100
                """
            ).fetchall()
    except Exception as exc:
        logger.error("DB error fetching transactions: %s", exc)
        return []

    return [
        {
            "date":       row["completed_at"] or row["created_at"],
            "recipient":  row["recipient"] or "",
            "amount":     row["amount"] or "0.00",
            "currency":   row["currency"] or "USD",
            "status":     (row["status"] or "").capitalize(),
            "purpose":    row["purpose"] or "",
            "capture_id": row["capture_id"],
            "order_id":   row["paypal_order_id"],
        }
        for row in rows
    ]


# ─── SESSION MANAGEMENT ENDPOINT ──────────────────────────────────────────────

@app.post("/session/start")
async def start_session(request: Request, response: Response):
    """
    Exchange the DEMO_SESSION_TOKEN for a session cookie.
    Accepts { "token": "<value>" } in the JSON body.
    On success, sets a secure HttpOnly cookie and returns { "ok": true }.

    This endpoint exists so the frontend can authenticate without
    embedding the token in HTML/JS source code.
    """
    ip = _client_ip(request)
    _check_rate_limit(ip, 10, 60, "/session/start")  # strict: 10/min
    _check_content_type_json(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    supplied = str(body.get("token", "")).strip()
    if not supplied:
        raise HTTPException(status_code=400, detail="token field is required.")

    if not DEMO_SESSION_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Session management is not configured on this server.",
        )

    if not secrets.compare_digest(supplied.encode(), DEMO_SESSION_TOKEN.encode()):
        raise HTTPException(status_code=403, detail="Invalid token.")

    # Set a secure HttpOnly SameSite=Strict cookie
    is_https = request.url.scheme == "https"
    response.set_cookie(
        key=_SESSION_COOKIE_NAME,
        value=DEMO_SESSION_TOKEN,
        httponly=True,
        samesite="strict",
        secure=is_https,  # Secure flag only over HTTPS
        max_age=3600 * 8,  # 8-hour session
        path="/",
    )
    return {"ok": True}


# ─── AI SHOPPING AGENT ────────────────────────────────────────────────────────

# ── Demo Product Catalog ──────────────────────────────────────────────────────
# All products in this catalog are for demonstration purposes only.
# Prices, specifications, availability, and merchant data are illustrative
# and do NOT represent actual marketplace listings or verified inventory.
DEMO_CATALOG: list[dict] = [
    # ── Wireless Headphones ─────────────────────────────────────────────────────
    {
        "id": "wh-anker-q45", "name": "Anker Soundcore Q45", "brand": "Anker",
        "category": "headphones", "category_display": "Wireless Headphones",
        "price": 49.99, "currency": "USD", "image_emoji": "🎧",
        "specs": {"Battery Life": "50 hours", "Connectivity": "Bluetooth 5.0", "Type": "Over-ear", "Weight": "250g"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Very budget-friendly", "Long 50-hour battery", "Comfortable for extended wear"],
        "cons": ["Average noise cancellation", "Plastic build quality"],
        "tags": ["wireless", "budget", "bass", "battery", "bluetooth"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "wh-jbl-tune760nc", "name": "JBL Tune 760NC", "brand": "JBL",
        "category": "headphones", "category_display": "Wireless Headphones",
        "price": 79.99, "currency": "USD", "image_emoji": "🎧",
        "specs": {"Battery Life": "35 hours", "ANC": "Active Noise Cancelling", "Type": "Over-ear", "Foldable": "Yes"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Good ANC for the price", "Foldable for portability", "JBL sound signature"],
        "cons": ["Bass-heavy tuning", "Mic quality is average"],
        "tags": ["wireless", "budget", "noise-cancelling", "foldable", "portable"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "wh-sennheiser-hd450bt", "name": "Sennheiser HD 450BT", "brand": "Sennheiser",
        "category": "headphones", "category_display": "Wireless Headphones",
        "price": 99.99, "currency": "USD", "image_emoji": "🎧",
        "specs": {"Battery Life": "30 hours", "ANC": "Active Noise Cancelling", "Codec": "aptX", "Type": "Over-ear"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Balanced audiophile sound", "aptX codec support", "Excellent build quality"],
        "cons": ["Larger form factor", "No app EQ on all platforms"],
        "tags": ["wireless", "audiophile", "mid-range", "noise-cancelling", "aptx"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "wh-bose-qc45", "name": "Bose QuietComfort 45", "brand": "Bose",
        "category": "headphones", "category_display": "Wireless Headphones",
        "price": 249.99, "currency": "USD", "image_emoji": "🎧",
        "specs": {"Battery Life": "24 hours", "ANC": "World-class ANC", "Type": "Over-ear", "Weight": "238g"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Best-in-class ANC", "Exceptional comfort", "Premium sound quality"],
        "cons": ["No aptX or LDAC codec", "Premium price"],
        "tags": ["noise-cancelling", "wireless", "premium", "comfort", "bose"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "wh-sony-xm5", "name": "Sony WH-1000XM5", "brand": "Sony",
        "category": "headphones", "category_display": "Wireless Headphones",
        "price": 299.99, "currency": "USD", "image_emoji": "🎧",
        "specs": {"Battery Life": "30 hours", "ANC": "Industry-leading ANC", "Codec": "LDAC hi-res audio", "Microphones": "8 microphones"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Top-tier ANC performance", "30hr battery with fast charge", "LDAC for hi-res audio"],
        "cons": ["Expensive", "Not foldable (new design)", "Large carrying case"],
        "tags": ["noise-cancelling", "wireless", "premium", "battery", "ldac", "sony"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    # ── Laptops ─────────────────────────────────────────────────────────────────
    {
        "id": "lt-acer-aspire5", "name": "Acer Aspire 5 A515", "brand": "Acer",
        "category": "laptops", "category_display": "Laptops",
        "price": 349.99, "currency": "USD", "image_emoji": "💻",
        "specs": {"CPU": "Intel Core i5-1235U", "RAM": "8GB DDR4", "Storage": "256GB NVMe SSD", "Display": "15.6\" FHD IPS"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Excellent budget value", "Good for everyday programming", "Slim design"],
        "cons": ["8GB RAM limits multitasking", "256GB storage fills up quickly"],
        "tags": ["intel", "student", "programming", "budget", "laptop"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "lt-dell-inspiron15", "name": "Dell Inspiron 15 3000", "brand": "Dell",
        "category": "laptops", "category_display": "Laptops",
        "price": 449.99, "currency": "USD", "image_emoji": "💻",
        "specs": {"CPU": "Intel Core i5-1135G7", "RAM": "8GB DDR4", "Storage": "512GB SSD", "Display": "15.6\" FHD"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Reliable Dell build", "512GB storage", "Good battery life"],
        "cons": ["Heavier than competitors", "Mediocre display brightness"],
        "tags": ["intel", "student", "office", "programming", "dell"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "lt-lenovo-ideapad3", "name": "Lenovo IdeaPad 3", "brand": "Lenovo",
        "category": "laptops", "category_display": "Laptops",
        "price": 499.99, "currency": "USD", "image_emoji": "💻",
        "specs": {"CPU": "AMD Ryzen 5 5500U", "RAM": "8GB DDR4", "Storage": "512GB SSD", "Display": "15.6\" FHD"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Fast Ryzen 5 CPU", "512GB storage", "Good for programming tasks"],
        "cons": ["Average display color accuracy", "Fans can be audible under load"],
        "tags": ["amd", "student", "programming", "budget", "ryzen"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "lt-asus-vivobook15", "name": "ASUS VivoBook 15", "brand": "ASUS",
        "category": "laptops", "category_display": "Laptops",
        "price": 599.99, "currency": "USD", "image_emoji": "💻",
        "specs": {"CPU": "AMD Ryzen 7 5700U", "RAM": "16GB DDR4", "Storage": "512GB NVMe SSD", "Display": "15.6\" FHD OLED"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["16GB RAM for AI/ML work", "Vivid OLED display", "Fast NVMe SSD"],
        "cons": ["Higher price point", "OLED has higher power draw"],
        "tags": ["amd", "programming", "ai", "performance", "oled", "ryzen7"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "lt-hp-pavilion15", "name": "HP Pavilion 15", "brand": "HP",
        "category": "laptops", "category_display": "Laptops",
        "price": 699.99, "currency": "USD", "image_emoji": "💻",
        "specs": {"CPU": "Intel Core i7-1255U", "RAM": "16GB DDR4", "Storage": "512GB SSD", "Display": "15.6\" FHD IPS"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Powerful Core i7 CPU", "16GB RAM handles multitasking", "Premium HP build"],
        "cons": ["Premium price", "Bloatware pre-installed"],
        "tags": ["intel", "programming", "performance", "ai", "i7", "hp"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    # ── Mechanical Keyboards ─────────────────────────────────────────────────────
    {
        "id": "kb-anne-pro2", "name": "Anne Pro 2", "brand": "Obins",
        "category": "keyboards", "category_display": "Mechanical Keyboards",
        "price": 69.99, "currency": "USD", "image_emoji": "⌨️",
        "specs": {"Layout": "60%", "Switches": "Gateron Red (linear)", "Wireless": "Bluetooth 4.0", "USB": "USB-C"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Compact 60% layout ideal for programmers", "Wireless connectivity", "Budget-friendly"],
        "cons": ["No function row — requires Fn combos", "60% can be limiting for some"],
        "tags": ["wireless", "compact", "budget", "programmers", "60percent", "mechanical"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "kb-keychron-k2", "name": "Keychron K2 Pro", "brand": "Keychron",
        "category": "keyboards", "category_display": "Mechanical Keyboards",
        "price": 99.99, "currency": "USD", "image_emoji": "⌨️",
        "specs": {"Layout": "75%", "Switches": "Gateron G Pro Red", "Wireless": "Bluetooth 5.1 / USB-C", "Backlight": "RGB"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["75% layout keeps arrow keys and F-row", "Excellent build quality", "Mac/Windows compatible"],
        "cons": ["No wrist rest included", "Slightly thicker profile"],
        "tags": ["wireless", "compact", "mechanical", "programmers", "75percent", "rgb"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "kb-logitech-mx-keys", "name": "Logitech MX Keys", "brand": "Logitech",
        "category": "keyboards", "category_display": "Mechanical Keyboards",
        "price": 99.99, "currency": "USD", "image_emoji": "⌨️",
        "specs": {"Layout": "Full-size", "Type": "Scissor-switch (not mechanical)", "Wireless": "Bluetooth / Logi Bolt", "Backlight": "Smart backlit"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Excellent typing comfort", "Multi-device pairing", "Smart auto-backlighting"],
        "cons": ["Not a mechanical keyboard — scissor switches", "No per-key RGB"],
        "tags": ["wireless", "slim", "productivity", "office", "multi-device"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "kb-razer-blackwidow-v3", "name": "Razer BlackWidow V3", "brand": "Razer",
        "category": "keyboards", "category_display": "Mechanical Keyboards",
        "price": 139.99, "currency": "USD", "image_emoji": "⌨️",
        "specs": {"Layout": "Full-size", "Switches": "Razer Green (clicky)", "Backlight": "Razer Chroma RGB", "Type": "Wired USB"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Satisfying clicky Razer Green switches", "Premium Chroma RGB", "Solid build quality"],
        "cons": ["Clicky switches not office-friendly", "Wired only"],
        "tags": ["rgb", "gaming", "full-size", "clicky", "razer"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    # ── Monitors ─────────────────────────────────────────────────────────────────
    {
        "id": "mon-benq-gl2780", "name": "BenQ GL2780", "brand": "BenQ",
        "category": "monitors", "category_display": "Monitors",
        "price": 149.99, "currency": "USD", "image_emoji": "🖥️",
        "specs": {"Size": "27\"", "Resolution": "1080p FHD", "Panel": "TN", "Refresh Rate": "75Hz"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 9.99,
        "pros": ["Very affordable 27\" monitor", "Fast response time", "Eye-care certified"],
        "cons": ["TN panel — limited viewing angles", "Only 1080p resolution"],
        "tags": ["fhd", "27inch", "budget", "office", "tn"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "mon-lg-27mp400", "name": "LG 27MP400", "brand": "LG",
        "category": "monitors", "category_display": "Monitors",
        "price": 199.99, "currency": "USD", "image_emoji": "🖥️",
        "specs": {"Size": "27\"", "Resolution": "1080p FHD", "Panel": "IPS", "Refresh Rate": "75Hz"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 9.99,
        "pros": ["IPS panel for better colors", "Wide viewing angles", "Good for programming and office"],
        "cons": ["Only 1080p at 27\"", "Limited connectivity (no USB-C)"],
        "tags": ["ips", "27inch", "office", "programming", "fhd"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "mon-dell-s2721d", "name": "Dell S2721D", "brand": "Dell",
        "category": "monitors", "category_display": "Monitors",
        "price": 279.99, "currency": "USD", "image_emoji": "🖥️",
        "specs": {"Size": "27\"", "Resolution": "1440p QHD", "Panel": "IPS", "Refresh Rate": "75Hz"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 9.99,
        "pros": ["QHD resolution excellent for coding", "IPS color accuracy", "Thin bezels"],
        "cons": ["75Hz — not ideal for gaming", "No USB-C"],
        "tags": ["qhd", "ips", "27inch", "programming", "dell"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "mon-asus-proart-pa278qv", "name": "ASUS ProArt PA278QV", "brand": "ASUS",
        "category": "monitors", "category_display": "Monitors",
        "price": 349.99, "currency": "USD", "image_emoji": "🖥️",
        "specs": {"Size": "27\"", "Resolution": "1440p QHD", "Panel": "IPS", "Color Accuracy": "Delta E ≤ 2"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 9.99,
        "pros": ["Professional color accuracy", "Factory calibrated", "USB hub built-in"],
        "cons": ["Premium price", "No HDR support"],
        "tags": ["qhd", "27inch", "professional", "design", "coloraccuracy"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    # ── Smart Watches ─────────────────────────────────────────────────────────────
    {
        "id": "sw-fitbit-charge6", "name": "Fitbit Charge 6", "brand": "Fitbit",
        "category": "smartwatches", "category_display": "Smart Watches",
        "price": 99.99, "currency": "USD", "image_emoji": "⌚",
        "specs": {"Battery": "7 days", "GPS": "Built-in GPS", "Heart Rate": "Continuous ECG-capable"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Excellent fitness tracking", "7-day battery", "Google Maps & Wallet support"],
        "cons": ["Limited app ecosystem", "Requires Fitbit Premium for full features"],
        "tags": ["fitness", "budget", "health", "gps", "fitbit"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "sw-garmin-fr55", "name": "Garmin Forerunner 55", "brand": "Garmin",
        "category": "smartwatches", "category_display": "Smart Watches",
        "price": 199.99, "currency": "USD", "image_emoji": "⌚",
        "specs": {"Battery": "20 days (watch mode)", "GPS": "Built-in GPS", "Running Dynamics": "Advanced metrics"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["20-day battery life", "Advanced running metrics", "Lightweight"],
        "cons": ["Limited smartwatch features", "Basic display"],
        "tags": ["running", "gps", "fitness", "battery", "garmin"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "sw-samsung-galaxy-watch6", "name": "Samsung Galaxy Watch 6", "brand": "Samsung",
        "category": "smartwatches", "category_display": "Smart Watches",
        "price": 249.99, "currency": "USD", "image_emoji": "⌚",
        "specs": {"Battery": "40 hours", "Health": "BIA body composition sensor", "Compatibility": "Android"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Full smartwatch features", "Body composition tracking", "Sleep coaching"],
        "cons": ["Android only", "Short 40hr battery vs competitors"],
        "tags": ["samsung", "android", "health", "gps", "smartwatch"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "sw-apple-watch-se2", "name": "Apple Watch SE (2nd Gen)", "brand": "Apple",
        "category": "smartwatches", "category_display": "Smart Watches",
        "price": 249.99, "currency": "USD", "image_emoji": "⌚",
        "specs": {"Battery": "18 hours", "Chip": "Apple S8", "Compatibility": "iPhone only", "Crash Detection": "Yes"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Best iPhone integration", "Crash & fall detection", "watchOS ecosystem"],
        "cons": ["iPhone only", "Short 18hr battery", "No always-on display"],
        "tags": ["apple", "ios", "health", "premium", "iphone"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    # ── Tablets ───────────────────────────────────────────────────────────────────
    {
        "id": "tb-amazon-fire-hd10", "name": "Amazon Fire HD 10", "brand": "Amazon",
        "category": "tablets", "category_display": "Tablets",
        "price": 129.99, "currency": "USD", "image_emoji": "📱",
        "specs": {"Display": "10.1\" Full HD 1080p", "Storage": "32GB (expandable)", "OS": "Fire OS (Android-based)"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Very affordable", "Great media consumption", "microSD expansion"],
        "cons": ["Limited to Amazon app store", "Plastic build", "No Google Play"],
        "tags": ["budget", "amazon", "entertainment", "reading", "media"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "tb-samsung-tab-a8", "name": "Samsung Galaxy Tab A8", "brand": "Samsung",
        "category": "tablets", "category_display": "Tablets",
        "price": 199.99, "currency": "USD", "image_emoji": "📱",
        "specs": {"Display": "10.5\" TFT LCD", "Storage": "32GB", "OS": "Android 11", "RAM": "3GB"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Full Android with Google Play", "Large 10.5\" screen", "Samsung DeX support"],
        "cons": ["Only 3GB RAM", "Slow processor for demanding tasks"],
        "tags": ["android", "samsung", "budget", "entertainment", "googleplay"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "tb-lenovo-tab-p11-pro", "name": "Lenovo Tab P11 Pro Gen 2", "brand": "Lenovo",
        "category": "tablets", "category_display": "Tablets",
        "price": 299.99, "currency": "USD", "image_emoji": "📱",
        "specs": {"Display": "11.2\" OLED 2K", "Storage": "128GB", "OS": "Android 12L", "RAM": "6GB"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Beautiful OLED display", "128GB storage", "Premium build"],
        "cons": ["No stylus included", "Software updates limited"],
        "tags": ["android", "oled", "productivity", "premium", "lenovo"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "tb-ipad-9th", "name": "Apple iPad (9th Generation)", "brand": "Apple",
        "category": "tablets", "category_display": "Tablets",
        "price": 329.99, "currency": "USD", "image_emoji": "📱",
        "specs": {"Chip": "Apple A13 Bionic", "Storage": "64GB", "Display": "10.2\" Retina IPS", "Pencil": "1st gen compatible"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["A13 Bionic chip — fast & future-proof", "Excellent iPad ecosystem", "Center Stage camera"],
        "cons": ["Lightning port (not USB-C)", "64GB base storage fills quickly"],
        "tags": ["apple", "ios", "education", "productivity", "ipad"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    # ── Webcams ───────────────────────────────────────────────────────────────────
    {
        "id": "wc-microsoft-lifecam", "name": "Microsoft LifeCam HD-3000", "brand": "Microsoft",
        "category": "webcams", "category_display": "Webcams",
        "price": 29.99, "currency": "USD", "image_emoji": "📷",
        "specs": {"Resolution": "720p HD", "Frame Rate": "30fps", "Microphone": "Built-in noise-cancelling"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Very affordable", "Plug-and-play USB", "No software required"],
        "cons": ["Only 720p resolution", "No autofocus"],
        "tags": ["budget", "720p", "office", "meetings", "plugandplay"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "wc-logitech-c920", "name": "Logitech C920 HD Pro", "brand": "Logitech",
        "category": "webcams", "category_display": "Webcams",
        "price": 69.99, "currency": "USD", "image_emoji": "📷",
        "specs": {"Resolution": "1080p Full HD", "Frame Rate": "30fps", "Autofocus": "Full HD autofocus"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Sharp 1080p image quality", "Reliable autofocus", "Wide browser/OS compatibility"],
        "cons": ["30fps only (no 60fps)", "Fixed clip mount"],
        "tags": ["1080p", "office", "streaming", "meetings", "logitech"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "wc-razer-kiyo", "name": "Razer Kiyo", "brand": "Razer",
        "category": "webcams", "category_display": "Webcams",
        "price": 89.99, "currency": "USD", "image_emoji": "📷",
        "specs": {"Resolution": "1080p Full HD", "Frame Rate": "30fps (60fps at 720p)", "Ring Light": "Built-in adjustable"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Integrated ring light (unique)", "Good for streaming", "No external lighting needed"],
        "cons": ["Bulkier than standard webcams", "Ring light can drain USB power"],
        "tags": ["ring-light", "streaming", "1080p", "razer", "youtube"],
        "in_stock": True, "data_source": "demo_catalog",
    },
    {
        "id": "wc-elgato-facecam", "name": "Elgato Facecam", "brand": "Elgato",
        "category": "webcams", "category_display": "Webcams",
        "price": 149.99, "currency": "USD", "image_emoji": "📷",
        "specs": {"Resolution": "1080p60 Full HD", "Frame Rate": "60fps", "Sensor": "Sony STARVIS sensor"},
        "merchant": "VoicePay Demo Store", "estimated_tax_rate": 0.08, "estimated_shipping": 0.0,
        "pros": ["Smooth 60fps video", "Sony low-light sensor", "Stream Deck integration"],
        "cons": ["Premium price for a webcam", "Software-heavy"],
        "tags": ["1080p60", "streaming", "professional", "elgato", "60fps"],
        "in_stock": True, "data_source": "demo_catalog",
    },
]


# ── Shopping Pydantic Models ──────────────────────────────────────────────────

class ShoppingParseRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def _check_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be empty")
        if len(v) > 1000:
            raise ValueError("message is too long (max 1000 characters)")
        return v


class ProductSearchRequest(BaseModel):
    budget: float
    currency: str = "USD"
    category: Optional[str] = None
    query: Optional[str] = None
    preferences: list[str] = []
    max_results: int = 3

    @field_validator("budget")
    @classmethod
    def _check_budget(cls, v: float) -> Decimal:
        if v is None or not math.isfinite(v):
            raise ValueError("budget must be a valid finite number")
        try:
            d = Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except InvalidOperation:
            raise ValueError("budget is not a valid decimal")
        if d < Decimal("0.01"):
            raise ValueError("budget must be at least 0.01")
        if d > Decimal("100000.00"):
            raise ValueError("budget cannot exceed 100,000")
        return d

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, v: str) -> str:
        v = v.upper().strip()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(
                f"Currency '{v}' is not supported. Supported: {', '.join(sorted(SUPPORTED_CURRENCIES))}"
            )
        return v

    @field_validator("max_results")
    @classmethod
    def _check_max_results(cls, v: int) -> int:
        return min(max(1, v), 10)

    @field_validator("preferences")
    @classmethod
    def _check_preferences(cls, v: list) -> list:
        return [str(p).strip().lower()[:50] for p in v[:10] if str(p).strip()]


# ── Shopping Helper Functions ─────────────────────────────────────────────────

def _calculate_product_total(product: dict) -> dict:
    """Calculate the estimated total cost for a demo catalog product."""
    price = Decimal(str(product["price"]))
    tax_rate = Decimal(str(product.get("estimated_tax_rate", 0.08)))
    shipping = Decimal(str(product.get("estimated_shipping", 0)))
    tax = (price * tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total = (price + tax + shipping).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "estimated_tax": float(tax),
        "estimated_shipping": float(shipping),
        "total_estimated": float(total),
    }


def _score_product(product: dict, query: str, preferences: list[str], budget: Decimal) -> float:
    """Score a product for relevance to the search request. Higher = better."""
    score = 0.0
    totals = _calculate_product_total(product)
    total = Decimal(str(totals["total_estimated"]))

    # Budget fit: strong positive signal
    if total <= budget:
        score += 10.0
        # Bonus for leaving budget headroom (normalized to 0–2)
        remaining_pct = float((budget - total) / budget)
        score += min(remaining_pct * 2, 2.0)

    product_tags = [t.lower() for t in product.get("tags", [])]
    product_text = (
        product.get("name", "") + " " +
        product.get("brand", "") + " " +
        " ".join(product_tags)
    ).lower()

    # Query word matching
    if query:
        for word in query.lower().split():
            if len(word) >= 3 and word in product_text:
                score += 1.5

    # Preference matching
    for pref in preferences:
        pref_lower = pref.lower()
        if any(pref_lower in tag for tag in product_tags):
            score += 2.0
        elif pref_lower in product_text:
            score += 0.5

    return score


def _assign_comparison_labels(products: list[dict]) -> list[dict]:
    """Assign comparison labels to ranked products (top 3 only)."""
    if not products:
        return products

    prices = [p["price"] for p in products[:3]]
    labeled = []

    for i, product in enumerate(products[:3]):
        p = dict(product)
        if i == 0:
            p["comparison_label"] = "best_overall"
        elif p["price"] == min(prices):
            p["comparison_label"] = "best_budget"
        else:
            p["comparison_label"] = "best_features"
        labeled.append(p)

    return labeled


def _build_recommendation_reason(product: dict, query: str, preferences: list[str], budget: Decimal) -> str:
    """Generate a short plain-language reason why this product is recommended."""
    totals = _calculate_product_total(product)
    total = Decimal(str(totals["total_estimated"]))
    within = total <= budget

    reasons = []

    if within:
        remaining = float(budget - total)
        reasons.append(f"fits within your budget with ${remaining:.2f} remaining after estimated tax")

    product_tags = [t.lower() for t in product.get("tags", [])]
    matched = [p for p in preferences if any(p.lower() in tag for tag in product_tags)]
    if matched:
        reasons.append(f"matches your {', '.join(matched[:2])} requirements")

    specs = product.get("specs", {})
    if specs:
        key, val = next(iter(specs.items()))
        reasons.append(f"features {key}: {val}")

    if not reasons:
        reasons.append(f"is a solid {product.get('category_display', 'product')} option")

    return "This product " + "; ".join(reasons) + "."


async def _ai_parse_shopping(message: str) -> dict:
    """Parse a shopping request into structured preferences.
    Uses OpenAI when available, falls back to a rule-based parser."""

    def _fallback_parse(msg: str) -> dict:
        lower = msg.lower()

        # Budget extraction
        budget: Optional[float] = None
        budget_patterns = [
            r"budget\s+(?:of\s+)?\$?\s*([\d,]+(?:\.\d{1,2})?)",
            r"\$\s*([\d,]+(?:\.\d{1,2})?)",
            r"([\d,]+(?:\.\d{1,2})?)\s*(?:dollars?|usd)",
            r"under\s+\$?\s*([\d,]+)",
            r"less\s+than\s+\$?\s*([\d,]+)",
            r"around\s+\$?\s*([\d,]+)",
            r"spend\s+\$?\s*([\d,]+)",
            r"max(?:imum)?\s+\$?\s*([\d,]+)",
        ]
        for pat in budget_patterns:
            m = re.search(pat, lower)
            if m:
                try:
                    candidate = float(m.group(1).replace(",", ""))
                    if 0 < candidate < 100000:
                        budget = candidate
                        break
                except ValueError:
                    pass

        # Currency
        currency = "USD"
        if re.search(r"\beur(os?)?\b", lower):
            currency = "EUR"
        elif re.search(r"\bgbp\b|\bpound(s)?\b", lower):
            currency = "GBP"

        # Category detection
        category_map = {
            "headphones": ["headphone", "headset", "earphone", "earbuds", "over-ear", "on-ear"],
            "laptops": ["laptop", "notebook", "computer", "macbook", "chromebook"],
            "keyboards": ["keyboard", "mechanical keyboard", "keycap"],
            "monitors": ["monitor", "display", "screen"],
            "smartwatches": ["smartwatch", "watch", "wearable", "fitness tracker"],
            "tablets": ["tablet", "ipad", "kindle"],
            "webcams": ["webcam", "camera", "web camera"],
            "smartphones": ["phone", "smartphone", "iphone", "android phone"],
        }
        category: Optional[str] = None
        for cat, keywords in category_map.items():
            if any(kw in lower for kw in keywords):
                category = cat
                break

        # Preferences
        pref_keywords = [
            "wireless", "bluetooth", "noise cancelling", "noise-cancelling",
            "battery", "portable", "gaming", "professional", "budget",
            "premium", "programming", "ai", "performance", "student",
            "office", "streaming", "waterproof", "fast charging",
        ]
        preferences = [kw for kw in pref_keywords if kw in lower]

        return {
            "budget": budget,
            "currency": currency,
            "category": category,
            "query": msg[:200],
            "preferences": preferences[:8],
            "brand": None,
            "ai_mode": "fallback",
        }

    if not OPENAI_API_KEY:
        return _fallback_parse(message)

    system_prompt = (
        "You are a shopping assistant parser. Extract shopping preferences from natural language.\n"
        "Return ONLY a JSON object with these exact keys:\n"
        '  "budget": number or null\n'
        '  "currency": 3-letter ISO code, default "USD"\n'
        '  "category": one of: headphones, laptops, keyboards, monitors, smartwatches, tablets, webcams, smartphones — or null\n'
        '  "query": string (cleaned search description)\n'
        '  "preferences": array of preference strings (e.g. ["wireless", "programming", "battery"])\n'
        '  "brand": string or null\n'
        "Return null for unknown fields. Supported currencies: USD, EUR, GBP, CAD, AUD."
    )

    try:
        import openai  # optional dependency

        oai = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        chat = await oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Parse: {message[:500]}"},
            ],
            max_tokens=200,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = chat.choices[0].message.content or "{}"
        parsed = json.loads(raw)

        # Server-side validation
        budget_raw = parsed.get("budget")
        budget: Optional[float] = None
        if budget_raw is not None:
            try:
                candidate = float(budget_raw)
                if math.isfinite(candidate) and 0 < candidate < 100000:
                    budget = candidate
            except (TypeError, ValueError):
                pass

        cur_raw = str(parsed.get("currency", "USD")).upper().strip()
        currency = cur_raw if cur_raw in {"USD", "EUR", "GBP", "CAD", "AUD"} else "USD"

        valid_cats = {"headphones", "laptops", "keyboards", "monitors", "smartwatches", "tablets", "webcams", "smartphones"}
        cat_raw = parsed.get("category")
        category = str(cat_raw).lower() if cat_raw and str(cat_raw).lower() in valid_cats else None

        prefs_raw = parsed.get("preferences", [])
        preferences = [
            str(p).strip().lower()[:50]
            for p in (prefs_raw if isinstance(prefs_raw, list) else [])[:10]
            if str(p).strip()
        ]

        brand_raw = parsed.get("brand")
        brand: Optional[str] = str(brand_raw).strip()[:100] if brand_raw else None

        return {
            "budget": budget,
            "currency": currency,
            "category": category,
            "query": str(parsed.get("query", message))[:200],
            "preferences": preferences,
            "brand": brand,
            "ai_mode": "openai",
        }
    except ImportError:
        logger.warning("openai not installed — using shopping fallback parser")
        return _fallback_parse(message)
    except Exception as exc:
        logger.warning("OpenAI shopping parse failed (%s) — using fallback", exc)
        return _fallback_parse(message)


# ── Shopping Endpoints ────────────────────────────────────────────────────────

@app.get("/catalog/categories")
async def get_catalog_categories(request: Request):
    """
    Return the list of available product categories in the demo catalog.
    Demo catalog: prices and availability are for demonstration purposes only.
    """
    ip = _client_ip(request)
    _check_rate_limit(ip, *_RATE_DEFAULT, "/catalog/categories")

    categories: dict[str, dict] = {}
    for p in DEMO_CATALOG:
        cat = p["category"]
        if cat not in categories:
            categories[cat] = {
                "id": cat,
                "display": p["category_display"],
                "count": 0,
                "emoji": p.get("image_emoji", "📦"),
            }
        categories[cat]["count"] += 1

    return {
        "categories": list(categories.values()),
        "total_products": len(DEMO_CATALOG),
        "demo_catalog": True,
        "note": (
            "This is a demonstration catalog. Prices, specifications, "
            "and availability are for demo purposes only."
        ),
    }


@app.post("/parse-shopping")
async def parse_shopping(req: ShoppingParseRequest, request: Request):
    """
    Parse a natural-language shopping request into structured preferences.
    Uses OpenAI when OPENAI_API_KEY is set; deterministic fallback otherwise.
    Does NOT search products or create any orders — parsing only.
    """
    ip = _client_ip(request)
    _check_rate_limit(ip, *_RATE_PARSE, "/parse-shopping")
    _check_content_type_json(request)

    result = await _ai_parse_shopping(req.message)

    # Server-side budget validation (independent of parser source)
    raw_budget = result.get("budget")
    if raw_budget is not None:
        try:
            d = Decimal(str(raw_budget)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if d < Decimal("0.01") or d > Decimal("100000.00"):
                result["budget"] = None
            else:
                result["budget"] = float(d)
        except (InvalidOperation, ValueError):
            result["budget"] = None

    return result


@app.post("/search-products")
async def search_products(req: ProductSearchRequest, request: Request):
    """
    Search the demo product catalog based on budget, category, and preferences.
    Returns filtered and ranked products with transparent comparison criteria.

    IMPORTANT: This endpoint returns DEMO catalog data only.
    Prices, specifications, and availability are for demonstration purposes
    and do NOT represent verified marketplace listings.
    """
    ip = _client_ip(request)
    _check_rate_limit(ip, *_RATE_PARSE, "/search-products")
    _check_content_type_json(request)

    budget: Decimal = req.budget  # Already Decimal from validator
    currency = req.currency
    category = (req.category or "").lower().strip() or None
    query = (req.query or "").strip().lower()
    preferences = req.preferences
    max_results = req.max_results

    # Filter by category when specified
    candidates = DEMO_CATALOG
    if category:
        filtered = [p for p in DEMO_CATALOG if p["category"] == category]
        # If the category matched something, use it; otherwise search all
        candidates = filtered if filtered else DEMO_CATALOG

    # Score and augment each product with totals
    scored: list[tuple[float, dict]] = []
    for product in candidates:
        totals = _calculate_product_total(product)
        product_with_totals = {**product, **totals}
        score = _score_product(product_with_totals, query, preferences, budget)
        scored.append((score, product_with_totals))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Separate within-budget from over-budget
    within_budget: list[dict] = []
    over_budget: list[dict] = []
    for _score, product in scored:
        total = Decimal(str(product["total_estimated"]))
        if total <= budget:
            within_budget.append(product)
        else:
            over_budget.append(product)

    # Select top results and enrich with labels and reasons
    top_products = within_budget[:max_results]
    for p in top_products:
        p["recommendation_reason"] = _build_recommendation_reason(p, query, preferences, budget)
    top_products = _assign_comparison_labels(top_products)

    logger.info(
        "Product search: budget=%s %s category=%s preferences=%s → %d within, %d over",
        budget, currency, category, preferences, len(within_budget), len(over_budget),
    )

    return {
        "products": top_products,
        "within_budget_count": len(within_budget),
        "over_budget_count": len(over_budget),
        "over_budget_alternatives": over_budget[:2] if not top_products else [],
        "budget": float(budget),
        "currency": currency,
        "category_searched": category,
        "query": query,
        "demo_catalog": True,
        "catalog_note": (
            "Products shown are from a demonstration catalog. "
            "Prices, specifications, and availability are for demo purposes only "
            "and do not represent actual marketplace listings."
        ),
        "total_searched": len(scored),
    }


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
