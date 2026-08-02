"""
Shared harness for the local, credit-free manual testing tools —
scripts/manual_test.py (terminal REPL) and scripts/manual_test_web.py
(browser chat UI). Both drive the SAME real app.main.webhook_handler
in-process via FastAPI's TestClient, with only the network/DB boundaries
stubbed out:

  - app.chatmitra.send_reply         -> captured locally, never actually sent
  - app.analytics.log_message_event  -> captured locally, never written to Supabase
  - app.analytics.has_been_welcomed  -> backed by an in-memory set, never
                                         queries real Supabase
  - app.handoff.is_paused/start_pause -> backed by an in-memory dict, never
                                         queries real Supabase (see
                                         simulate_owner_message/fast_forward_pause)

app.handoff.record_own_send/was_sent_by_bot are NOT stubbed — they're
already pure in-memory (no Supabase involved), so the harness uses the
real echo-detection logic exactly as production does.

Keeping this in one place means the two front-ends can't drift from each
other or from the actual production decision logic.

IMPORTANT: Harness patches app.main's module-level names for as long as it's
open. That's safe here because these tools always run as their OWN separate
process, never inside the actual deployed server — patching app.main.send_reply
globally would silently stop real customer replies if it ever ran in the same
process as live Chat Mitra webhook traffic.
"""

import logging
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

# Quiet app.main's own INFO-level inbound/outbound logging (raw log blocks
# that would duplicate whatever a caller here prints/renders instead) —
# must happen BEFORE importing app.main, since its module-level
# logging.basicConfig() call is a no-op once the root logger already has a
# handler.
logging.basicConfig(level=logging.WARNING)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app import conversation, groq_client, handoff  # noqa: E402
from app.analytics import WELCOME_LAYER  # noqa: E402

DEFAULT_SENDER = "919876543210"

# The business's own WhatsApp number, used as "from" when simulating the
# owner personally messaging a customer directly (see
# Harness.simulate_owner_message) — matches Chat Mitra's own docs example.
OWNER_NUMBER = "919888888888"


class Harness:
    """
    One instance = one sandboxed testing session. Tracks which senders have
    been "seen" so far (to simulate first-contact status) and whether real
    Groq calls are enabled, and applies every required patch for its
    lifetime via the `with Harness() as h:` context manager.
    """

    def __init__(self):
        self.seen_senders: set[str] = set()
        # Senders who have already been sent the long welcome. Separate from
        # seen_senders because the two are genuinely different questions
        # now: the welcome goes out on a customer's first bare greeting,
        # which may not be the first message they ever sent.
        self.welcomed_senders: set[str] = set()
        self.groq_enabled = False
        self.client: TestClient | None = None
        self._captured: dict = {}
        self._real_classify_and_phrase = groq_client.classify_and_phrase
        self._stack = ExitStack()

        # Human-handoff simulation state (see simulate_owner_message /
        # fast_forward_pause) — sender -> paused_until, in-memory only.
        self.pauses: dict[str, datetime] = {}
        self.pause_hours = handoff.DEFAULT_PAUSE_HOURS

    def __enter__(self) -> "Harness":
        s = self._stack
        s.enter_context(patch.object(main.settings, "CHATMITRA_WEBHOOK_SECRET", ""))
        s.enter_context(
            patch("app.main.send_reply", new_callable=AsyncMock, side_effect=self._fake_send_reply)
        )
        s.enter_context(
            patch(
                "app.main.log_message_event",
                new_callable=AsyncMock,
                side_effect=self._fake_log_message_event,
            )
        )
        s.enter_context(
            patch(
                "app.main.has_been_welcomed",
                new_callable=AsyncMock,
                side_effect=self._fake_has_been_welcomed,
            )
        )
        s.enter_context(
            patch(
                "app.groq_client.classify_and_phrase",
                new_callable=AsyncMock,
                side_effect=self._groq_gate,
            )
        )
        s.enter_context(
            patch("app.main.is_paused", new_callable=AsyncMock, side_effect=self._fake_is_paused)
        )
        s.enter_context(
            patch("app.main.start_pause", new_callable=AsyncMock, side_effect=self._fake_start_pause)
        )
        self.client = TestClient(main.app)
        return self

    def __exit__(self, *exc_info) -> None:
        self._stack.close()

    async def _fake_has_been_welcomed(self, sender: str) -> bool:
        return sender in self.welcomed_senders

    async def _fake_send_reply(self, to: str, message_text: str) -> bool:
        self._captured["reply_text"] = message_text
        return True

    async def _fake_log_message_event(self, **kwargs) -> None:
        self._captured.update(kwargs)

    async def _groq_gate(self, message, candidates, history=None):
        """Real Groq call when groq_enabled, otherwise simulates an outage
        (None) so match_perfume falls back to the deterministic name index —
        see the module docstring.

        Mirrors classify_and_phrase's full signature, `history` included.
        It has to: app.matcher calls this by keyword, so a stale signature
        raises a TypeError that match_perfume catches as "Groq unreachable",
        and the console would quietly test the fallback path with the Groq
        toggle switched on."""
        if self.groq_enabled:
            return await self._real_classify_and_phrase(
                message, candidates=candidates, history=history
            )
        return None

    async def _fake_is_paused(self, sender: str) -> bool:
        until = self.pauses.get(sender)
        return bool(until and datetime.now(timezone.utc) < until)

    async def _fake_start_pause(self, sender: str) -> None:
        self.pauses[sender] = datetime.now(timezone.utc) + timedelta(hours=self.pause_hours)

    def send(self, sender: str, text: str, message_type: str = "text") -> dict:
        """
        Dispatch one simulated inbound message and return a structured
        result: {reply_text, layer, perfume_id, confidence, ambiguous} (some
        keys absent when unset — reply_text is None when the bot stays
        silent by design, not an error).
        """
        message = {"type": message_type}
        if message_type == "text":
            message["text"] = text

        payload = {
            "event": "message.received",
            "message_id": uuid.uuid4().hex,
            "from": sender,
            "message": message,
        }

        self._captured.clear()
        self.client.post("/webhook", json=payload)
        self.seen_senders.add(sender)
        if self._captured.get("layer") == WELCOME_LAYER:
            self.welcomed_senders.add(sender)
        return dict(self._captured)

    def reset_sender(self, sender: str) -> None:
        """Forget this sender entirely — whether they have been welcomed AND
        the recent conversation, so the next message starts a genuinely
        fresh chat rather than one where the bot still remembers the last
        price card and resolves "and 5ml?" against it."""
        self.seen_senders.discard(sender)
        self.welcomed_senders.discard(sender)
        conversation.clear(sender)

    def is_known(self, sender: str) -> bool:
        return sender in self.seen_senders

    def simulate_owner_message(self, customer_sender: str, text: str) -> None:
        """
        Simulate the owner personally replying to a customer directly
        through Chat Mitra (NOT through this bot) — constructs the same
        "message.sent" webhook payload Chat Mitra sends for that, so this
        exercises the REAL app.main._handle_message_sent logic end to end,
        including the echo-detection heuristic (see app.handoff) that's
        what makes this distinguishable from the bot's own replies.
        """
        payload = {
            "event": "message.sent",
            "message_id": uuid.uuid4().hex,
            "direction": "outbound",
            "from": OWNER_NUMBER,
            "to": customer_sender,
            "message": {"type": "text", "text": text},
        }
        self.client.post("/webhook", json=payload)

    def fast_forward_pause(self, sender: str, hours: float) -> bool:
        """
        Testing helper: simulate `hours` having passed for this sender's
        human-handoff pause, without waiting for real time to pass — the
        thing the local instance can do that a live webhook test can't.
        Returns False if this sender isn't currently paused at all.
        """
        if sender not in self.pauses:
            return False
        self.pauses[sender] -= timedelta(hours=hours)
        return True

    def pause_status(self, sender: str) -> str:
        until = self.pauses.get(sender)
        if not until:
            return "not paused"
        remaining_hours = (until - datetime.now(timezone.utc)).total_seconds() / 3600
        if remaining_hours <= 0:
            return "pause expired - bot is active again"
        return f"paused for {remaining_hours:.1f} more hours"
