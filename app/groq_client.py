"""
Groq LLM classification client.

Uses the OpenAI-compatible SDK pointed at Groq's API to classify which
perfume a customer message refers to, choosing only from a caller-supplied
shortlist of candidates — never the full catalog (see
app.matcher._top_candidates_for_llm). Returns ONLY a perfume_id from that
shortlist, or None. Never generates prices or reply text.
"""

import logging

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)


def _build_system_prompt(candidates: dict[str, dict]) -> str:
    """
    Build the classification prompt scoped to a shortlist of candidate
    perfumes. With 1200+ perfumes, listing the entire catalog runs to
    ~23K tokens — confirmed in production to blow straight through Groq's
    per-minute token limit (6000 TPM), failing every single call. Every
    caller must pass a pre-narrowed shortlist; there is no "full catalog"
    fallback here on purpose, so that bug can't quietly come back.
    """
    perfume_list = "\n".join(
        f"- {pid}: {data['display_name']}" for pid, data in candidates.items()
    )

    return f"""You are a perfume identification assistant for a decant business.
Your ONLY job is to identify which perfume the customer is asking about.

Here is the list of perfume IDs and their display names to choose from:
{perfume_list}

RULES:
1. Return ONLY the perfume_id (e.g. "afnan9pm_rebel") that best matches what the customer is asking about.
2. If the customer's message does not clearly refer to any perfume in the list, return exactly the word: none
3. Do NOT return any explanation, price, greeting, or extra text — just the ID or "none".
4. Match based on the perfume name, brand, common abbreviations, or misspellings.
5. If you are not confident, return "none" — a wrong match is worse than no match.
"""


async def classify_perfume(message: str, candidates: dict[str, dict]) -> str | None:
    """
    Ask the LLM to classify which perfume the message refers to, choosing
    only from the given candidate shortlist.

    Returns a perfume_id string if confidently matched, or None.
    Never raises — all exceptions are caught and logged.
    """
    if not settings.GROQ_API_KEY:
        logger.warning("GROQ_API_KEY not set — skipping LLM classification")
        return None

    if not candidates:
        return None

    try:
        client = AsyncOpenAI(
            api_key=settings.GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )

        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            temperature=0,
            max_tokens=100,
            messages=[
                {"role": "system", "content": _build_system_prompt(candidates)},
                {"role": "user", "content": message},
            ],
        )

        result = response.choices[0].message.content
        if result:
            result = result.strip().strip('"').strip("'").lower()

        logger.info("Groq LLM response: %s", result)

        # Validate: must be one of the offered candidates or "none"
        if result and result != "none" and result in candidates:
            return result

        return None

    except Exception:
        logger.exception("Groq API call failed")
        return None
