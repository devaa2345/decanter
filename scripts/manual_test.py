"""
Manual test console — try messages against the bot's REAL decision logic
(app.main.webhook_handler) without spending a single Chat Mitra credit,
without a real Groq API call, and without touching Supabase.

Runs in-process via FastAPI's TestClient (see scripts/_manual_test_core.py's
Harness), so this exercises the exact same code path production traffic
takes (dedup, first-contact welcome, order-confirmation short-circuit,
length cutoff, Groq/exact/fuzzy matching, greeting fallback, formatter
output) — not a re-implemented copy of the logic that could drift from
what's actually live.

Prefer a browser instead of a terminal? scripts/manual_test_web.py serves
the same thing as a WhatsApp-style chat page.

Groq is OFF (mocked "unreachable", same as a real Groq outage) by default,
so match_perfume falls through to the free deterministic exact/fuzzy layer
— the same thing app.main's tests do for exactly this reason. Use
":groq on" to make real Groq calls if you specifically want to see Groq's
own classification/phrasing (uses GROQ_API_KEY from .env; costs a fraction
of a cent per message — your call, not the default).

Also simulates the human-handoff pause (see app/handoff.py): ":owner <text>"
pretends YOU (the owner) just messaged the current sender directly on
WhatsApp, which pauses the bot for that sender — and ":advance <hours>"
simulates that many hours passing, without waiting for real time, so you
can confirm the bot picks back up again once the window elapses.

Run:
    python scripts/manual_test.py

Commands (start a line with ":"):
    :sender <number>   switch to a different fake sender (default 919876543210)
    :new               forget the current sender - their next message looks like first contact again
    :senders           list every sender seen so far this session
    :nontext <type>    simulate one non-text message (e.g. ":nontext image")
    :groq on|off       toggle real Groq calls (default: off)
    :owner <text>      simulate the owner personally messaging the current sender directly - pauses the bot for them
    :advance <hours>   fast-forward time by <hours> for the current sender's pause
    :pausehours <n>    set the configured pause duration used by future :owner messages (default 24)
    :status            show the current sender's pause status
    :help              show this list
    :quit / :exit      quit
Anything else is sent as a normal inbound text message from the current sender.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Windows consoles default stdout to the system codepage (cp1252), which
# can't encode the emoji in WELCOME_MESSAGE/etc. — force UTF-8 so replies
# print exactly as WhatsApp would show them instead of crashing on print().
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from scripts._manual_test_core import DEFAULT_SENDER, Harness  # noqa: E402


def _print_result(result: dict) -> None:
    reply = result.get("reply_text")
    print()
    if reply is None:
        print("(silent - no reply sent, no credit spent)")
    else:
        print("-" * 60)
        print(reply)
        print("-" * 60)

    layer = result.get("layer")
    if layer:
        extras = [f"layer={layer}"]
        if result.get("perfume_id"):
            extras.append(f"perfume_id={result['perfume_id']}")
        if result.get("confidence") is not None:
            extras.append(f"confidence={result['confidence']}")
        if result.get("ambiguous"):
            extras.append("ambiguous=True")
        print(f"[{'  '.join(extras)}]")
    print()


def _run(h: Harness) -> None:
    sender = DEFAULT_SENDER

    print(__doc__)
    print(f"Current sender: {sender} (first contact - next message gets the welcome)")
    print("Groq: OFF (mocked unreachable) — type :groq on for real Groq calls\n")

    while True:
        try:
            line = input(f"[{sender}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue

        if line.startswith(":"):
            parts = line[1:].split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print(__doc__)
            elif cmd == "sender":
                if not arg:
                    print("Usage: :sender <number>")
                else:
                    sender = arg
                    status = "first contact" if not h.is_known(sender) else "known sender"
                    print(f"Switched to sender {sender} ({status})")
            elif cmd == "new":
                h.reset_sender(sender)
                print(f"{sender} is now first-contact again")
            elif cmd == "senders":
                if not h.seen_senders:
                    print("(none seen yet)")
                for s in sorted(h.seen_senders):
                    print(f"  {s}{' <- current' if s == sender else ''}")
            elif cmd == "groq":
                if arg == "on":
                    h.groq_enabled = True
                    print("Groq: ON (real API calls)")
                elif arg == "off":
                    h.groq_enabled = False
                    print("Groq: OFF (mocked unreachable)")
                else:
                    print("Usage: :groq on|off")
            elif cmd == "nontext":
                _print_result(h.send(sender, "", message_type=arg or "image"))
            elif cmd == "owner":
                if not arg:
                    print("Usage: :owner <text>")
                else:
                    h.simulate_owner_message(sender, arg)
                    print(f"(simulated) Owner -> {sender}: {arg}")
                    print(f"Bot status for {sender}: {h.pause_status(sender)}")
            elif cmd == "advance":
                try:
                    hours = float(arg)
                except ValueError:
                    print("Usage: :advance <hours>")
                else:
                    if h.fast_forward_pause(sender, hours):
                        print(f"Advanced {hours}h. Bot status for {sender}: {h.pause_status(sender)}")
                    else:
                        print(f"{sender} isn't currently paused - nothing to advance")
            elif cmd == "pausehours":
                try:
                    h.pause_hours = float(arg)
                except ValueError:
                    print("Usage: :pausehours <n>")
                else:
                    print(f"Future :owner messages will pause senders for {h.pause_hours}h")
            elif cmd == "status":
                print(f"Bot status for {sender}: {h.pause_status(sender)}")
            else:
                print(f"Unknown command: {cmd!r} (:help for the list)")
            continue

        _print_result(h.send(sender, line))

    print("Bye!")


if __name__ == "__main__":
    with Harness() as h:
        _run(h)
