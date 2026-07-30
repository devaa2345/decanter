"""
Manual test console — browser edition. A WhatsApp-style chat page for
trying messages against the bot's REAL decision logic
(app.main.webhook_handler) without spending a single Chat Mitra credit,
without a real Groq API call, and without touching Supabase.

Prefer a terminal? scripts/manual_test.py does the same thing as a REPL —
both share scripts/_manual_test_core.py's Harness, so they can't drift from
each other or from the real webhook handler.

This is its OWN separate local server (never the actual deployed app) —
see _manual_test_core's module docstring for why that separation matters.
Binds to 127.0.0.1 only, never 0.0.0.0 — this is a local dev tool, not
something to expose on a network.

Run:
    python scripts/manual_test_web.py
Then open http://127.0.0.1:8765 in a browser.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from scripts._manual_test_core import DEFAULT_SENDER, Harness  # noqa: E402

HOST = "127.0.0.1"
PORT = 8765

app = FastAPI(title="Sovereign Scents — Manual Test Console")
harness = Harness()


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE.replace("__DEFAULT_SENDER__", DEFAULT_SENDER)


@app.post("/api/send")
async def api_send(request: Request):
    payload = await request.json()
    sender = (payload.get("sender") or DEFAULT_SENDER).strip() or DEFAULT_SENDER
    text = payload.get("text") or ""
    message_type = payload.get("message_type") or "text"

    result = harness.send(sender, text, message_type=message_type)
    return {
        "reply_text": result.get("reply_text"),
        "layer": result.get("layer"),
        "perfume_id": result.get("perfume_id"),
        "confidence": result.get("confidence"),
        "ambiguous": bool(result.get("ambiguous")),
    }


@app.post("/api/reset")
async def api_reset(request: Request):
    payload = await request.json()
    sender = (payload.get("sender") or DEFAULT_SENDER).strip() or DEFAULT_SENDER
    harness.reset_sender(sender)
    return {"ok": True}


@app.post("/api/groq")
async def api_groq(request: Request):
    payload = await request.json()
    harness.groq_enabled = bool(payload.get("enabled"))
    return {"groq_enabled": harness.groq_enabled}


@app.post("/api/owner-message")
async def api_owner_message(request: Request):
    """Simulate the owner personally messaging this sender directly on
    WhatsApp (not through the bot) — see Harness.simulate_owner_message.
    Pauses the bot for this sender per the currently configured duration."""
    payload = await request.json()
    sender = (payload.get("sender") or DEFAULT_SENDER).strip() or DEFAULT_SENDER
    text = payload.get("text") or ""
    harness.simulate_owner_message(sender, text)
    return {"pause_status": harness.pause_status(sender)}


@app.post("/api/advance")
async def api_advance(request: Request):
    """Fast-forward the current sender's pause clock by N hours, without
    waiting for real time to pass — see Harness.fast_forward_pause."""
    payload = await request.json()
    sender = (payload.get("sender") or DEFAULT_SENDER).strip() or DEFAULT_SENDER
    hours = float(payload.get("hours") or 0)
    advanced = harness.fast_forward_pause(sender, hours)
    return {"advanced": advanced, "pause_status": harness.pause_status(sender)}


@app.get("/api/pause-status")
async def api_pause_status(sender: str = DEFAULT_SENDER):
    return {"pause_status": harness.pause_status(sender)}


@app.post("/api/pause-hours")
async def api_pause_hours(request: Request):
    """Set the pause duration future owner-messages will use (mirrors the
    real Settings dashboard page, but local/in-memory only)."""
    payload = await request.json()
    harness.pause_hours = float(payload.get("hours") or harness.pause_hours)
    return {"pause_hours": harness.pause_hours}


HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sovereign Scents — Manual Test Console</title>
<style>
  :root {
    --wa-header: #075E54;
    --wa-accent: #128C7E;
    --wa-bg: #E5DDD5;
    --wa-bubble-bot: #FFFFFF;
    --wa-bubble-me: #DCF8C6;
    --wa-text: #111B21;
    --wa-meta: #667781;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #0b141a;
    color: var(--wa-text);
    display: flex;
    justify-content: center;
    min-height: 100vh;
  }
  .phone {
    width: 100%;
    max-width: 480px;
    min-height: 100vh;
    background: var(--wa-bg);
    display: flex;
    flex-direction: column;
    box-shadow: 0 0 24px rgba(0,0,0,0.4);
  }
  header {
    background: var(--wa-header);
    color: #fff;
    padding: 14px 16px;
  }
  header h1 { margin: 0; font-size: 16px; font-weight: 600; }
  header p { margin: 2px 0 0; font-size: 12px; opacity: 0.85; }
  .badge {
    display: inline-block;
    margin-top: 6px;
    font-size: 11px;
    background: rgba(255,255,255,0.15);
    padding: 2px 8px;
    border-radius: 10px;
  }
  .controls {
    background: #f0f2f1;
    padding: 8px 10px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    border-bottom: 1px solid #d1d7d6;
    font-size: 12px;
  }
  .controls input[type=text] {
    padding: 5px 8px;
    border: 1px solid #cfd8d7;
    border-radius: 6px;
    font-size: 12px;
    width: 130px;
  }
  .controls button {
    padding: 5px 10px;
    border: none;
    border-radius: 6px;
    background: var(--wa-accent);
    color: #fff;
    font-size: 12px;
    cursor: pointer;
  }
  .controls button.secondary { background: #6b7c7a; }
  .controls button:hover { opacity: 0.9; }
  .toggle-label {
    display: flex;
    align-items: center;
    gap: 4px;
    color: #3b4a48;
  }
  .controls.handoff { border-top: 1px dashed #d1d7d6; }
  .controls input[type=number] {
    padding: 5px 8px;
    border: 1px solid #cfd8d7;
    border-radius: 6px;
    font-size: 12px;
    width: 60px;
  }
  #pauseStatus {
    font-weight: 600;
    color: #3b4a48;
  }
  #pauseStatus.active { color: #b45309; }
  #chat {
    flex: 1;
    overflow-y: auto;
    padding: 14px 10px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .row { display: flex; }
  .row.me { justify-content: flex-end; }
  .row.bot { justify-content: flex-start; }
  .bubble {
    max-width: 78%;
    padding: 7px 10px 6px;
    border-radius: 8px;
    font-size: 14px;
    line-height: 1.35;
    white-space: pre-wrap;
    word-wrap: break-word;
    box-shadow: 0 1px 1px rgba(0,0,0,0.1);
  }
  .bubble.me { background: var(--wa-bubble-me); border-top-right-radius: 2px; }
  .bubble.bot { background: var(--wa-bubble-bot); border-top-left-radius: 2px; }
  .meta {
    font-size: 10.5px;
    color: var(--wa-meta);
    margin: 2px 4px 0;
  }
  .row.bot .meta { text-align: left; }
  .row.me .meta { text-align: right; }
  .system-note {
    align-self: center;
    background: #d9dbd5;
    color: #4a4a4a;
    font-size: 11.5px;
    padding: 4px 10px;
    border-radius: 8px;
    margin: 4px 0;
    text-align: center;
  }
  footer {
    display: flex;
    padding: 8px;
    gap: 8px;
    background: #f0f2f1;
    border-top: 1px solid #d1d7d6;
  }
  footer input[type=text] {
    flex: 1;
    padding: 10px 12px;
    border-radius: 20px;
    border: 1px solid #cfd8d7;
    font-size: 14px;
  }
  footer button {
    background: var(--wa-accent);
    color: #fff;
    border: none;
    border-radius: 20px;
    padding: 0 18px;
    font-size: 14px;
    cursor: pointer;
  }
  footer button:disabled { opacity: 0.5; cursor: default; }
</style>
</head>
<body>
  <div class="phone">
    <header>
      <h1>Sovereign Scents — Manual Test Console</h1>
      <p>Simulating what a customer would see on WhatsApp</p>
      <span class="badge">local only · no Chat Mitra credits · no real Supabase writes</span>
    </header>

    <div class="controls">
      <input id="senderInput" type="text" value="__DEFAULT_SENDER__">
      <button id="switchBtn">Switch</button>
      <button id="newBtn" class="secondary">New sender</button>
      <button id="imageBtn" class="secondary">Send image</button>
      <label class="toggle-label">
        <input id="groqToggle" type="checkbox"> Real Groq calls
      </label>
    </div>

    <div class="controls handoff">
      <span>Bot status: <span id="pauseStatus">not paused</span></span>
      <input id="ownerTextInput" type="text" placeholder="Message as the owner…" style="width:150px">
      <button id="ownerSendBtn" class="secondary">Send as owner</button>
      <input id="advanceHoursInput" type="number" value="24" min="0" step="0.5">
      <button id="advanceBtn" class="secondary">Advance hours</button>
      <input id="pauseHoursInput" type="number" value="24" min="0.1" step="0.5" title="Pause duration used by future owner messages">
      <button id="setPauseHoursBtn" class="secondary">Set duration</button>
    </div>

    <div id="chat"></div>

    <footer>
      <input id="textInput" type="text" placeholder="Type a message, e.g. sauvage 5ml" autocomplete="off">
      <button id="sendBtn">Send</button>
    </footer>
  </div>

<script>
  const chatEl = document.getElementById("chat");
  const senderInput = document.getElementById("senderInput");
  const textInput = document.getElementById("textInput");
  const sendBtn = document.getElementById("sendBtn");

  let currentSender = senderInput.value.trim();
  const conversations = {}; // sender -> array of render calls (dom nodes rebuilt on switch)

  function ensureThread(sender) {
    if (!conversations[sender]) {
      conversations[sender] = [];
    }
    return conversations[sender];
  }

  function renderThread(sender) {
    chatEl.innerHTML = "";
    const thread = ensureThread(sender);
    if (thread.length === 0) {
      appendSystemNote(`${sender} — first contact: their next message gets the welcome`, false);
    } else {
      thread.forEach(item => appendNode(item, false));
    }
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  function appendNode(item, store = true) {
    if (store) ensureThread(currentSender).push(item);
    if (item.kind === "system") {
      const div = document.createElement("div");
      div.className = "system-note";
      div.textContent = item.text;
      chatEl.appendChild(div);
      return;
    }
    const row = document.createElement("div");
    row.className = "row " + item.kind;
    const bubble = document.createElement("div");
    bubble.className = "bubble " + item.kind;
    bubble.textContent = item.text;
    row.appendChild(bubble);
    chatEl.appendChild(row);
    if (item.meta) {
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = item.meta;
      const wrap = document.createElement("div");
      wrap.className = "row " + item.kind;
      wrap.appendChild(meta);
      chatEl.appendChild(wrap);
    }
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  function appendSystemNote(text, store = true) {
    appendNode({ kind: "system", text }, store);
  }

  function metaLine(data) {
    if (!data.layer) return "";
    const parts = [`layer=${data.layer}`];
    if (data.perfume_id) parts.push(`perfume=${data.perfume_id}`);
    if (data.confidence !== null && data.confidence !== undefined) parts.push(`confidence=${data.confidence}`);
    if (data.ambiguous) parts.push("ambiguous");
    return parts.join("  ·  ");
  }

  async function sendMessage(text, messageType = "text") {
    const sender = currentSender;
    if (messageType === "text") {
      appendNode({ kind: "me", text });
    } else {
      appendNode({ kind: "me", text: `📎 (sent a ${messageType} message)` });
    }

    sendBtn.disabled = true;
    try {
      const resp = await fetch("/api/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sender, text, message_type: messageType }),
      });
      const data = await resp.json();
      if (data.reply_text === null || data.reply_text === undefined) {
        appendSystemNote(
          data.layer === "human_handoff_pause"
            ? "🔇 Bot stayed silent — owner is handling this conversation directly"
            : "🔇 Bot stayed silent — no reply sent, no credit spent"
        );
      } else {
        appendNode({ kind: "bot", text: data.reply_text, meta: metaLine(data) });
      }
    } catch (err) {
      appendSystemNote("⚠️ Request failed: " + err);
    } finally {
      sendBtn.disabled = false;
      refreshPauseStatus();
    }
  }

  sendBtn.addEventListener("click", () => {
    const text = textInput.value.trim();
    if (!text) return;
    textInput.value = "";
    sendMessage(text);
  });

  textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendBtn.click();
  });

  document.getElementById("switchBtn").addEventListener("click", () => {
    const next = senderInput.value.trim();
    if (!next) return;
    currentSender = next;
    renderThread(currentSender);
    refreshPauseStatus();
  });

  document.getElementById("newBtn").addEventListener("click", async () => {
    await fetch("/api/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sender: currentSender }),
    });
    conversations[currentSender] = [];
    renderThread(currentSender);
  });

  document.getElementById("imageBtn").addEventListener("click", () => {
    sendMessage("", "image");
  });

  document.getElementById("groqToggle").addEventListener("change", async (e) => {
    await fetch("/api/groq", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: e.target.checked }),
    });
    appendSystemNote(e.target.checked ? "Groq: ON (real API calls)" : "Groq: OFF (mocked unreachable)");
  });

  // --- Human handoff simulation (see app/handoff.py) ------------------------

  const pauseStatusEl = document.getElementById("pauseStatus");

  function applyPauseStatus(status) {
    pauseStatusEl.textContent = status;
    pauseStatusEl.classList.toggle("active", status.startsWith("paused for"));
  }

  async function refreshPauseStatus() {
    const resp = await fetch(`/api/pause-status?sender=${encodeURIComponent(currentSender)}`);
    const data = await resp.json();
    applyPauseStatus(data.pause_status);
  }

  document.getElementById("ownerSendBtn").addEventListener("click", async () => {
    const input = document.getElementById("ownerTextInput");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    appendSystemNote(`👤 (simulated) Owner → ${currentSender}: ${text}`);
    const resp = await fetch("/api/owner-message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sender: currentSender, text }),
    });
    const data = await resp.json();
    applyPauseStatus(data.pause_status);
    appendSystemNote(`Bot status: ${data.pause_status}`);
  });

  document.getElementById("advanceBtn").addEventListener("click", async () => {
    const hours = Number(document.getElementById("advanceHoursInput").value) || 0;
    const resp = await fetch("/api/advance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sender: currentSender, hours }),
    });
    const data = await resp.json();
    if (!data.advanced) {
      appendSystemNote(`${currentSender} isn't currently paused — nothing to advance`);
      return;
    }
    applyPauseStatus(data.pause_status);
    appendSystemNote(`⏩ Advanced ${hours}h — bot status: ${data.pause_status}`);
  });

  document.getElementById("setPauseHoursBtn").addEventListener("click", async () => {
    const hours = Number(document.getElementById("pauseHoursInput").value) || 24;
    const resp = await fetch("/api/pause-hours", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hours }),
    });
    const data = await resp.json();
    appendSystemNote(`Future owner messages will pause senders for ${data.pause_hours}h`);
  });

  renderThread(currentSender);
  refreshPauseStatus();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    with harness:
        print(f"Manual test console running at http://{HOST}:{PORT} (local only — Ctrl+C to stop)")
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
