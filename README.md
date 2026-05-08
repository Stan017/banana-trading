<div align="center">

# 🍌 Banana — AI-Powered Crypto Trading Assistant

**Institutional-grade market analysis, trade journaling, and AI mentorship in one open-source platform.**

> ⚠️ **Live demo temporarily unavailable.** The hosted version requires a server outside the US to access Binance market data (Binance blocks AWS/US IPs). Working on a fix. In the meantime, clone and run locally — it works perfectly.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.x-black?style=flat-square&logo=flask)](https://flask.palletsprojects.com)
[![Claude API](https://img.shields.io/badge/Anthropic-Claude%203.5-orange?style=flat-square)](https://anthropic.com)
[![Qdrant](https://img.shields.io/badge/Qdrant-Vector%20DB-red?style=flat-square)](https://qdrant.tech)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

[Features](#-features) · [Tech Stack](#-tech-stack) · [Architecture](#-architecture) · [Getting Started](#-getting-started) · [Configuration](#-configuration) · [Self-Hosting](#-self-hosting)

</div>

---

## What is Banana?

Banana is a full-stack web application that combines **real-time market intelligence** with an **AI trading mentor** trained on institutional methodology. It connects directly to exchanges, analyzes market structure, and gives traders brutally honest feedback on their trades — no fluff, no generic advice.

Built as a **personal side project** to solve a real problem: most retail traders lose not because of bad entries, but because of bad **process**. Banana tracks your trades, identifies your patterns, and tells you exactly what you're doing wrong.

---

## ✨ Features

### 🤖 AI Trading Assistant (RAG-powered)
- Conversational AI built on **Claude 3.5 Sonnet** with access to a curated knowledge base of institutional trading methodology
- Context-aware responses: the AI knows your current trades, positions, and market conditions before answering
- Real-time market data injection: prices, market regime, volume profile — all pre-fetched before the AI responds
- **Hidden Markov Model** market regime detection (Trending / Ranging / Momentum states)
- Correct perpetual futures math: `PnL = (Notional / Entry) × ΔPrice` — not the simplified (and wrong) `ΔPrice × Leverage`

### 📓 Trade Journal
- Full CRUD for trades: open, close, annotate
- Automatic PnL calculation with leverage adjustment
- Planned and actual R:R tracking
- **CSV import** from Bitunix, Binance, Bybit, and generic formats
- **Bitunix one-click sync** — pulls all closed positions via authenticated API
- Duplicate detection via `exchange_trade_id`
- Stats aggregated at SQL level: win rate, average R:R, PnL, current streak, per-asset breakdown
- Pro tier: deep pattern analysis — Claude reads your full history and identifies systematic errors

### 📊 Market Depth & Liquidity Heatmap
- Real-time **order book heatmap** rendered on canvas behind LightweightCharts candlesticks
- Bid/ask walls, volume imbalance, VPOC, VAH, VAL overlays
- **Liquidity map**: sweep zones, order blocks, Fair Value Gaps (FVG)
- Delta analysis, volume profile, correlation matrix

### 🔍 Confluence Scanner
- Multi-timeframe technical score (0–4) per asset
- Market bias detection (LONG / SHORT / NEUTRAL)
- Kill zone awareness (London open, NY open, NY close)
- Powers the AI's `confianza_bot` score displayed on each open trade

### 🔐 Auth & Plans
- Email/password + **Google OAuth** (Flask-Dance)
- Free tier: journal + chat + scanner
- Pro tier: deep AI analysis, advanced stats
- Session-based auth with CSRF protection and secure cookies

### 🔔 Notifications
- In-app bell with unread badge
- Trade alerts (SL hit, TP hit, liquidation proximity)
- Real-time via polling (WebSocket-ready architecture)

---

## 🛠 Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3.10, Flask 3.x, SQLAlchemy 2.x |
| **Database** | SQLite (dev) / PostgreSQL (prod) |
| **AI** | Anthropic Claude 3.5 Sonnet |
| **Vector DB** | Qdrant Cloud (RAG knowledge base) |
| **Embeddings** | `sentence-transformers` (all-MiniLM-L6-v2) |
| **Market Regime** | Hidden Markov Model (`hmmlearn`) |
| **Exchange APIs** | Binance REST + WebSocket, Bitunix Futures API |
| **Charts** | LightweightCharts v4 (TradingView) + Canvas 2D |
| **Auth** | Flask-Login, Flask-Dance (Google OAuth) |
| **Frontend** | Vanilla JS, CSS custom properties (no framework) |
| **Production** | Gunicorn (single worker, rate-limit safe) |
| **Security** | Flask-Talisman (CSP, HSTS), HMAC signed cookies |

---

## 🏗 Architecture

```
banana/
├── app_flask.py          # Flask app factory, blueprints, auth setup
├── config.py             # Environment config (load_dotenv)
├── models.py             # SQLAlchemy models: Usuario, Journal, Notificacion
├── resources.py          # Shared: Claude client, Qdrant, embedder, RAG helpers
│
├── routes/
│   ├── auth_routes.py    # Login, register, Google OAuth callback
│   ├── chat_routes.py    # AI chat endpoint + context assembly
│   ├── journal_routes.py # Trade CRUD, stats, CSV import, deep analysis
│   ├── bitunix_routes.py # Bitunix key management + sync
│   ├── scanner_routes.py # Confluence scanner API
│   ├── profile_routes.py # User profile + API key storage
│   ├── main_routes.py    # Index, liquidity, depth, edge pages
│   └── admin_routes.py   # Admin panel
│
├── analysis/
│   ├── liquidity.py      # Sweep zones, OB, FVG detection
│   ├── volume_profile.py # VPOC, VAH, VAL calculation
│   ├── delta.py          # Delta and cumulative delta
│   └── ob_fvg.py         # Order block / Fair Value Gap engine
│
├── binance_data.py       # Binance REST + WS: prices, klines, orderbook
├── bitunix_client.py     # Bitunix Futures API (double SHA256 auth)
├── scanner.py            # Multi-TF confluence scoring engine
├── hmm_regime.py         # Hidden Markov Model for market regime
├── notificaciones.py     # Notification engine
├── email_service.py      # Transactional email (SMTP)
│
└── templates/
    ├── index.html        # Main chat interface
    ├── journal.html      # Trade journal
    ├── liquidity.html    # Liquidity map
    ├── depth.html        # Market depth heatmap
    ├── edge.html         # Edge analytics
    └── perfil.html       # User profile
```

### How the AI Chat Works

```
User message
     │
     ▼
┌─────────────────────────────────────────────┐
│  1. Detect if market data is needed         │
│     (regex: "BTC", "price", "entry"...)     │
│                                             │
│  2. If yes → fetch in parallel:             │
│     • Current price (Binance)               │
│     • Market regime (HMM model)             │
│     • Scanner confluences                   │
│     • User's open positions                 │
│                                             │
│  3. Semantic search in Qdrant               │
│     (8,735 chunks of trading methodology)  │
│                                             │
│  4. Assemble system prompt:                 │
│     context + market data + user profile   │
│                                             │
│  5. Claude 3.5 Sonnet → streamed response  │
└─────────────────────────────────────────────┘
```

### Bitunix Authentication (Double SHA256)

```python
step1 = SHA256(nonce + timestamp + api_key + query_params + body)
sign  = SHA256(step1 + secret_key)
# query_params: key+value concatenated, no = or &, sorted ASCII
```

---

## 🚀 Getting Started

### Prerequisites

- Python 3.10+
- A [Qdrant Cloud](https://qdrant.tech) account (free tier works)
- An [Anthropic API key](https://console.anthropic.com)
- A [Binance API key](https://binance.com) (read-only)
- Optional: Google OAuth credentials, Bitunix API key

### Installation

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/banana-trading.git
cd banana-trading

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate      # Linux/Mac
venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy environment template
cp .env.example .env
# Edit .env with your API keys (see Configuration section)

# 5. Initialize the database
python -c "from app_flask import app; from models import db; app.app_context().__enter__(); db.create_all()"

# 6. Index your knowledge base (optional — for AI RAG)
python procesar_docs.py

# 7. Run development server
python app_flask.py
```

Open `http://127.0.0.1:5000` — you're live.

---

## ⚙️ Configuration

Copy `.env.example` to `.env` and fill in your keys:

```env
# ── Flask ──────────────────────────────────────────────
FLASK_SECRET_KEY=your-random-secret-key-here
IS_PROD=false

# ── Database ───────────────────────────────────────────
DATABASE_URL=sqlite:///trading.db
# For production PostgreSQL:
# DATABASE_URL=postgresql://user:password@host:5432/banana

# ── Anthropic (Claude) ─────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...

# ── Qdrant Cloud (RAG knowledge base) ──────────────────
QDRANT_URL=https://YOUR-CLUSTER.qdrant.io
QDRANT_API_KEY=your-qdrant-api-key
QDRANT_COLLECTION=tradebot_kb

# ── Binance (market data — read only) ──────────────────
BINANCE_API_KEY=your-binance-api-key
BINANCE_SECRET=your-binance-secret

# ── Google OAuth (optional) ────────────────────────────
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret

# ── Email (optional — welcome emails) ──────────────────
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@email.com
SMTP_PASSWORD=your-app-password
```

---

## 🐳 Self-Hosting

### Option A: Plain Python (simplest)

```bash
# Production with Gunicorn
pip install gunicorn
gunicorn -c gunicorn.conf.py app_flask:app
```

### Option B: Docker

```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5000
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app_flask:app"]
```

```bash
docker build -t banana .
docker run -p 5000:5000 --env-file .env banana
```

### Option C: Railway / Render / Fly.io (1-click PaaS)

1. Fork this repo
2. Connect to Railway/Render
3. Add environment variables from `.env.example`
4. Deploy — done

> **Note on workers:** The app uses `workers=1` intentionally. The rate limiter is in-memory; multi-worker deployments need Redis. See `gunicorn.conf.py` for details.

---

## 📡 API Reference

All endpoints require authentication (session cookie). Returns JSON.

### Journal

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/journal/trade` | Create a new trade |
| `GET` | `/journal/trades` | List trades (paginated) |
| `GET` | `/journal/stats` | Aggregated statistics |
| `PATCH` | `/journal/trade/:id/cerrar` | Close an open trade |
| `DELETE` | `/journal/trade/:id` | Delete a trade |
| `POST` | `/journal/importar-csv` | Import trades from CSV |
| `POST` | `/journal/analisis-profundo` | AI deep pattern analysis (Pro) |

### Market Data

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/precio/:symbol` | Current price |
| `GET` | `/api/regimen/:symbol` | HMM market regime |
| `GET` | `/api/scanner/:symbol` | Confluence score |
| `GET` | `/api/liquidity/:symbol` | Liquidity zones |
| `GET` | `/api/depth/:symbol` | Order book snapshot |

### Auth

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/auth/register` | Create account |
| `POST` | `/auth/login` | Login |
| `GET` | `/auth/google` | Google OAuth |
| `GET` | `/logout` | Logout |
| `GET` | `/me` | Current user |

---

## 🧠 The Knowledge Base (RAG)

The AI's institutional knowledge comes from a curated collection of trading methodology documents stored as vector embeddings in Qdrant. To add your own:

1. Drop `.pdf` or `.txt` files in the `document/` folder
2. Run `python procesar_docs.py`
3. Documents are chunked, embedded, and upserted to Qdrant automatically

The system uses **cosine similarity** search + **cross-encoder reranking** to find the most relevant context for each user query.

---

## 🔒 Security

- All API routes return `401 JSON` for unauthenticated requests (no redirect leaking)
- Content Security Policy via Flask-Talisman
- HSTS enforced in production
- Bitunix/Binance API keys stored encrypted at the user level, never logged
- Rate limiting: 10 req/s per IP (in-memory, Gunicorn single-worker)
- Input sanitization on all trade fields
- SQL injection protection via SQLAlchemy ORM

---

## 🤝 Contributing

Pull requests are welcome. For major changes, open an issue first.

```bash
# Run tests
python test_journal.py

# Code style
# No linter enforced yet — follow the existing patterns
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

You're free to use, modify, and distribute this code. If you build something cool with it, a ⭐ is appreciated.

---

<div align="center">

Built by **Stanley** · [@devopstanley](mailto:devopstanley@gmail.com)

*"The market doesn't care about your feelings. Neither does this app."*

</div>
