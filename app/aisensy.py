"""
AiSensy send-message client.

Sends session replies (free-form text within the 24-hour window)
to customers via the AiSensy API.

NOTE: The endpoint and payload format below are based on research,
not confirmed AiSensy documentation. Verify before go-live.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# AiSensy session message endpoint (to be confirmed with AiSensy support)
AISENSY_SEND_URL = "https://backend.aisensy.com/campaign/t1/api/v2"


async def send_reply(to: str, message_text: str) -> bool:
    """
    Send a text reply to a customer via AiSensy.

    Args:
        to: Customer's phone number (with country code, e.g. "919876543210")
        message_text: The reply text to send

    Returns:
        True if the message was sent successfully, False otherwise.
    """
    if not settings.AISENSY_API_KEY:
        logger.error("AISENSY_API_KEY not set — cannot send reply")
        return False

    # Payload for AiSensy campaign/session API
    # This uses the campaign API endpoint with a pre-configured
    # "session reply" campaign. The business owner needs to:
    # 1. Create an API campaign in AiSensy dashboard for session replies
    # 2. Set the campaign name in the payload below
    #
    # Alternative: If AiSensy supports direct session text via
    # api.aisensy.io/v1/messages, swap to that endpoint instead.
    payload = {
        "apiKey": settings.AISENSY_API_KEY,
        "campaignName": "price_bot_reply",  # Must match the campaign name in AiSensy dashboard
        "destination": to,
        "userName": "Customer",
        "templateParams": [message_text],
    }

    headers = {
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                AISENSY_SEND_URL,
                json=payload,
                headers=headers,
            )

            if response.status_code == 200:
                logger.info(
                    "Reply sent successfully to %s (status=%d)",
                    to,
                    response.status_code,
                )
                return True
            else:
                logger.error(
                    "AiSensy send failed: status=%d, body=%s",
                    response.status_code,
                    response.text[:500],
                )
                return False

    except httpx.TimeoutException:
        logger.error("AiSensy send timed out for %s", to)
        return False
    except Exception:
        logger.exception("AiSensy send failed for %s", to)
        return False
