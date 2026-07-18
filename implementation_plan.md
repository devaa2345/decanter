# WhatsApp Perfume Decanter Price-Query Bot Backend

A FastAPI backend that receives inbound WhatsApp messages via AiSensy webhooks, classifies them against a perfume catalog using a 3-layer matching pipeline, and replies with a formatted price card.

---

## User Review Required

> [!IMPORTANT]
> **AiSensy API integration is based on best-effort research, not confirmed documentation.** AiSensy does not publish a fully open API spec. The webhook payload shape and session-message endpoint documented below are based on web research and community references. **Before go-live, the business owner MUST confirm:**
> 1. The exact inbound webhook JSON payload by sending a test message and inspecting the received POST body.
> 2. Whether `POST https://api.aisensy.io/v1/messages` with `type: "text"` is the correct endpoint for session replies (vs. the older `backend.aisensy.com/campaign/...` endpoint which is template-only).
> 3. Whether AiSensy provides a webhook signing secret / signature header for verification.

> [!WARNING]
> **Placeholder prices only.** The catalog shipped with this build contains example perfumes with made-up prices. The real catalog (keywords, aliases, confirmed prices for all 7 size tiers) must be supplied by the business owner before launch. Do NOT go live with placeholder prices.

## Open Questions

> [!IMPORTANT]
> **Q1 — Non-text messages (image, voice, sticker, location):** The spec asks us to confirm whether these should get a short auto-reply prompting the user to type, or be silently ignored for the owner to handle. **Defaulting to**: send a short text prompt (`"Please type your question and I'll help with pricing 🙂"`) per the spec's instruction to default to a short text prompt if unconfirmed.

> [!IMPORTANT]
> **Q2 — Full price card vs. single-size reply:** The spec says we may always send the full card (all sizes) even if a specific size is asked about, and to default to "send the full card" if unconfirmed. **Defaulting to**: always send the full price card.

> [!IMPORTANT]
> **Q3 — AiSensy webhook signature verification:** Research suggests AiSensy may or may not provide a signing secret. The code will include an **optional** HMAC verification layer that activates only when `AISENSY_WEBHOOK_SECRET` is set in the environment. If AiSensy doesn't offer this feature, leave that env var unset and the check is skipped.

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
│   ├── aisensy.py            # AiSensy send-message client
│   ├── groq_client.py        # Groq LLM classification client
│   ├── config.py             # Settings loaded from environment variables
│   └── dedup.py              # In-memory message-ID deduplication cache with TTL
├── tests/
│   ├── __init__.py
│   ├── test_matcher.py       # Unit tests for the matching pipeline
│   ├── test_formatter.py     # Unit tests for reply formatting
│   └── test_webhook.py       # Integration tests for the webhook endpoint (mocked AiSensy)
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

### AiSensy integration

#### [NEW] [aisensy.py](file:///c:/Users/ABC/Downloads/decanter/app/aisensy.py)

- `send_reply(to: str, message_text: str) → bool`
- Calls `POST https://api.aisensy.io/v1/messages` with:
  ```json
  {
    "to": "<phone_number>",
    "type": "text",
    "text": { "body": "<message_text>" }
  }
  ```
- Headers: `Authorization: Bearer <AISENSY_API_KEY>`, `Content-Type: application/json`.
- Returns `True` on success, logs full error details on failure.
- Uses `httpx.AsyncClient` with a 10-second timeout.

> [!WARNING]
> This endpoint/payload is based on research, not confirmed AiSensy docs. It must be verified before go-live. The code will be structured so swapping the endpoint or payload shape is a single-file change.

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
- `AISENSY_API_KEY` (required)
- `AISENSY_WEBHOOK_SECRET` (optional — enables HMAC verification if set)
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

1. **Verify signature** (if `AISENSY_WEBHOOK_SECRET` is set): check HMAC header, reject 403 if invalid.
2. **Parse payload**: extract `message_id`, `from` (sender phone), `message_type`, `message_text`. Use defensive parsing — if any required field is missing, log and return 200 (don't retry).
3. **Sanity checks**: reject if message too long (`MAX_MESSAGE_LENGTH`), if message type is not text → send `NON_TEXT_MESSAGE` reply.
4. **Dedup**: if `is_duplicate(message_id)` → return 200 silently.
5. **Match**: call `match_perfume(message_text)` (the 3-layer pipeline).
6. **Build reply**:
   - If `ambiguous` → send `AMBIGUOUS_MESSAGE`.
   - If `perfume_id` is `None` → send `FALLBACK_MESSAGE`.
   - Otherwise → send `build_price_card(perfume_id)`.
7. **Send reply** via `aisensy.send_reply(from, reply_text)`.
8. **Log**: message text, match result (perfume_id / layer / confidence / ambiguous), success/failure of send. Use Python `logging` to stdout (Render captures stdout logs).
9. **Return 200** immediately (AiSensy expects a fast ack).

Processing is synchronous in the FastAPI async handler (the matching pipeline is CPU-bound and fast; the Groq call has a ~1-2s timeout; the AiSensy send is one HTTP call). If latency becomes an issue, we can move to `BackgroundTasks` — but for this scale (single user's WhatsApp), synchronous is simpler and sufficient.

**Webhook payload structure** (best-effort based on research — must be verified):
```json
{
  "id": "wamid.xxxxx",
  "timestamp": "1688000000",
  "from": "919876543210",
  "message": {
    "type": "text",
    "text": {
      "body": "how much for 10ml sauvage"
    }
  }
}
```

The parser will be written defensively with multiple fallback field paths to handle potential payload variations.

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
      - key: AISENSY_API_KEY
        sync: false
      - key: GROQ_API_KEY
        sync: false
      - key: AISENSY_WEBHOOK_SECRET
        sync: false
```

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

#### [NEW] [.env.example](file:///c:/Users/ABC/Downloads/decanter/.env.example)

```env
# AiSensy API key (from Manage > API Key in AiSensy dashboard)
AISENSY_API_KEY=your_aisensy_api_key_here

# Optional: AiSensy webhook signing secret (leave empty if not provided by AiSensy)
AISENSY_WEBHOOK_SECRET=

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
- Mocks AiSensy send-message call.
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
8. **Known limitations** (AiSensy API assumptions, placeholder catalog, fuzzy threshold tuning)

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
5. **Before go-live**: owner must test against real AiSensy account to confirm webhook payload shape and send-message API work.
