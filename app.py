"""
Bull/Bear Stock Dashboard — with accounts + Stripe subscriptions.
Persistence: MongoDB Atlas (survives Render restarts/redeploys, unlike local SQLite).

Free tier:  5 large-cap stocks, fixed list.
Paid tier ($9/mo): 10 large-cap stocks + your penny-stock list, plus you can
                    add your own custom tickers (up to CUSTOM_TICKER_LIMIT).

Run locally:
    pip install -r requirements.txt
    copy .env.example -> .env and fill in your Stripe + MongoDB values
    python app.py
"""

import os
import json
import time
from datetime import datetime, timedelta
from functools import wraps

import stripe
import yfinance as yf
from flask import (
    Flask, jsonify, request, redirect, url_for, session,
    render_template_string, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient
from bson import ObjectId
from bson.errors import InvalidId

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional; env vars can be set directly instead

import warnings
warnings.filterwarnings("ignore")

# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-production")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")          # price_xxx for the $9/mo plan
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DOMAIN = os.environ.get("DOMAIN", "http://localhost:5000")

MONGODB_URI = os.environ.get("MONGODB_URI", "")
MONGODB_DB_NAME = os.environ.get("MONGODB_DB_NAME", "bullbear")

CUSTOM_TICKER_LIMIT = 15   # max custom tickers a paid user can add
CACHE_TTL_SECONDS = 30     # avoid hammering yfinance on every page refresh

# ============================================================
# MONGODB
# ============================================================

_mongo_client = None
_db = None


def get_db():
    """Lazily create a single shared MongoClient (pymongo pools connections itself)."""
    global _mongo_client, _db
    if _db is None:
        if not MONGODB_URI:
            raise RuntimeError("MONGODB_URI is not set — add it to your .env or Render environment variables.")
        _mongo_client = MongoClient(MONGODB_URI)
        _db = _mongo_client[MONGODB_DB_NAME]
    return _db


def init_db():
    db = get_db()
    db.users.create_index("email", unique=True)


def oid(user_id_str):
    """Safely convert a string back to a Mongo ObjectId, or None if invalid."""
    try:
        return ObjectId(user_id_str)
    except (InvalidId, TypeError):
        return None


# ============================================================
# STOCK LISTS
# ============================================================

FREE_STOCKS = ["AAPL", "MSFT", "GOOGL", "NVDA", "TSLA"]

LARGE_CAP_FULL = ["AAPL", "MSFT", "GOOGL", "NVDA", "META", "TSLA", "AMZN", "JPM", "JNJ", "WMT"]

with open(os.path.join(os.path.dirname(__file__), "data-pennystocks.json")) as f:
    PENNY_STOCKS = json.load(f)

PAID_BASE_STOCKS = LARGE_CAP_FULL + PENNY_STOCKS

# ============================================================
# AUTH HELPERS
# ============================================================

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def get_current_user():
    user_id = oid(session.get("user_id", ""))
    if user_id is None:
        return None
    return get_db().users.find_one({"_id": user_id})


def set_user_tier_by_customer(stripe_customer_id, tier, subscription_id=None):
    update = {"$set": {"tier": tier}}
    if subscription_id:
        update["$set"]["stripe_subscription_id"] = subscription_id
    get_db().users.update_one({"stripe_customer_id": stripe_customer_id}, update)


def set_user_tier_by_email(email, tier, stripe_customer_id=None, subscription_id=None):
    update = {"$set": {"tier": tier}}
    if stripe_customer_id:
        update["$set"]["stripe_customer_id"] = stripe_customer_id
    if subscription_id:
        update["$set"]["stripe_subscription_id"] = subscription_id
    get_db().users.update_one({"email": email}, update)


# ============================================================
# STOCK ANALYSIS (with a tiny in-memory cache)
# ============================================================

_cache = {}  # ticker -> (timestamp, data)


def analyze_stock(ticker):
    now = time.time()
    cached = _cache.get(ticker)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=120)

        stock = yf.Ticker(ticker)
        df = stock.history(start=start_date, end=end_date)

        if df.empty or len(df) < 30:
            return None

        current_price = df["Close"].iloc[-1]
        ma30 = df["Close"].rolling(window=30).mean().iloc[-1]
        price_30d_ago = df["Close"].iloc[-30]
        momentum = ((current_price - price_30d_ago) / price_30d_ago) * 100

        if current_price > ma30 and momentum > 5:
            signal, emoji, color = "STRONG BULLISH", "🚀", "#00ff00"
        elif current_price > ma30 and momentum > 0:
            signal, emoji, color = "BULLISH", "🟢", "#90ff90"
        elif current_price < ma30 and momentum < -5:
            signal, emoji, color = "STRONG BEARISH", "💀", "#ff0000"
        elif current_price < ma30:
            signal, emoji, color = "BEARISH", "🔴", "#ff6666"
        else:
            signal, emoji, color = "NEUTRAL", "⚪", "#ffff00"

        predicted_price = current_price * (1 + momentum / 100)

        try:
            name = stock.info.get("longName", ticker)[:25]
        except Exception:
            name = ticker

        result = {
            "ticker": ticker,
            "name": name,
            "current_price": round(current_price, 2),
            "momentum": round(momentum, 2),
            "predicted_price": round(predicted_price, 2),
            "signal": signal,
            "emoji": emoji,
            "color": color,
            "ma30": round(ma30, 2),
        }
        _cache[ticker] = (now, result)
        return result
    except Exception:
        return None


def stocks_for_user(user):
    """Return the ticker list this user is allowed to see."""
    if user is None or user.get("tier") != "paid":
        return list(FREE_STOCKS)

    custom = user.get("watchlist", [])

    seen = set()
    combined = []
    for t in PAID_BASE_STOCKS + custom:
        if t not in seen:
            seen.add(t)
            combined.append(t)
    return combined


# ============================================================
# AUTH ROUTES
# ============================================================

REGISTER_HTML = """
<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Sign up</title>{{ style }}</head>
<body><div class="authbox">
<h1>📈 Bull/Bear Dashboard</h1>
<h2>Create your free account</h2>
{% with messages = get_flashed_messages() %}{% if messages %}<div class="flash">{{ messages[0] }}</div>{% endif %}{% endwith %}
<form method="POST">
  <input name="email" type="email" placeholder="Email" required>
  <input name="password" type="password" placeholder="Password" required minlength="6">
  <button type="submit">Sign up</button>
</form>
<p>Already have an account? <a href="{{ url_for('login') }}">Log in</a></p>
</div></body></html>
"""

LOGIN_HTML = """
<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Log in</title>{{ style }}</head>
<body><div class="authbox">
<h1>📈 Bull/Bear Dashboard</h1>
<h2>Log in</h2>
{% with messages = get_flashed_messages() %}{% if messages %}<div class="flash">{{ messages[0] }}</div>{% endif %}{% endwith %}
<form method="POST">
  <input name="email" type="email" placeholder="Email" required>
  <input name="password" type="password" placeholder="Password" required>
  <button type="submit">Log in</button>
</form>
<p>No account? <a href="{{ url_for('register') }}">Sign up free</a></p>
</div></body></html>
"""

AUTH_STYLE = """
<style>
  body { font-family: -apple-system, sans-serif; background: linear-gradient(135deg,#0f0c29,#302b63,#24243e);
         min-height: 100vh; display:flex; align-items:center; justify-content:center; margin:0; }
  .authbox { background: rgba(255,255,255,0.08); padding: 40px; border-radius: 15px; width: 320px; color: #fff; text-align:center; }
  h1 { font-size: 1.6em; margin-bottom: 4px; }
  h2 { font-weight: normal; color:#bbb; margin-top:0; }
  input { width: 100%; padding: 10px; margin: 8px 0; border-radius: 8px; border: none; box-sizing: border-box; }
  button { width: 100%; padding: 10px; margin-top: 8px; border-radius: 8px; border: none; background: #667eea; color: #fff; font-weight: bold; cursor: pointer; }
  button:hover { background: #764ba2; }
  a { color: #90caf9; }
  .flash { background: rgba(255,80,80,0.25); padding: 8px; border-radius: 8px; margin-bottom: 10px; font-size: 14px; }
</style>
"""


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        db = get_db()

        if db.users.find_one({"email": email}):
            flash("An account with that email already exists.")
            return redirect(url_for("register"))

        result = db.users.insert_one({
            "email": email,
            "password_hash": generate_password_hash(password),
            "tier": "free",
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
            "watchlist": [],
            "created_at": datetime.now().isoformat(),
        })
        session["user_id"] = str(result.inserted_id)
        return redirect(url_for("dashboard"))

    return render_template_string(REGISTER_HTML, style=AUTH_STYLE)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = get_db().users.find_one({"email": email})
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.")
            return redirect(url_for("login"))
        session["user_id"] = str(user["_id"])
        return redirect(url_for("dashboard"))

    return render_template_string(LOGIN_HTML, style=AUTH_STYLE)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# STRIPE ROUTES
# ============================================================

@app.route("/upgrade")
@login_required
def upgrade():
    user = get_current_user()
    html = """
    <!DOCTYPE html><html><head><meta charset="UTF-8"><title>Upgrade</title>{{ style }}</head>
    <body><div class="authbox">
    <h1>📈 Go Paid — $9/mo</h1>
    <p style="color:#ccc; text-align:left; font-size:14px;">
      Unlocks the full large-cap list, the penny-stock watchlist, and lets you
      add up to {{ limit }} of your own tickers.
    </p>
    {% if tier == 'paid' %}
      <p>You're already on the paid plan 🎉</p>
      <form method="POST" action="{{ url_for('billing_portal') }}"><button type="submit">Manage billing</button></form>
    {% else %}
      <form method="POST" action="{{ url_for('create_checkout_session') }}">
        <button type="submit">Subscribe with Stripe</button>
      </form>
    {% endif %}
    <p><a href="{{ url_for('dashboard') }}">&larr; back to dashboard</a></p>
    </div></body></html>
    """
    return render_template_string(html, style=AUTH_STYLE, tier=user["tier"], limit=CUSTOM_TICKER_LIMIT)


@app.route("/create-checkout-session", methods=["POST"])
@login_required
def create_checkout_session():
    user = get_current_user()
    if not STRIPE_PRICE_ID or not stripe.api_key:
        flash("Stripe isn't configured yet — set STRIPE_SECRET_KEY and STRIPE_PRICE_ID in .env")
        return redirect(url_for("upgrade"))

    try:
        params = {
            "payment_method_types": ["card"],
            "line_items": [{"price": STRIPE_PRICE_ID, "quantity": 1}],
            "mode": "subscription",
            "success_url": DOMAIN + "/success?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": DOMAIN + "/upgrade",
            "client_reference_id": str(user["_id"]),
        }
        if user.get("stripe_customer_id"):
            params["customer"] = user["stripe_customer_id"]
        else:
            params["customer_email"] = user["email"]

        checkout_session = stripe.checkout.Session.create(**params)
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        flash(f"Stripe error: {e}")
        return redirect(url_for("upgrade"))


@app.route("/billing-portal", methods=["POST"])
@login_required
def billing_portal():
    user = get_current_user()
    if not user.get("stripe_customer_id"):
        return redirect(url_for("upgrade"))
    portal = stripe.billing_portal.Session.create(
        customer=user["stripe_customer_id"],
        return_url=DOMAIN + "/upgrade",
    )
    return redirect(portal.url, code=303)


@app.route("/success")
@login_required
def success():
    return redirect(url_for("dashboard"))


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return "", 400

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
        if email:
            set_user_tier_by_email(email, "paid", stripe_customer_id=customer_id, subscription_id=subscription_id)

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        customer_id = obj.get("customer")
        status = obj.get("status")  # active, trialing, past_due, canceled, unpaid...
        tier = "paid" if status in ("active", "trialing") else "free"
        set_user_tier_by_customer(customer_id, tier)

    return "", 200


# ============================================================
# DASHBOARD
# ============================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock Bull/Bear Dashboard</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,sans-serif; background:linear-gradient(135deg,#0f0c29,#302b63,#24243e); min-height:100vh; padding:20px; color:#fff; }
  .container { max-width:1400px; margin:0 auto; }
  .topbar { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }
  .tier-badge { padding:4px 12px; border-radius:12px; font-size:12px; font-weight:bold; }
  .tier-free { background:#555; }
  .tier-paid { background:linear-gradient(135deg,#667eea,#764ba2); }
  .topbar a { color:#90caf9; text-decoration:none; margin-left:14px; font-size:13px; }
  h1 { text-align:center; font-size:2.2em; margin-bottom:6px; background:linear-gradient(135deg,#667eea,#764ba2); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
  .subtitle { text-align:center; color:#aaa; margin-bottom:20px; }
  .upsell { background:rgba(102,126,234,0.15); border:1px solid #667eea; border-radius:12px; padding:14px 20px; text-align:center; margin-bottom:20px; }
  .upsell a { color:#fff; font-weight:bold; }
  .addbox { display:flex; gap:8px; margin-bottom:20px; justify-content:center; }
  .addbox input { padding:8px 12px; border-radius:8px; border:none; }
  .addbox button { padding:8px 16px; border-radius:8px; border:none; background:#667eea; color:#fff; cursor:pointer; font-weight:bold; }
  .stats { display:flex; justify-content:space-around; background:rgba(255,255,255,0.1); border-radius:15px; padding:20px; margin-bottom:30px; flex-wrap:wrap; }
  .stat { text-align:center; } .stat-value { font-size:32px; font-weight:bold; } .stat-label { font-size:12px; color:#aaa; }
  .refresh-bar { text-align:right; color:#aaa; font-size:12px; margin-bottom:20px; }
  .stock-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(320px, 1fr)); gap:20px; }
  .card { background:rgba(255,255,255,0.1); backdrop-filter:blur(10px); border-radius:15px; padding:20px; border-left:4px solid; transition:transform .3s; }
  .card:hover { transform:translateY(-4px); }
  .card-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
  .ticker { font-size:22px; font-weight:bold; }
  .signal-badge { padding:4px 10px; border-radius:20px; font-size:11px; font-weight:bold; background:rgba(0,0,0,0.5); }
  .price { font-size:30px; font-weight:bold; margin:8px 0; }
  .momentum { font-size:16px; margin-bottom:10px; }
  .positive { color:#00ff00; } .negative { color:#ff4444; }
  .details { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:12px; padding-top:12px; border-top:1px solid rgba(255,255,255,0.2); }
  .detail-label { font-size:11px; color:#aaa; } .detail-value { font-weight:bold; }
  @media (max-width:768px){ .stock-grid{grid-template-columns:1fr;} .stats{flex-direction:column; gap:15px;} }
</style>
</head>
<body>
<div class="container">
  <div class="topbar">
    <span class="tier-badge tier-{{ tier }}">{{ 'PAID' if tier == 'paid' else 'FREE' }} PLAN</span>
    <div>
      {% if tier != 'paid' %}<a href="{{ url_for('upgrade') }}">Upgrade — $9/mo</a>{% else %}<a href="{{ url_for('upgrade') }}">Manage billing</a>{% endif %}
      <a href="{{ url_for('logout') }}">Log out</a>
    </div>
  </div>
  <h1>📈 Stock Bull/Bear Dashboard</h1>
  <div class="subtitle">30-day momentum &amp; moving-average signal</div>

  {% if tier != 'paid' %}
  <div class="upsell">
    You're seeing {{ free_count }} of {{ paid_count }}+ stocks. <a href="{{ url_for('upgrade') }}">Upgrade for $9/mo</a> to unlock the full list, penny stocks, and custom tickers.
  </div>
  {% else %}
  <div class="addbox">
    <input id="tickerInput" placeholder="Add a ticker, e.g. PLTR" maxlength="10">
    <button onclick="addTicker()">Add to watchlist</button>
  </div>
  {% endif %}

  <div class="stats" id="stats">
    <div class="stat"><div class="stat-value" id="totalStocks">-</div><div class="stat-label">Total Stocks</div></div>
    <div class="stat"><div class="stat-value" id="bullishCount" style="color:#00ff00">-</div><div class="stat-label">Bullish</div></div>
    <div class="stat"><div class="stat-value" id="bearishCount" style="color:#ff4444">-</div><div class="stat-label">Bearish</div></div>
    <div class="stat"><div class="stat-value" id="bestStock">-</div><div class="stat-label">Best Performer</div></div>
  </div>

  <div class="refresh-bar">Last updated: <span id="lastUpdate">--:--:--</span> <span id="updateIndicator"></span></div>
  <div class="stock-grid" id="stockGrid"><div style="text-align:center; grid-column:1/-1;">Loading stock data...</div></div>
</div>

<script>
async function fetchData() {
  const indicator = document.getElementById('updateIndicator');
  indicator.innerHTML = ' 🔄 Updating...';
  try {
    const response = await fetch('/api/stocks');
    const data = await response.json();
    updateDashboard(data);
    indicator.innerHTML = ' ✅ Live';
    setTimeout(() => { indicator.innerHTML = ''; }, 2000);
  } catch (e) {
    indicator.innerHTML = ' ❌ Error';
  }
}

function updateDashboard(data) {
  document.getElementById('totalStocks').textContent = data.total;
  document.getElementById('bullishCount').textContent = data.bullish;
  document.getElementById('bearishCount').textContent = data.bearish;
  document.getElementById('bestStock').textContent = data.best_performer;
  document.getElementById('lastUpdate').textContent = data.last_update;

  const grid = document.getElementById('stockGrid');
  grid.innerHTML = '';
  data.stocks.forEach(stock => {
    const card = document.createElement('div');
    card.className = 'card';
    card.style.borderLeftColor = stock.color;
    card.innerHTML = `
      <div class="card-header">
        <span class="ticker">${stock.emoji} ${stock.ticker}</span>
        <span class="signal-badge" style="color:${stock.color}">${stock.signal}</span>
      </div>
      <div class="price">$${stock.current_price}</div>
      <div class="momentum ${stock.momentum >= 0 ? 'positive' : 'negative'}">
        ${stock.momentum >= 0 ? '+' : ''}${stock.momentum}% (30-day momentum)
      </div>
      <div class="details">
        <div><div class="detail-label">Company</div><div class="detail-value">${stock.name}</div></div>
        <div><div class="detail-label">30-Day MA</div><div class="detail-value">$${stock.ma30}</div></div>
      </div>`;
    grid.appendChild(card);
  });
}

async function addTicker() {
  const input = document.getElementById('tickerInput');
  const ticker = input.value.trim().toUpperCase();
  if (!ticker) return;
  const res = await fetch('/api/watchlist/add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ticker})
  });
  const result = await res.json();
  if (!result.success) { alert(result.error); return; }
  input.value = '';
  fetchData();
}

fetchData();
setInterval(fetchData, 30000);
</script>
</body>
</html>
"""


@app.route("/")
@login_required
def dashboard():
    user = get_current_user()
    return render_template_string(
        DASHBOARD_HTML,
        tier=user["tier"],
        free_count=len(FREE_STOCKS),
        paid_count=len(PAID_BASE_STOCKS),
    )


@app.route("/api/stocks")
@login_required
def get_stocks():
    user = get_current_user()
    tickers = stocks_for_user(user)

    results = []
    bullish = bearish = 0
    for ticker in tickers:
        data = analyze_stock(ticker)
        if data:
            results.append(data)
            if "BULLISH" in data["signal"]:
                bullish += 1
            elif "BEARISH" in data["signal"]:
                bearish += 1

    results.sort(key=lambda x: x["momentum"], reverse=True)
    best = results[0] if results else None

    return jsonify({
        "stocks": results,
        "total": len(results),
        "bullish": bullish,
        "bearish": bearish,
        "best_performer": f"{best['ticker']} (+{best['momentum']:.1f}%)" if best else "-",
        "last_update": datetime.now().strftime("%H:%M:%S"),
    })


@app.route("/api/watchlist/add", methods=["POST"])
@login_required
def add_to_watchlist():
    user = get_current_user()
    if user.get("tier") != "paid":
        return jsonify({"success": False, "error": "Custom tickers are a paid feature. Upgrade for $9/mo."}), 403

    ticker = (request.json or {}).get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"success": False, "error": "No ticker provided."}), 400

    if len(user.get("watchlist", [])) >= CUSTOM_TICKER_LIMIT:
        return jsonify({"success": False, "error": f"You've hit the {CUSTOM_TICKER_LIMIT}-ticker custom limit."}), 400

    # quick validity check
    check = analyze_stock(ticker)
    if check is None:
        return jsonify({"success": False, "error": f"Couldn't find data for '{ticker}'. Check the symbol."}), 400

    get_db().users.update_one({"_id": user["_id"]}, {"$addToSet": {"watchlist": ticker}})
    return jsonify({"success": True})


@app.route("/api/watchlist/remove", methods=["POST"])
@login_required
def remove_from_watchlist():
    user = get_current_user()
    ticker = (request.json or {}).get("ticker", "").strip().upper()
    get_db().users.update_one({"_id": user["_id"]}, {"$pull": {"watchlist": ticker}})
    return jsonify({"success": True})


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("📈 Bull/Bear Dashboard — Free + Paid ($9/mo)")
    print("=" * 60)
    print(f"\n🌐 http://localhost:5000")
    print("   /register  /login  /upgrade")
    if not MONGODB_URI:
        print("\n⚠️  MongoDB isn't configured — set MONGODB_URI in .env")
    if not stripe.api_key or not STRIPE_PRICE_ID:
        print("\n⚠️  Stripe isn't fully configured — set STRIPE_SECRET_KEY / STRIPE_PRICE_ID in .env")
    print("=" * 60 + "\n")
    app.run(debug=False, host="0.0.0.0", port=5000)
