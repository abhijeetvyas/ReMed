"""AI layer — exactly two call types (deep dive §4). The AI only PROPOSES:
it returns JSON contracts; ordinary code validates, and nothing reaches live
data without the user's explicit Apply.
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


class AIError(Exception):
    pass


class AIClient:
    """Provider-agnostic client for the single required capability:
    chat completion with optional vision input, JSON out."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        self.base_url = (base_url or settings.ai_base_url).rstrip("/")
        self.api_key = api_key or settings.ai_api_key
        self.model = model or settings.ai_model

    async def _complete_json(self, content, prompt: str, max_tokens: int = 1600) -> dict:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            resp = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            if resp.status_code >= 400:
                raise AIError(f"AI provider HTTP {resp.status_code}")
            text = resp.json()["choices"][0]["message"]["content"]
        return _parse_json_object(text)


def _parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise AIError("no JSON object in model output")
    return json.loads(text[start:end + 1])


# ---------------------------------------------------------------- ① Extract

async def extract_prescription(image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    """Image → medicines JSON. Raises AIError/ValidationError on failure."""
    if not settings.ai_configured:
        raise AIError("AI provider not configured")
    client = AIClient()
    b64 = base64.b64encode(image_bytes).decode()
    content = [
        {"type": "text", "text": EXTRACT_PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]
    return await client._complete_json(content, EXTRACT_PROMPT)


# ---------------------------------------------------------------- ② Interpret edit

async def interpret_edit(user_text: str, current_context: dict) -> dict:
    """Free text + current structured state → change-set JSON (or clarify)."""
    if not settings.ai_configured:
        raise AIError("AI provider not configured")
    client = AIClient()
    prompt = interpret_prompt() + _edit_context_block(current_context) + "\n\nUSER REQUEST:\n" + user_text
    return await client._complete_json(prompt, prompt)
