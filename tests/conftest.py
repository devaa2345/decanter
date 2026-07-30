"""
Shared test configuration.

The one thing every test in this suite needs: no network. app.config reads
a local .env, so the moment a real GROQ_API_KEY is present there — which it
is, for anyone actually developing against the live model — tests that do
not explicitly mock Groq start making real API calls. That is slow (the
suite went from 2 seconds to 2 minutes), costs money, depends on a rate
limit, and makes results depend on what a model felt like saying.

Blanking the key is the right lever rather than mocking the client: it is
the same switch production uses when Groq is not configured, so the tests
exercise a real supported code path (app.groq_client.classify_and_phrase
returns None, app.matcher falls back to the deterministic index) instead of
a fictional one. Tests that want Groq behaviour patch classify_and_phrase
directly, which bypasses this entirely.

Live model behaviour is measured by scripts/benchmark_llm.py, which is not
part of this suite precisely because it needs a key and a network.
"""

import pytest

from app.config import settings


@pytest.fixture(autouse=True, scope="session")
def no_live_groq():
    original = settings.GROQ_API_KEY
    settings.GROQ_API_KEY = ""
    yield
    settings.GROQ_API_KEY = original
