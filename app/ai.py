"""AI layer — three call types (deep dive §4): photo extract, typed-text
extract, edit interpret. The AI only PROPOSES: it returns JSON contracts;
ordinary code validates, and nothing reaches live data without the user's
explicit Apply.
"""
import base64
import json
import logging
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger("remed.ai")

# Prompts live one file each under app/prompts/ so they can be edited without
# touching code. The JSON schema block in extract.md is kept in sync with
# MEDICINE_SCHEMA_EXAMPLE (used by tests).
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

MEDICINE_SCHEMA_EXAMPLE = {
    "medicines": [
        {"name": "Azithromycin 500", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["breakfast", "dinner"], "food": "after", "days": 5}
    ],
    "notes": []
}


def load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


EXTRACT_PROMPT = load_prompt("extract.md")


def _edit_context_block(ctx: dict) -> str:
    return json.dumps(ctx, indent=1)


def interpret_prompt() -> str:
    return load_prompt("interpret_edit.md")


def type_extract_prompt() -> str:
    return load_prompt("type_extract.md")


class AIError(Exception):
    pass


class AIClient:
    """Provider-agnostic client for the single required capability:
    chat completion with optional vision input, JSON out.

    Model fallback (founder): settings.ai_models is an ordered chain because
    each model has its own rate limit — a 429 (or 5xx/timeout) on model N
    transparently retries model N+1. The caller is told which model answered,
    so ai_calls rows record reality, not config.

    Reliability contract: ANY per-model failure that is about the model
    (unavailable, rate-limited, truncated/garbled JSON, unexpected payload
    shape) falls through to the next model. Only a hard request failure (bad
    key or malformed request — needs a human) aborts immediately. JSONDecodeError
    escaping this module would bypass every handler's `except AIError` and
    leave the user in silence (review E2)."""

    # 429/5xx = rate-limited or provider-side trouble; 404 = model name unknown
    # (typo, renamed, or not yet live) — all fall through to the next model.
    # Other 4xx (bad key, malformed request) fail hard: fixing them needs a human.
    FALLBACK_ON = (404, 429, 500, 502, 503, 504)

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 models: list[str] | None = None):
        self.base_url = (base_url or settings.ai_base_url).rstrip("/")
        self.api_key = api_key or settings.ai_api_key
        self.models = models or settings.ai_models

    async def complete_json_chain(self, content, prompt: str, max_tokens: int = 1600) -> tuple[dict, str]:
        """Try each model in the chain; return (json, model_used). Raises
        AIError only if every model fails (now including unparseable output)."""
        body_base: dict = {
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        last_err: Exception | None = None
        async with httpx.AsyncClient(timeout=90) as client:
            for model in self.models:
                try:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=body_base | {"model": model},
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    log.warning("model %s network failure (%s) — falling back", model, type(e).__name__)
                    last_err = e
                    continue
                if resp.status_code in self.FALLBACK_ON:
                    log.warning("model %s unavailable (HTTP %s) — falling back", model, resp.status_code)
                    last_err = AIError(f"{model}: HTTP {resp.status_code}")
                    continue
                if resp.status_code >= 400:
                    raise AIError(f"{model}: HTTP {resp.status_code}")
                try:
                    text = resp.json()["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError) as e:
                    log.warning("model %s returned unusable response shape (%s) — falling back",
                                model, type(e).__name__)
                    last_err = e
                    continue
                try:
                    return _parse_json_object(text), model  # parse failure = this model's problem
                except AIError as e:
                    log.warning("model %s returned unparseable JSON (%s) — falling back", model, e)
                    last_err = e
                    continue
        raise AIError(f"all models failed: {last_err}")

    # kept for back-compat; prefer complete_json_chain
    async def _complete_json(self, content, prompt: str, max_tokens: int = 1600) -> dict:
        obj, _ = await self.complete_json_chain(content, prompt, max_tokens)
        return obj


def _parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise AIError("no JSON object in model output")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        # braces present, contents malformed (truncation mid-token, stray text
        # inside) — must surface as AIError so the chain falls through instead
        # of escaping every handler's `except AIError` (review E2)
        raise AIError(f"malformed JSON in model output: {e}") from e


# ---------------------------------------------------------------- ① Extract

async def extract_prescription(image_bytes: bytes, mime: str = "image/jpeg") -> tuple[dict, str]:
    """Image → (medicines JSON, model_used). Raises AIError if the whole chain fails."""
    if not settings.ai_configured:
        raise AIError("AI provider not configured")
    client = AIClient()
    b64 = base64.b64encode(image_bytes).decode()
    content = [
        {"type": "text", "text": EXTRACT_PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]
    return await client.complete_json_chain(content, EXTRACT_PROMPT)


# ---------------------------------------------------------------- ② Interpret edit

async def interpret_edit(user_text: str, current_context: dict) -> tuple[dict, str]:
    """Free text + current structured state → (change-set JSON, model_used)."""
    if not settings.ai_configured:
        raise AIError("AI provider not configured")
    client = AIClient()
    prompt = interpret_prompt() + _edit_context_block(current_context) + "\n\nUSER REQUEST:\n" + user_text
    return await client.complete_json_chain(prompt, prompt)


# ---------------------------------------------------------------- ③ Extract from typed text

async def extract_text_prescription(user_text: str) -> tuple[dict, str]:
    """Typed prescription description → (medicines JSON, model_used). Same
    JSON contract as extract_prescription (the photo path); only the input
    differs. Raises AIError if the whole chain fails."""
    if not settings.ai_configured:
        raise AIError("AI provider not configured")
    client = AIClient()
    prompt = type_extract_prompt() + user_text
    return await client.complete_json_chain(prompt, prompt)
