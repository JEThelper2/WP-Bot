# WP-Bot Railway Deployment Guide

## Architecture (All-in-One)

```
┌─────────────────────────────────────────────────────┐
│                 Railway Service                      │
│                                                     │
│  ┌──────────────────┐  ┌─────────────────────────┐  │
│  │    Track A        │  │      Track B             │  │
│  │  (port $PORT)     │──│  (port 8200 internal)    │  │
│  │  WhatsApp webhook │  │  WordPress writes        │  │
│  │  AI intent parse  │  │  Change log + undo       │  │
│  │  Landing page (/) │  │                          │  │
│  └──────────────────┘  └─────────────────────────┘  │
│          │                       │                   │
│     SQLite DB               Redis (optional)        │
│    (in-container)          Postgres (optional)      │
└─────────────────────────────────────────────────────┘
```

- **Track A** listens on Railway's `$PORT` (required). It also serves the static landing page at `/`.
- **Track B** listens on port 8200 (internal only, not exposed).
- Both share the same network namespace (localhost).
- SQLite databases persist on the Railway volume at `/app/data/`.

---

## Environment Variables

### Required for Production

| Variable | Example | Description |
|----------|---------|-------------|
| `WHATSAPP_API_TOKEN` | `EAAxxxxxx` | WhatsApp Business System User access token |
| `WHATSAPP_PHONE_NUMBER_ID` | `1234567890` | Your WhatsApp Business phone number ID |
| `WHATSAPP_APP_SECRET` | `abc123...` | Meta app secret — enables webhook signature verification |
| `WHATSAPP_VERIFY_TOKEN` | `my-unique-token` | Meta webhook verification token (must match Meta dashboard) |
| `GROQ_API_KEY` | `gsk_xxxxxx` | Groq API key for AI intent parsing |
| `ADMIN_USERNAME` | `admin` | Username for admin dashboard login |
| `ADMIN_PASSWORD` | `your-secure-password` | Password for admin dashboard login |
| `ADMIN_TOKEN` | `any-random-string` | (Optional) Bearer token for API access to admin endpoints |

### Optional (Recommended)

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PROVIDER` | `groq` | AI provider for intent parsing (`groq` or `gemini`) |
| `AI_FALLBACK_PROVIDER` | _(empty)_ | Fallback AI provider on rate limit (e.g. `gemini`) |
| `GEMINI_API_KEY` | _(empty)_ | Google Gemini API key (if using Gemini) |
| `TRANSCRIPTION_PROVIDER` | `groq` | Voice note transcription provider |
| `GROQ_MODEL` | _(auto)_ | Override Groq model (default is auto-selected) |

### Optional (Track B — WordPress Integration)

| Variable | Default | Description |
|----------|---------|-------------|
| `WPBOT_SECRETS_KEY` | _(empty)_ | Encryption key for stored WordPress passwords. **Critical for production** — without it, passwords are lost on restart |
| `WPBOT_REDIS_URL` | `redis://localhost:6379/0` | Redis URL for pending confirmations. Falls back to in-memory if unavailable |
| `WPBOT_PG_DSN` | _(empty)_ | Postgres DSN for change log durability. Falls back to in-memory if empty |

### Optional (Telegram — for testing without WhatsApp)

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | _(empty)_ | Telegram Bot API token |
| `TELEGRAM_WEBHOOK_SECRET` | _(empty)_ | Secret token for Telegram webhook validation |

---

## Step-by-Step Railway Deployment

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "Initial WP-Bot commit"
git remote add origin https://github.com/YOUR_USER/wp-bot.git
git push -u origin main
```

### 2. Create Railway Project

1. Go to [railway.app](https://railway.app) → sign in with GitHub
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Select your `wp-bot` repository
4. Railway will detect the `Dockerfile` and start building
5. Wait for the first deploy to finish (it will show a deploy log)

### 3. Add Postgres Database

Postgres is needed for Track B's change log (audit trail + undo). Without it, changes are stored in-memory and lost on restart.

1. In your Railway project dashboard, click **"＋ New"** (top-left)
2. Select **"Database"** → **"PostgreSQL"**
3. Railway provisions a Postgres instance (~30 seconds)
4. Click on the Postgres service → go to the **"Variables"** tab
5. Copy the value of `DATABASE_URL` (looks like `postgresql://user:pass@host:5432/dbname`)
6. Go back to your **main WP-Bot service** → **"Variables"** tab
7. Add this variable:
   ```
   WPBOT_PG_DSN=postgresql://user:pass@host:5432/dbname
   ```
   Paste the `DATABASE_URL` value you copied.

> **Note:** Railway also sets `DATABASE_URL` automatically on services linked to the Postgres plugin, but WP-Bot reads `WPBOT_PG_DSN`, so you must map it manually.

### 4. Add Redis

Redis is needed for Track B's pending confirmations (the YES/NO flow). Without it, confirmations are in-memory and lost on restart.

1. In your Railway project dashboard, click **"＋ New"** (top-left)
2. Select **"Database"** → **"Redis"**
3. Railway provisions a Redis instance (~15 seconds)
4. Click on the Redis service → go to the **"Variables"** tab
5. Copy the value of `REDIS_URL` (looks like `redis://default:password@host:6379`)
6. Go back to your **main WP-Bot service** → **"Variables"** tab
7. Add this variable:
   ```
   WPBOT_REDIS_URL=redis://default:password@host:6379
   ```
   Paste the `REDIS_URL` value you copied.

### 5. Set Remaining Environment Variables

Go to your **WP-Bot service** → **"Variables"** tab and add all of these:

**Required:**
```
WHATSAPP_API_TOKEN=EAAxxxxx
WHATSAPP_PHONE_NUMBER_ID=1234567890
WHATSAPP_APP_SECRET=your_app_secret
WHATSAPP_VERIFY_TOKEN=your_unique_verify_token
GROQ_API_KEY=gsk_xxxxx
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-secure-password-here
ADMIN_TOKEN=optional_api_token_here
WPBOT_SECRETS_KEY=generate-a-long-random-string-here
```

> **Tip:** Generate a strong `WPBOT_SECRETS_KEY` with: `python3 -c "import secrets; print(secrets.token_hex(32))"`
> This key encrypts stored WordPress passwords. If it changes after onboarding, all saved credentials become undecryptable.

**Already set by Railway plugins (do NOT overwrite):**
- `WPBOT_PG_DSN` — set in step 3
- `WPBOT_REDIS_URL` — set in step 4

### 6. Add Persistent Storage (Railway Volume)

Railway containers are ephemeral — SQLite data is lost on redeploy without a volume:

1. In your Railway service → **"Settings"** tab
2. Scroll to **"Volumes"** section
3. Click **"Add Volume"**
4. Set **mount path** to `/app/data`
5. Click **"Add"**
6. Redeploy the service (click **"Deploy"** → **"Redeploy"**)

This ensures the SQLite databases (`inbound.db` for Track A, `trackb.db` for Track B) survive restarts and deploys.

### 7. Generate a Domain

Railway provides a free subdomain. You can also use a custom domain.

**Free Railway domain:**
1. In your Railway service → **"Settings"** tab
2. Scroll to **"Networking"** section
3. Click **"Generate Domain"**
4. Railway assigns something like `wp-bot-production.up.railway.app`
5. Your app is now live at that URL

**Custom domain (e.g. `bot.yourdomain.com`):**
1. In your Railway service → **"Settings"** tab → **"Networking"**
2. Click **"Custom Domain"**
3. Enter your domain (e.g. `bot.yourdomain.com`)
4. Railway shows you DNS records to configure — typically:
   ```
   Type:  CNAME
   Name:  bot
   Value: wp-bot-production.up.railway.app
   ```
   Or if Railway gives you an A record:
   ```
   Type:  A
   Name:  bot
   Value: 75.2.60.5  (Railway's IP)
   ```
5. Go to your domain registrar (Namecheap, Cloudflare, GoDaddy, etc.)
6. Add the DNS record exactly as shown
7. Wait for DNS propagation (5 minutes to 48 hours, usually < 30 min)
8. Back in Railway, click **"Verify"** — it will confirm when DNS resolves
9. Railway auto-provisions a free SSL certificate (Let's Encrypt)
10. Your site is now live at `https://bot.yourdomain.com`

> **Cloudflare users:** Set the DNS record proxy status to **DNS Only** (grey cloud) for the CNAME/A record. Railway handles SSL itself — Cloudflare's proxy can interfere with the certificate provisioning.

### 8. Set WhatsApp Webhook URL

In Meta Developer Dashboard → WhatsApp → Configuration → Webhook:
- **Webhook URL**: `https://your-domain.com/webhook`
- **Verify Token**: Must match your `WHATSAPP_VERIFY_TOKEN` env var
- **Subscribe to**: `messages` events

### 9. Verify Deployment

After deployment, test these endpoints:

```bash
# Health check
curl https://your-domain.com/health

# Landing page
curl https://your-domain.com/

# Static pages
curl https://your-domain.com/site/privacy.html
curl https://your-domain.com/site/onboarding.html

# Admin dashboard — open in browser
# Navigate to: https://your-domain.com/admin/login
# Enter the ADMIN_USERNAME and ADMIN_PASSWORD you set

# API access (if ADMIN_TOKEN is set)
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  https://your-domain.com/admin/dashboard

# Prometheus metrics
curl https://your-domain.com/metrics
```

**Check Railway deploy logs for errors:**
1. In your Railway service → **"Deployments"** tab
2. Click the latest deploy
3. Check the **"Build Logs"** and **"Deploy Logs"** tabs
4. Look for any Python tracebacks or connection errors

**Verify databases are connected:**
1. Hit `GET /health` — it should return `{"status": "ok", ...}`
2. In Railway, check the Postgres and Redis service panels — they should show **"Active"** status
3. Send a test WhatsApp message and check `/messages` to confirm the pipeline works end-to-end

---

## Local Development

```bash
# Install dependencies
pip install -e ./shared-contract
pip install -e ./track-a
pip install -e ./track-b

# Run Track A (port 8000)
uvicorn track_a.main:app --reload --port 8000

# Run Track B (port 8200) — in a separate terminal
uvicorn track_b.main:app --reload --port 8200
```

---

## What Gets Deployed

| Component | Port | URL Path | Purpose |
|-----------|------|----------|---------|
| Landing page | 8000 | `/` | Marketing site |
| Track A webhook | 8000 | `/webhook` | WhatsApp message handling |
| Track A health | 8000 | `/health` | Health check |
| Track A admin | 8000 | `/admin/*`, `/dashboard/*` | Monitoring |
| Track B API | 8200 | Internal only | WordPress integration |
| Static files | 8000 | `/site/*` | Privacy, Terms, Onboarding pages |
