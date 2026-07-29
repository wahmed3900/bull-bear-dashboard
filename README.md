# Bull/Bear Dashboard — Free + Paid ($9/mo)

## What changed from your original script

- **Accounts**: email/password login, stored in **MongoDB Atlas** (not SQLite —
  Render's free tier wipes local files on every restart/redeploy, so persistent
  data has to live somewhere external).
- **Tiers**:
  - **Free** → 5 large-cap stocks (`AAPL, MSFT, GOOGL, NVDA, TSLA`).
  - **Paid** → 10 large-cap stocks + your 18-ticker penny-stock list, plus you can
    add up to 15 of your own tickers from the dashboard.
- **Stripe billing**: Checkout for the $9/mo subscription, a webhook that
  automatically flips a user to `paid` when they subscribe (and back to `free`
  if they cancel), and a billing portal link to manage/cancel.
- **Small perf fix**: added a 30-second in-memory cache per ticker so a bigger
  paid watchlist doesn't hammer Yahoo Finance on every poll.
- Fixed `data-peenystocks.json` → `data-pennystocks.json`, which had a stray
  `json` line at the top that made it invalid JSON.

## 1. Install

```bash
pip install -r requirements.txt
```

## 2. Set up MongoDB Atlas

1. In your Atlas project: **Database → Create/Build a Database → Free (M0)** →
   pick any region → **Create Deployment**.
2. **Database Access**: confirm you have a database user (username + password —
   avoid `@ : /` in the password, or URL-encode them).
3. **Network Access → Add IP Address → Allow Access from Anywhere** (`0.0.0.0/0`)
   — Render's free tier doesn't have a fixed outbound IP.
4. **Database → Connect → Drivers → Python** → copy the connection string, then
   fill in your username/password and a database name, e.g.:
   ```
   mongodb+srv://myuser:mypassword@cluster0.xxxxx.mongodb.net/bullbear?retryWrites=true&w=majority
   ```
   That full string is your `MONGODB_URI`.

## 3. Set up Stripe (test mode first)

1. Create a free Stripe account at https://dashboard.stripe.com (test mode is on by default).
2. **Product & price**: Product catalog → Add product → name it "Bull/Bear Dashboard Pro" →
   add a recurring price of **$9.00 / month**. Copy the price ID (starts `price_...`).
3. **API keys**: Developers → API keys → copy your test **Secret key** (`sk_test_...`)
   and **Publishable key** (`pk_test_...`).
4. Copy `.env.example` to `.env` and fill in `MONGODB_URI`, `STRIPE_SECRET_KEY`,
   `STRIPE_PUBLISHABLE_KEY`, and `STRIPE_PRICE_ID` from the steps above.

## 4. Set up the webhook (so subscriptions actually flip the tier)

Easiest way locally is the Stripe CLI:

```bash
stripe login
stripe listen --forward-to localhost:5000/stripe-webhook
```

That command prints a `whsec_...` value — put it in `.env` as `STRIPE_WEBHOOK_SECRET`.
Keep `stripe listen` running in its own terminal while you test.

(For production, you'd instead add a webhook endpoint in the Stripe Dashboard
pointing at `https://yourdomain.com/stripe-webhook`, listening for
`checkout.session.completed`, `customer.subscription.updated`, and
`customer.subscription.deleted`.)

## 5. Run it

```bash
python app.py
```

Go to `http://localhost:5000`, sign up, and you'll land on the free dashboard.
Click **Upgrade — $9/mo** to test checkout. Stripe test mode card:
`4242 4242 4242 4242`, any future expiry, any CVC.

## Deploying on Render

1. Push this repo to GitHub, connect it to your Render web service.
2. **Render → your service → Environment**: add `SECRET_KEY`, `MONGODB_URI`,
   `MONGODB_DB_NAME`, `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`,
   `STRIPE_PRICE_ID`, `STRIPE_WEBHOOK_SECRET`, and `DOMAIN` (your Render URL,
   e.g. `https://your-app.onrender.com`).
3. In Stripe Dashboard → **Developers → Webhooks → Add endpoint**, point it at
   `https://your-app.onrender.com/stripe-webhook`, and select
   `checkout.session.completed`, `customer.subscription.updated`,
   `customer.subscription.deleted`. Copy the signing secret it gives you into
   `STRIPE_WEBHOOK_SECRET` in Render.
4. Deploy. Because MongoDB Atlas is external, your users and their tiers now
   survive Render restarts/redeploys.

## Notes / next steps you may want

- Passwords are hashed (werkzeug), but there's no email verification or
  password-reset flow yet — worth adding before real users sign up.
- `SECRET_KEY` in `.env.example` is a placeholder — generate a real random
  string for production (e.g. `python -c "import secrets; print(secrets.token_hex(32))"`).
- The custom-ticker cap (15) and cache TTL (30s) are both adjustable constants
  near the top of `app.py`.
- Atlas's free M0 tier is fine to start, but keep an eye on connection limits
  if traffic grows.
