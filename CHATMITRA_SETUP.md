# Chat Mitra setup checklist (WhatsApp integration)

The bot sends and receives WhatsApp messages through Chat Mitra
(replacing the earlier AiSensy integration). Do this once, using their
[documentation](https://chatmitra.com/documentation/whatsapp-business-api/)
as the source of truth if anything here looks out of date.

## 1. Get your API token

**Dashboard → Settings → API Keys** — copy your Bearer token.

## 2. Create the webhook

**Dashboard → Settings → API & Integrations → Webhooks → Add Webhook**

- URL: your deployed bot's `/webhook` endpoint (e.g.
  `https://decanter-bot.onrender.com/webhook`) — must be public HTTPS, not
  localhost. You can't test the receiving side against localhost; use the
  Render deployment for that, or a tunnel (ngrok etc.) if you need to test
  locally first.
- Event types: subscribe to **`message.received`** only — that's the only
  event this bot acts on (it ignores `message.sent`/`message.failed`/
  `message.status.updated` if they arrive, but there's no reason to send the
  extra traffic).
- Click **Create Webhook**, then **copy the webhook secret immediately** —
  Chat Mitra can't show it to you again. Paste it into `.env` right away (see
  step 3) or you'll have to delete and recreate the webhook to get a new one.
- Use the **Test Webhook** button afterward to confirm your endpoint returns
  200 OK.

## 3. Set environment variables

In your local `.env` (already gitignored):

```env
CHATMITRA_API_TOKEN=<Bearer token from step 1>
CHATMITRA_WEBHOOK_SECRET=<webhook secret from step 2>
```

Then set the same two values in **Render → your service → Environment**.

## What changed from AiSensy

- `app/aisensy.py` is gone — `app/chatmitra.py` sends replies now (same
  `send_reply(to, message_text)` signature, nothing else in the codebase
  changed because of that).
- The webhook payload shape, signature header (`X-Webhook-Signature`), and
  algorithm (HMAC-SHA256 hex digest of the raw body) all come straight from
  Chat Mitra's documentation — nothing here is guessed, unlike the old AiSensy
  integration.
- There's no webhook verification handshake to configure (no `hub.verify_token`
  / challenge step) — Chat Mitra just starts POSTing events once the webhook
  is created.

## What you never need to give Claude / paste into chat

Your API token and webhook secret are Chat Mitra account credentials — fill
them into `.env` and Render's dashboard yourself. There's no step here that
requires pasting either into the chat.
