"""
Chat Mitra send-message client.

Sends session replies (free-form text within the active 24-hour conversation
window — see https://chatmitra.com/documentation/whatsapp-business-api/) to
customers via the Chat Mitra API. We only ever reply to a message we just
received, so that window is always open and a "raw" text message is always
the right message kind — no templates needed.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

CHATMITRA_BASE_URL = "https://backend.chatmitra.com/developer/api"
CHATMITRA_SEND_URL = f"{CHATMITRA_BASE_URL}/send_message"


async def send_reply(to: str, message_text: str) -> bool:
    """
    Send a text reply to a customer via Chat Mitra.

    Args:
        to: Customer's phone number, country code included, no "+" (e.g. "919876543210")
        message_text: The reply text to send

    Returns:
        True if the message was accepted (any 2xx — Chat Mitra returns 202
        with the request queued, not 200), False otherwise.
    """
    if not settings.CHATMITRA_API_TOKEN:
        logger.error("CHATMITRA_API_TOKEN not set — cannot send reply")
        return False

    payload = {
        "recipient_mobile_number": to,
        "messages": [
            {
                "kind": "raw",
                "payload": {
                    "type": "text",
                    "text": {"body": message_text},
                },
            }
        ],
    }

    headers = {
        "Authorization": f"Bearer {settings.CHATMITRA_API_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                CHATMITRA_SEND_URL,
                json=payload,
                headers=headers,
            )

            if response.is_success:
                job_id = None
                try:
                    job_id = response.json().get("jobId")
                except Exception:
                    pass
                logger.info(
                    "Reply sent successfully to %s (status=%d, jobId=%s)",
                    to,
                    response.status_code,
                    job_id,
                )
                return True
            else:
                logger.error(
                    "Chat Mitra send failed: status=%d, body=%s",
                    response.status_code,
                    response.text[:500],
                )
                return False

    except httpx.TimeoutException:
        logger.error("Chat Mitra send timed out for %s", to)
        return False
    except Exception:
        logger.exception("Chat Mitra send failed for %s", to)
        return False
