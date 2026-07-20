# WhatsApp Perfume Decanter Price-Query Bot Backend

A FastAPI backend that receives inbound WhatsApp messages via Chat Mitra webhooks, classifies them against a perfume catalog using a 3-layer matching pipeline, and replies with a formatted price card.

---

## User Review Required

> [!NOTE]
> **Update (migration to Chat Mitra):** the bot originally integrated with AiSensy, whose API shape had to be guessed from web research (see the superseded warning this replaced, below). It has since been fully migrated to [Chat Mitra](https://chatmitra.com/documentation/whatsapp-business-api/), whose endpoint, payload shapes, and webhook signature scheme are all explicitly documented — nothing about the integration below is a guess anymore. See `CHATMITRA_SETUP.md` for the account setup steps (API token, webhook creation) still required before go-live.
>
> ~~AiSensy API integration is based on best-effort research, not confirmed documentation. AiSensy does not publish a fully open API spec. Before go-live, the business owner MUST confirm the exact inbound webhook JSON payload, the correct endpoint for session replies, and whether AiSensy provides a webhook signing secret.~~ (superseded — see above)

> [!WARNING]
> **Placeholder prices only.** The catalog shipped with this build contains example perfumes with made-up prices. The real catalog (keywords, aliases, confirmed prices for all 7 size tiers) must be supplied by the business owner before launch. Do NOT go live with placeholder prices.

## Open Questions

> [!IMPORTANT]
> **Q1 — Non-text messages (image, voice, sticker, location):** The spec asks us to confirm whether these should get a short auto-reply prompting the user to type, or be silently ignored for the owner to handle. **Defaulting to**: send a short text prompt (`"Please type your question and I'll help with pricing 🙂"`) per the spec's instruction to default to a short text prompt if unconfirmed.

> [!IMPORTANT]
> **Q2 — Full price card vs. single-size reply:** The spec says we may always send the full card (all sizes) even if a specific size is asked about, and to default to "send the full card" if unconfirmed. **Defaulting to**: always send the full price card.

> [!IMPORTANT]
> **Q3 — Webhook signature verification (resolved by the Chat Mitra migration):** Chat Mitra's documentation confirms a signing secret (`X-Webhook-Signature`, HMAC-SHA256 hex digest of the raw body). The code includes an HMAC verification layer that activates when `CHATMITRA_WEBHOOK_SECRET` is set — set it (see `CHATMITRA_SETUP.md`) before go-live; leaving it unset only skips verification for local dev convenience.

---

## Proposed Changes

### Project structure

```
decanter/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, health check, webhook endpoint
│   ├── catalog.py            # Perfume catalog data + shipping card (the ONLY file to edit for prices)
│   ├── matcher.py            # 3-layer matching pipeline (exact → fuzzy → LLM)
│   ├── formatter.py          # Build the WhatsApp reply text from matched perfume data
│   ├── chatmitra.py           # Chat Mitra send-message client
│   ├── groq_client.py        # Groq LLM classification client
│   ├── config.py             # Settings loaded from environment variables
│   └── dedup.py              # In-memory message-ID deduplication cache with TTL
├── tests/
│   ├── __init__.py
│   ├── test_matcher.py       # Unit tests for the matching pipeline
│   ├── test_formatter.py     # Unit tests for reply formatting
│   └── test_webhook.py       # Integration tests for the webhook endpoint (mocked Chat Mitra)
├── requirements.txt          # Pinned dependencies
├── .env.example              # All required env vars documented
├── render.yaml               # Render deployment blueprint
├── README.md                 # Architecture, how-to guides, pre-launch checklist
└── .gitignore
```

---

### Data layer

#### [NEW] [catalog.py](file:///c:/Users/ABC/Downloads/decanter/app/catalog.py)

Single-source-of-truth for perfume data and shipping card. Designed so a non-developer can hand us a new perfume and we add it with a few-line edit.

- `PERFUMES` dict keyed by unique `perfume_id`.
- Each entry: `keywords` (list of lowercase strings including common misspellings), `display_name`, `prices` dict for all 7 tiers (`3ml`, `5ml`, `8ml`, `10ml`, `20ml`, `30ml`, `100ml_full`).
- `SHIPPING_CARD` constant string, stored once, appended to every reply.
- Will ship with **2 example perfumes** (Dior Sauvage, Bleu de Chanel) with clearly marked placeholder prices.

---

### Matching pipeline

#### [NEW] [matcher.py](file:///c:/Users/ABC/Downloads/decanter/app/matcher.py)

Implements the 3-layer pipeline per spec §6:

**Layer 1 — Normalize + substring match:**
- Lowercase, strip punctuation/extra whitespace from incoming message.
- For each perfume, check if any keyword is a substring of the normalized message.
- If multiple perfumes match, prefer the longest matching keyword (most specific).
- If exactly one perfume matches → return it.

**Layer 2 — Fuzzy match (`rapidfuzz`):**
- If Layer 1 fails, extract individual words and sliding n-grams (2-word, 3-word) from the message.
- Compare each against all keywords using `rapidfuzz.fuzz.partial_ratio`.
- Threshold: **80/100** (configurable via `FUZZY_THRESHOLD` env var).
- If best score ≥ threshold and only one perfume matches → return it.
- If multiple perfumes exceed the threshold, only accept if one is clearly dominant (score gap ≥ 10); otherwise treat as ambiguous → fallback.

**Layer 3 — Groq LLM classification:**
- Only called when Layers 1 & 2 both fail.
- System prompt instructs the model to return ONLY a `perfume_id` from the known list or the literal word `none`.
- Model: `llama-3.1-8b-instant`, `temperature=0`.
- Strict parsing: if response is not in the catalog's id list → treat as `none`.
- Wrapped in try/except: any exception (timeout, malformed response, API error) → treat as no match.

**Multi-match detection (across all layers):**
- If the message contains keywords for 2+ different perfumes (e.g., "sauvage vs bleu de chanel"), return a special "ambiguous" result.
- The webhook handler sends a clarification message instead of a price card.

**Return value:** A `MatchResult` dataclass containing:
- `perfume_id: Optional[str]`
- `layer: Optional[str]` ("exact", "fuzzy", "llm", or None)
- `ambiguous: bool`
- `confidence: Optional[float]`

---

### Reply formatting

#### [NEW] [formatter.py](file:///c:/Users/ABC/Downloads/decanter/app/formatter.py)

- `build_price_card(perfume_id) → str`: Looks up perfume in catalog, formats the 2-column price layout per spec §5 with WhatsApp `*bold*` formatting, appends `SHIPPING_CARD`, returns the full message text as a single string.
- `FALLBACK_MESSAGE = "Which perfume are you asking about? 🙂"`
- `AMBIGUOUS_MESSAGE = "I found more than one perfume in your message — which one are you asking about? 🙂"`
- `NON_TEXT_MESSAGE = "Please type your question and I'll help with pricing 🙂"`
- Prices ≥ 1000 get comma formatting (e.g., `₹1,099`).

---

### Chat Mitra integration

#### [chatmitra.py](app/chatmitra.py)

- `send_reply(to: str, message_text: str) → bool`
- Calls `POST https://backend.chatmitra.com/developer/api/send_message` with:
  ```json
  {
    "recipient_mobile_number": "<phone_number>",
    "messages": [{"kind": "raw", "payload": {"type": "text", "text": {"body": "<message_text>"}}}]
  }
  ```
- Headers: `Authorization: Bearer <CHATMITRA_API_TOKEN>`, `Content-Type: application/json`.
- Success is **HTTP 202** (`response.is_success`, not a hardcoded `== 200`) with body `{"status": "success", "jobId": "..."}` — the `jobId` is logged for support/debugging.
- Only sends "raw" (session) text messages — always valid here since we only ever reply within the 24-hour window a customer's own inbound message just opened.
- Returns `True` on success, logs full error details on failure.
- Uses `httpx.AsyncClient` with a 10-second timeout.

Confirmed directly from [Chat Mitra's documentation](https://chatmitra.com/documentation/whatsapp-business-api/api-reference-docs/) — see `CHATMITRA_SETUP.md` for the account-side setup (API token, webhook creation).

---

### Groq LLM client

#### [NEW] [groq_client.py](file:///c:/Users/ABC/Downloads/decanter/app/groq_client.py)

- Uses the OpenAI-compatible Python SDK (`openai` package) pointed at `https://api.groq.com/openai/v1`.
- System prompt: dynamically built from the catalog's list of `perfume_id`s and their display names.
- Single function: `classify_perfume(message: str) → Optional[str]` returning a `perfume_id` or `None`.
- All exceptions caught and logged; never raises.

---

### Configuration

#### [NEW] [config.py](file:///c:/Users/ABC/Downloads/decanter/app/config.py)

Pydantic `BaseSettings` class loading from environment:
- `CHATMITRA_API_TOKEN` (required)
- `CHATMITRA_WEBHOOK_SECRET` (optional — enables HMAC verification if set)
- `GROQ_API_KEY` (required)
- `FUZZY_THRESHOLD` (default: 80)
- `MAX_MESSAGE_LENGTH` (default: 500 — reject messages longer than this)
- `DEDUP_TTL_SECONDS` (default: 300 — message ID dedup window)

---

### Deduplication

#### [NEW] [dedup.py](file:///c:/Users/ABC/Downloads/decanter/app/dedup.py)

- In-memory `dict` mapping `message_id → timestamp`.
- `is_duplicate(message_id: str) → bool`: returns `True` if already seen within `DEDUP_TTL_SECONDS`.
- Periodic cleanup: on each call, evict entries older than TTL. Capped at 10,000 entries max (LRU eviction).
- Ephemeral by design — Render restarts clear this, which is fine (worst case: a message gets replied to twice across a redeploy, extremely rare).

---

### Webhook handler (main application)

#### [NEW] [main.py](file:///c:/Users/ABC/Downloads/decanter/app/main.py)

**`GET /health`** — returns `{"status": "ok"}`, no side effects, no external calls. For UptimeRobot pings.

**`POST /webhook`** — the core handler:

1. **Verify signature** (if `CHATMITRA_WEBHOOK_SECRET` is set): check the `X-Webhook-Signature` HMAC header, reject 403 if invalid.
2. **Parse payload**: ignore anything where `event != "message.received"` (Chat Mitra also delivers `message.sent`/`message.failed`/`message.status.updated` to the same URL if subscribed). Otherwise extract `message_id`, `from` (sender phone), `message.type`, `message.text`.
3. **Order-confirmation short-circuit**: the website's "confirm my order" template (see `app/order_confirmation.py`) skips the matcher entirely — checked before the length cutoff since an order with many line items can be long.
4. **Sanity checks**: reject if message too long (`MAX_MESSAGE_LENGTH`), if message type is not text → send `NON_TEXT_MESSAGE` reply.
5. **Dedup**: if `is_duplicate(message_id)` → return 200 silently. This runs ahead of every reply-sending branch, which matters here — Chat Mitra retries non-2xx/timeout responses up to 3 times.
6. **Match**: call `match_perfume(message_text)` (the 3-layer pipeline).
7. **Build reply**:
   - If `ambiguous` → send `AMBIGUOUS_MESSAGE`.
   - If `perfume_id` is `None` → send `FALLBACK_MESSAGE`.
   - Otherwise → send `build_price_card(perfume_id)`.
8. **Send reply** via `chatmitra.send_reply(from, reply_text)`.
9. **Log**: message text, match result (perfume_id / layer / confidence / ambiguous), success/failure of send — to stdout (Render captures it) and, if Supabase is configured, to `message_events` for the analytics dashboard.
10. **Return 200** immediately (Chat Mitra expects a fast ack, and retries on timeout).

Processing is synchronous in the FastAPI async handler (the matching pipeline is CPU-bound and fast; the Groq call has a ~1-2s timeout; the Chat Mitra send is one HTTP call). If latency becomes an issue, we can move to `BackgroundTasks` — but for this scale (single user's WhatsApp), synchronous is simpler and sufficient.

**Webhook payload structure** (confirmed from [Chat Mitra's webhook documentation](https://chatmitra.com/documentation/whatsapp-business-api/webhooks/)):
```json
{
  "event": "message.received",
  "message_id": "wamid_abc123",
  "direction": "inbound",
  "from": "919876543210",
  "to": "919888888888",
  "timestamp": 1705329000,
  "message": {
    "type": "text",
    "text": "how much for 10ml sauvage"
  }
}
```

Unlike the old AiSensy integration, this is one confirmed flat shape, not a multi-format guess — `app/main.py`'s `_extract_message_data` parses it directly instead of defensively trying several possible structures.

---

### Deployment

#### [NEW] [render.yaml](file:///c:/Users/ABC/Downloads/decanter/render.yaml)

```yaml
services:
  - type: web
    name: decanter-bot
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: CHATMITRA_API_TOKEN
        sync: false
      - key: GROQ_API_KEY
        sync: false
      - key: CHATMITRA_WEBHOOK_SECRET
        sync: false
```

(The deployed `render.yaml` also lists the Supabase dashboard/analytics env vars — see `SUPABASE_SETUP.md` — omitted here since this section predates that feature.)

#### [NEW] [requirements.txt](file:///c:/Users/ABC/Downloads/decanter/requirements.txt)

```
fastapi==0.115.0
uvicorn==0.30.6
httpx==0.27.2
rapidfuzz==3.9.7
openai==1.47.0
pydantic-settings==2.5.2
pytest==8.3.3
pytest-asyncio==0.24.0
```

#### [.env.example]

```env
# Chat Mitra API bearer token (Settings -> API Keys). See CHATMITRA_SETUP.md.
CHATMITRA_API_TOKEN=your_chatmitra_api_token_here

# Chat Mitra webhook signing secret (shown once when the webhook is created)
CHATMITRA_WEBHOOK_SECRET=

# Groq API key (from console.groq.com)
GROQ_API_KEY=your_groq_api_key_here

# Fuzzy match threshold (0-100, default 80)
FUZZY_THRESHOLD=80

# Max message length to process (default 500)
MAX_MESSAGE_LENGTH=500

# Message dedup TTL in seconds (default 300)
DEDUP_TTL_SECONDS=300
```

#### [NEW] [.gitignore](file:///c:/Users/ABC/Downloads/decanter/.gitignore)

Standard Python gitignore + `.env`

---

### Tests

#### [NEW] [test_matcher.py](file:///c:/Users/ABC/Downloads/decanter/tests/test_matcher.py)

~20+ test cases covering:
- Exact keyword match (`"sauvage"` → `dior_sauvage`)
- Substring match (`"how much for sauvage"` → `dior_sauvage`)
- Common misspelling — fuzzy match (`"savage"`, `"savuage"`, `"sawage"`)
- Abbreviation (`"bdc"` → `bleu_de_chanel`)
- Brand-only mention (`"dior"` if configured as a keyword)
- Multiple perfumes in one message → ambiguous
- Completely unrelated message (`"hello"`, `"are you open?"`) → no match
- Empty message → no match
- Very long gibberish → no match
- Case insensitivity (`"SAUVAGE"`, `"Bleu De Chanel"`)

#### [NEW] [test_formatter.py](file:///c:/Users/ABC/Downloads/decanter/tests/test_formatter.py)

- Verifies card output matches expected format (bold name, 2-column layout, comma-formatted prices, shipping card appended).

#### [NEW] [test_webhook.py](file:///c:/Users/ABC/Downloads/decanter/tests/test_webhook.py)

- Uses FastAPI `TestClient`.
- Mocks the Chat Mitra send-message call.
- Sends simulated webhook payloads and asserts correct replies are constructed.
- Tests dedup (same message ID twice → only one reply).
- Tests non-text message handling.

---

### README

#### [NEW] [README.md](file:///c:/Users/ABC/Downloads/decanter/README.md)

Sections:
1. **Architecture overview** (the diagram from the spec, translated to readable markdown)
2. **How to add a new perfume** (step-by-step, pointing at `catalog.py`)
3. **How to update prices** (edit the `prices` dict in `catalog.py`)
4. **How to run locally** (`pip install -r requirements.txt`, `cp .env.example .env`, fill in keys, `uvicorn app.main:app --reload`)
5. **How to run tests** (`pytest tests/ -v`)
6. **How to simulate a webhook call** (documented `curl` command with sample JSON payload)
7. **Pre-launch checklist** (15-20 items)
8. **Known limitations** (placeholder catalog, fuzzy threshold tuning)

---

## Verification Plan

### Automated Tests
```bash
pytest tests/ -v
```

All tests must pass before any deployment.

### Manual Verification
1. Start the server locally with `uvicorn app.main:app --reload`.
2. Hit `GET /health` — expect `200 {"status": "ok"}`.
3. Send simulated webhook payloads via `curl` for:
   - Exact match (`"sauvage"`) → expect full price card + shipping card.
   - Misspelling (`"savage"`) → expect same price card via fuzzy match.
   - Abbreviation (`"bdc"`) → expect Bleu de Chanel card.
   - Multiple perfumes (`"sauvage vs bleu"`) → expect clarification message.
   - Unrelated message (`"hello"`) → expect fallback message.
   - Non-text message type → expect typing prompt.
   - Duplicate message ID → expect no duplicate reply.
4. Verify logs capture match layer and result for each message.
5. **Before go-live**: owner must test against a real Chat Mitra account (see `CHATMITRA_SETUP.md`) to confirm the live webhook and send-message flow end-to-end — the shapes themselves are already confirmed from documentation, but a live test still catches account-specific config issues (e.g. webhook not subscribed correctly, wrong token).
