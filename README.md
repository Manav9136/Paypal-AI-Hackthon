# VoicePay AI — Your Personal AI Shopping & Payment Agent

> **Hackathon Project** — PayPal AI Hackathon 2026
> **Status:** Sandbox Mode — All payments are test transactions. No real money is transferred.

---

## Overview

VoicePay AI is an intelligent AI shopping assistant and voice-guided payment platform. Users describe what they need and their budget in plain language — the AI finds the best products, compares them transparently, and guides the user through a secure PayPal Sandbox checkout by voice or text.

### Key Features

| Feature | Description |
|---------|-------------|
| 🤖 **AI Shopping Agent** | Natural language product discovery with budget filtering |
| 🔊 **Voice Input** | Web Speech API — speak your request or type it |
| 🛍️ **Product Comparison** | Transparent ranking by price, features, and value |
| 💳 **PayPal Checkout** | Secure Sandbox checkout with server-side validation |
| 🔒 **Security-First** | Secrets never in browser; amounts verified server-side |
| 📱 **Responsive** | Works on desktop, tablet, and mobile |

---

## Project Structure

```
Paypal-AI-Hackthon-main/
├── backend/
│   ├── main.py              # FastAPI backend — all endpoints
│   ├── requirements.txt     # Python dependencies
│   ├── .env                 # Local credentials (NOT committed)
│   ├── .env.example         # Template — copy to .env
│   ├── voicepay.db          # SQLite database (auto-created)
│   └── tests/
│       ├── test_main.py     # Existing payment endpoint tests
│       └── test_shopping.py # Shopping endpoint tests (new)
└── frontend/
    ├── index.html           # Main HTML — all sections
    ├── style.css            # Design system + shopping styles
    ├── app.js               # Payment flow, voice, navigation
    └── shopping.js          # AI shopping agent module (new)
```

---

## Setup & Installation

### Prerequisites

- Python 3.10+ (tested on 3.12 and 3.13)
- A modern web browser (Chrome/Edge recommended for Web Speech API)
- A PayPal Developer account with a Sandbox app (optional — app runs in demo mode without it)

### 1. Clone and Navigate

```bash
cd "e:\Paypal-AI-Hackthon\Paypal-AI-Hackthon-main"
```

### 2. Create a Virtual Environment

```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment

```bash
copy .env.example .env
```

Edit `backend/.env` and fill in your values:

```env
# Required for PayPal checkout:
PAYPAL_CLIENT_ID=your_sandbox_client_id
PAYPAL_CLIENT_SECRET=your_sandbox_client_secret

# Required for /transactions access control:
DEMO_SESSION_TOKEN=generate_a_long_random_secret

# Optional — enables AI-powered NL parsing:
OPENAI_API_KEY=your_openai_key
```

**Generate a session token:**
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

#### Getting PayPal Sandbox Credentials

1. Go to [developer.paypal.com](https://developer.paypal.com)
2. Log in and click **My Apps & Credentials**
3. Click **Create App** under the Sandbox section
4. Copy the **Client ID** and **Secret**

> ⚠️ **Note:** INR (Indian Rupee) is not supported in standard PayPal Sandbox accounts. Use USD, EUR, GBP, CAD, or AUD.

### 5. Start the Backend

```bash
cd backend
python main.py
# or with uvicorn directly:
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

The API will be available at: `http://localhost:8000`
Interactive docs: `http://localhost:8000/docs` (sandbox/dev only)

### 6. Serve the Frontend

Use any static file server. Options:

**VS Code Live Server** (recommended):
- Install the Live Server extension
- Right-click `frontend/index.html` → Open with Live Server

**Python:**
```bash
cd frontend
python -m http.server 5500
```

**Node.js:**
```bash
cd frontend
npx serve . -p 5500
```

Open `http://localhost:5500` in your browser.

---

## API Endpoints

### Existing Endpoints (preserved)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | None | Health check |
| GET | `/config` | None | Returns PayPal Client ID (never secret) |
| POST | `/parse-payment` | None | Parse natural-language payment request |
| POST | `/create-order` | None | Create PayPal Sandbox order |
| POST | `/capture-order` | None | Capture approved order (strict validation) |
| GET | `/transactions` | `X-Session-Token` | List verified transactions |
| POST | `/session/start` | None | Exchange token → session cookie |

### New Shopping Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/catalog/categories` | None | List available product categories |
| POST | `/parse-shopping` | None | Parse natural-language shopping request |
| POST | `/search-products` | None | Search demo catalog with budget filtering |

#### `POST /parse-shopping`

```json
// Request
{ "message": "I have $250. Find me a laptop for programming." }

// Response
{
  "budget": 250.00,
  "currency": "USD",
  "category": "laptops",
  "query": "laptop for programming",
  "preferences": ["programming"],
  "brand": null,
  "ai_mode": "openai"  // or "fallback" or "demo"
}
```

#### `POST /search-products`

```json
// Request
{
  "budget": 250.00,
  "currency": "USD",
  "category": "laptops",
  "query": "programming laptop",
  "preferences": ["programming", "student"],
  "max_results": 3
}

// Response
{
  "products": [...],
  "within_budget_count": 2,
  "over_budget_count": 3,
  "budget": 250.00,
  "currency": "USD",
  "demo_catalog": true,
  "catalog_note": "Products are from a demonstration catalog..."
}
```

---

## Payment Flow

1. **Shop** — User describes budget and needs → AI recommends products
2. **Select** — User selects a product → checkout summary displayed
3. **Review** — User reviews exact amount, merchant, product name
4. **Confirm** — User explicitly approves before PayPal order is created
5. **Pay** — PayPal Sandbox button appears → user completes checkout
6. **Verify** — Backend verifies amount + currency match before recording
7. **History** — Verified transaction saved to SQLite and displayed

---

## Running Tests

```bash
cd backend
# Activate venv first
python -m pytest tests/ -v
```

### Test Coverage

- `tests/test_main.py` — Health check, config, parse-payment, create-order, capture-order, transactions
- `tests/test_shopping.py` — Catalog categories, parse-shopping, search-products (budget filtering, currency validation)

### Manual PayPal Sandbox Tests

To test the full checkout flow:

1. Create a PayPal Sandbox **buyer account** at [developer.paypal.com](https://developer.paypal.com) → Sandbox → Accounts
2. Start the backend with valid PayPal credentials
3. Open the frontend and start an AI shopping session
4. Select a product → proceed to payment → confirm
5. Log in with the Sandbox buyer account when PayPal opens
6. Complete checkout → verify success message and transaction history

**Test card** (alternative to buyer account):
- Card: `4032030000000000` (Visa test)
- Expiry: any future date
- CVV: any 3 digits

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `APP_ENV` | No | `sandbox` | Environment: `sandbox` or `production` |
| `HOST` | No | `127.0.0.1` | Server bind address |
| `PORT` | No | `8000` | Server port |
| `PAYPAL_CLIENT_ID` | Yes* | — | PayPal Sandbox Client ID |
| `PAYPAL_CLIENT_SECRET` | Yes* | — | PayPal Sandbox Client Secret |
| `PAYPAL_BASE_URL` | No | `https://api-m.sandbox.paypal.com` | PayPal API base URL |
| `OPENAI_API_KEY` | No | — | OpenAI API key (enables AI parsing) |
| `OPENAI_MODEL` | No | `gpt-4o-mini` | OpenAI model for parsing |
| `DATABASE_URL` | No | `sqlite:///./voicepay.db` | Database connection string |
| `DEMO_SESSION_TOKEN` | Yes* | — | Token for /transactions access |
| `CORS_ORIGINS` | No | localhost variants | Allowed frontend origins |

*Required for full functionality; app runs in demo mode without them.

---

## Implemented Features

### ✅ Complete
- Home dashboard with hero, CTAs, three-step overview
- AI Shopping Agent section with voice + text input
- Natural language shopping request parsing (AI or rule-based fallback)
- Demo product catalog (8 categories, 35+ products) with budget filtering
- Product comparison with transparent ranking (Best Overall, Best Budget, Best Features)
- Product cards with pricing breakdown, estimated tax, pros/cons, AI recommendation reason
- Shopping → VoicePay handoff (select product → auto-fills payment form)
- Follow-up queries (cheaper, better battery, compare)
- Over-budget alternatives display
- VoicePay section with microphone + text input (preserved and enhanced)
- Confirmation modal with sandbox notice
- PayPal Sandbox checkout with server-side validation
- Session token authentication for /transactions
- Enhanced transaction history with order ID, capture ID, sandbox badge
- Session notice in Transactions section
- Mobile-responsive layout
- Accessibility: skip links, ARIA labels, keyboard navigation, focus management
- Demo mode fallback when backend is unavailable

### ⚠️ Known Limitations
- Product catalog is demo data — not connected to a real marketplace
- Demo products: prices, specs, and availability are illustrative only
- PayPal INR not supported in standard Sandbox accounts
- Voice input requires Chrome or Edge (Web Speech API)
- Rate limiting is in-memory (per-process) — not suitable for multi-worker deployments
- Session token is simple — not suitable for multi-user production environments

### 🔧 Remaining / Future Work
- Real product catalog integration (e.g., RapidAPI, Amazon PA, Shopify)
- Shopping cart with multiple items
- User accounts and order history
- Currency conversion for international products
- Redis-backed rate limiting for production scale
- A/B tested confirmation UX
- Full end-to-end Playwright test suite

---

## Security Notes

- **Secrets**: `PAYPAL_CLIENT_SECRET`, `OPENAI_API_KEY`, and `DEMO_SESSION_TOKEN` are never exposed to the browser, API responses, or logs.
- **Amount validation**: All monetary amounts use Python `Decimal` arithmetic. Frontend-submitted prices are never trusted for order creation.
- **Capture verification**: Captured amount and currency are verified against the database-stored order before any transaction is recorded.
- **CORS**: Restricted to explicitly configured origins. `"null"` is intentionally excluded.
- **SQL injection**: All database queries use parameterised statements.
- **XSS**: All user-generated content is escaped before HTML insertion.

---

## Startup Commands (Quick Reference)

```bash
# Terminal 1 — Backend
cd "e:\Paypal-AI-Hackthon\Paypal-AI-Hackthon-main\backend"
.venv\Scripts\activate
python main.py

# Terminal 2 — Frontend
cd "e:\Paypal-AI-Hackthon\Paypal-AI-Hackthon-main\frontend"
python -m http.server 5500

# Tests
cd "e:\Paypal-AI-Hackthon\Paypal-AI-Hackthon-main\backend"
.venv\Scripts\activate
python -m pytest tests/ -v
```

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, Uvicorn |
| Database | SQLite (WAL mode, parameterised queries) |
| Payments | PayPal Orders v2 API (Sandbox) |
| AI Parsing | OpenAI GPT-4o-mini (optional) / Rule-based fallback |
| Frontend | HTML5, CSS3, Vanilla JavaScript (ES2022) |
| Voice | Web Speech API (browser-native) |
| Icons | Lucide Icons (CDN) |
| Fonts | Inter (Google Fonts) |

---

*VoicePay AI is a hackathon demonstration project. It is not affiliated with or endorsed by PayPal Holdings, Inc.*
