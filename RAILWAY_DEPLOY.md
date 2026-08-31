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
| `ADMIN_TOKEN` | `any-random-string` | Protects admin dashboard + API endpoints |

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

### 3. Set Environment Variables

Go to the **Variables** tab in Railway and add these:

**Required:**
```
WHATSAPP_API_TOKEN=EAAxxxxx
WHATSAPP_PHONE_NUMBER_ID=1234567890
WHATSAPP_APP_SECRET=your_app_secret
WHATSAPP_VERIFY_TOKEN=your_unique_verify_token
GROQ_API_KEY=gsk_xxxxx
ADMIN_TOKEN=some_random_string_here
```

**Recommended:**
```
WPBOT_SECRETS_KEY=generate-a-long-random-string-here
```

### 4. Add Persistent Storage (Railway Volume)

Railway containers are ephemeral — SQLite data is lost on redeploy without a volume:

1. In your Railway service → **Settings** → **Volumes**
2. Click **"Add Volume"**
3. Set mount path to `/app/data`
4. This ensures SQLite databases survive restarts

### 5. Configure Custom Domain

1. In Railway → **Settings** → **Networking**
2. Click **"Generate Domain"** for a free `*.up.railway.app` URL
3. Or add your custom domain and configure DNS

### 6. Set WhatsApp Webhook URL

In Meta Developer Dashboard → WhatsApp → Configuration → Webhook:
- **Webhook URL**: `https://your-app.up.railway.app/webhook`
- **Verify Token**: Must match your `WHATSAPP_VERIFY_TOKEN` env var
- **Subscribe to**: `messages` events

### 7. Verify Deployment

After deployment, test these endpoints:

```bash
# Health check
curl https://your-app.up.railway.app/health

# Landing page
curl https://your-app.up.railway.app/

# Admin dashboard (if ADMIN_TOKEN is set)
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  https://your-app.up.railway.app/dashboard/
```

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
