"""AI layer — exactly two call types (deep dive §4). The AI only PROPOSES:
it returns JSON contracts; ordinary code validates, and nothing reaches live
data without the user's explicit Apply.
"""
import base64
import json
import logging

import httpx

from .config import settings

log = logging.getLogger("remed.ai")

MEDICINE_SCHEMA_EXAMPLE = {
    "medicines": [
        {"name": "Azithromycin 500", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["breakfast", "dinner"], "food": "after", "days": 5}
    ],
    "notes": []
}

EXTRACT_PROMPT = f"""You read photos of doctors' prescriptions and return ONLY a JSON object, no other text.

Extract every oral medicine course you can read. Rules:
- slots: subset of ["breakfast", "lunch", "dinner"] — infer from timing cues (morning, 1-0-1, after meals, etc.). Every medicine needs at least one slot.
- food: "before" or "after" — from phrases like "before food", "1 hour before meals", "after food". Default "after".
- days: duration of that medicine's course in days (e.g. "5 days", "1-0-1 × 7"). If only a quantity is given (e.g. 10 tablets) with 2 doses/day, compute days = quantity / doses_per_day.
- tablets_per_dose: number of tablets per occasion. Use 1 unless the prescription clearly says otherwise (e.g. "2 tabs", "1+0+1" means 1).
- form: tablet | capsule | syrup | other.
- Copy medicine names and strengths exactly as written (including the strength, e.g. "Azithromycin 500").
- Do NOT invent medicines, doses, or durations. Skip anything illegible; if nothing is legible, return {{"medicines": [], "notes": ["unreadable"]}}.
- notes: anything worth flagging (illegible lines, non-oral items like ointments).

JSON schema:
{json.dumps(MEDICINE_SCHEMA_EXAMPLE)}"""


def _edit_context_block(ctx: dict) -> str:
    return json.dumps(ctx, indent=1)


INTERPRET_PROMPT = """You help edit a patient's confirmed prescriptions from their own words. Return ONLY a JSON object, no other text.

You receive the CURRENT state (users settings + prescriptions with medicines) and the user's request in free text. Return an operation list:

{ "operations": [ ... ], "clarify": null }

Operation shapes (only include fields being changed):
- {"op":"update_medicine","target":"<partial medicine name>","fields":{"slots":["breakfast","dinner"],"food":"before","days":5,"tablets_per_dose":2,"name":"Fixed Name"}}
- {"op":"add_medicine","prescription":"<id or a medicine name in the target prescription, or 'new'>","fields":{"name":"Dolo 650","form":"tablet","tablets_per_dose":1,"slots":["breakfast","lunch","dinner"],"food":"after","days":3}}
- {"op":"remove_medicine","target":"<partial medicine name>"}
- {"op":"set_meal_time","target":"breakfast|lunch|dinner","new_value":"20:30"}
- {"op":"set_offset","target":"before|after","new_value":-20}   (minutes; negative = minutes before meal)
- {"op":"set_timezone","new_value":"Asia/Kolkata"}              (IANA name)
- {"op":"cancel_prescription","target":"<a medicine name in it, or prescription id>"}

Rules:
- If the user's request is ambiguous (e.g. two medicines match, or you cannot tell which prescription), set "clarify" to ONE short question and leave operations empty.
- Use the user's wording: "before food" → food before; "actually 5 days" → days 5; "doctor cancelled X" → remove_medicine or cancel_prescription.
- Never invent numbers the user did not give. Change only what they asked.
- "slots" always use the values breakfast/lunch/dinner.
If the request cannot be mapped to operations, return {"operations": [], "clarify": null}.

CURRENT STATE:
"""


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
    prompt = INTERPRET_PROMPT + _edit_context_block(current_context) + "\n\nUSER REQUEST:\n" + user_text
    return await client._complete_json(prompt, INTERPRET_PROMPT)
