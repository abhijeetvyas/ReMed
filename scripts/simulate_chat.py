"""Generate chat-simulation.html — a transcript of a real user's chat with the
bot, using the REAL handlers/rendering/engine code paths (only the AI is
scripted, and photos are replaced by a placeholder). Every bot message in the
artifact is byte-for-byte what the code produces.

Run:  .venv/bin/python scripts/simulate_chat.py
"""
import asyncio
import html
import json
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import changesets, db, engine, handlers, rendering, scheduling  # noqa: E402
from app.telegram import extract_event  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 1, 20, 1, 0, tzinfo=timezone.utc)  # 06:30 IST, day 0
CHAT = "424242"

# --- deterministic clock -----------------------------------------------------
handlers._utcnow = lambda: NOW


class _Date(date):
    @classmethod
    def today(cls):
        return date(2026, 1, 20)


changesets.date = _Date


class _DT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


rendering.datetime = _DT

# --- transcript recorder (stands in for Telegram) ----------------------------
ENTRIES: list[dict] = []
_mid = [1000]
_upd = [1]


def header(title, note=""):
    ENTRIES.append({"side": "header", "text": title, "note": note})


class Rec:
    async def send_text(self, chat_id, text):
        ENTRIES.append({"side": "bot", "text": text})
        return _mid[0]

    async def send_message_with_buttons(self, chat_id, text, buttons):
        mid = _mid[0]
        _mid[0] += 1
        ENTRIES.append({
            "side": "bot", "text": text, "mid": mid,
            "labels": [[b["text"] for b in row] for row in buttons],
            "cb": [[b["callback_data"] for b in row] for row in buttons],
        })
        return mid

    async def update_message_buttons(self, chat_id, message_id, buttons):
        if not buttons:
            ENTRIES.append({"side": "note",
                            "text": "buttons on that reminder quietly clear — no reply message"})

    async def answer_callback(self, cb_id, text=""):
        pass


rec = Rec()


async def user_text(text):
    ENTRIES.append({"side": "user", "text": text})
    await handlers.handle_event(rec, extract_event(
        {"update_id": _upd[0], "message": {"chat": {"id": CHAT}, "text": text}}))
    _upd[0] += 1


async def user_photo():
    ENTRIES.append({"side": "user", "text": "📷 [prescription photo attached]"})
    await handlers.handle_event(rec, extract_event(
        {"update_id": _upd[0], "message": {"chat": {"id": CHAT},
                                           "photo": [{"file_id": "x", "width": 720, "height": 1280}]}}))
    _upd[0] += 1


async def tap(label=None):
    """Tap the FIRST button of the newest buttoned message (Apply is always first)."""
    for e in reversed(ENTRIES):
        if e.get("cb"):
            data = e["cb"][0][0]
            label = label or e["labels"][0][0]
            await handlers.handle_event(rec, extract_event({
                "update_id": _upd[0], "callback_query": {
                    "id": f"cb{_upd[0]}", "message": {"message_id": e["mid"], "chat": {"id": CHAT}},
                    "data": data}}))
            _upd[0] += 1
            ENTRIES.append({"side": "tap", "text": f"👆 taps {label}"})
            return
    raise RuntimeError("no buttoned message to tap")


async def tick_at(h, m):
    when = datetime(2026, 1, 20, h, m, tzinfo=IST).astimezone(timezone.utc)
    await engine.tick(rec, now=when)
    ENTRIES.append({"side": "clock", "text": f"engine tick · {h:02d}:{m:02d} IST"})


# --- scripted AI --------------------------------------------------------------
MULTI = [
    {"name": "Pantoprazole 40", "form": "tablet", "tablets_per_dose": 1,
     "slots": ["breakfast"], "food": "before", "days": 10},
    {"name": "Azithromycin 500", "form": "tablet", "tablets_per_dose": 1,
     "slots": ["breakfast", "dinner"], "food": "after", "days": 5},
    {"name": "Dolo 650", "form": "tablet", "tablets_per_dose": 1,
     "slots": ["breakfast", "lunch", "dinner"], "food": "after", "days": 3},
    {"name": "Levocetirizine 5", "form": "tablet", "tablets_per_dose": 1,
     "slots": ["dinner"], "food": "after", "days": 3},
    {"name": "Doxycycline 100", "form": "capsule", "tablets_per_dose": 1,
     "slots": ["lunch"], "food": "after", "days": 7},
]
SINGLE = [
    {"name": "Augmentin 625 Duo", "form": "tablet", "tablets_per_dose": 1,
     "slots": ["dinner"], "food": "after", "days": 7},
]
UNREADABLE = {"medicines": [], "notes": ["unreadable"]}

_extract_calls = [0]


async def fake_extract(image_bytes, mime="image/jpeg"):
    _extract_calls[0] += 1
    n = _extract_calls[0]
    if n == 1:
        return {"medicines": MULTI, "notes": []}
    if n == 2:
        return {"medicines": SINGLE, "notes": []}
    return UNREADABLE


async def fake_interpret(user_text_arg, context):
    t = user_text_arg.lower()
    if "azithromycin" in t and "before" in t:
        return {"operations": [{"op": "update_medicine", "target": "azithromycin",
                                "fields": {"food": "before"}}], "clarify": None}
    if "9:45" in t or "9 45" in t:
        return {"operations": [{"op": "set_meal_time", "target": "dinner", "new_value": "21:45"}],
                "clarify": None}
    if "8:50" in t:
        return {"operations": [{"op": "set_meal_time", "target": "dinner", "new_value": "20:50"}],
                "clarify": None}
    if "30 minutes before" in t or "30 min before" in t:
        return {"operations": [{"op": "set_offset", "target": "before", "new_value": 30}],
                "clarify": None}
    if "make do" in t:  # deliberately ambiguous target → changesets raises Ambiguous
        return {"operations": [{"op": "update_medicine", "target": "Do",
                                "fields": {"food": "before"}}], "clarify": None}
    if t.strip() == "doxycycline" or ("doxycycline" in t and "before" in t):
        return {"operations": [{"op": "update_medicine", "target": "doxycycline",
                                "fields": {"food": "before"}}], "clarify": None}
    if "fluconazole" in t:
        return {"operations": [{"op": "add_medicine", "prescription": "draft",
                                "fields": {"name": "Fluconazole 150", "form": "tablet",
                                           "tablets_per_dose": 1, "slots": ["dinner"],
                                           "food": "after", "days": 3}}], "clarify": None}
    # gibberish (edit & stray): nothing mappable
    return {"operations": [], "clarify": None}


handlers.extract_prescription = fake_extract
handlers.interpret_edit = fake_interpret


async def fake_download(token, file_id):
    return b"\x89PNG ..simulated-photo-bytes.."


handlers.download_photo = fake_download

# --- the scenarios ------------------------------------------------------------

async def main():
    sim_db = ROOT / "scripts" / "_sim.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(sim_db) + suffix).unlink(missing_ok=True)
    db.init(str(sim_db))

    # -- 0 · onboarding ---------------------------------------------------------
    header("Onboarding", "First-ever message. No accounts, no phone number — the chat IS the app.")
    await user_text("/start")

    # -- 1 · multi-medicine photo ------------------------------------------------
    header("Photo #1 — a 5-medicine prescription",
           "06:30 IST. Breakfast 08:00 · lunch 14:00 · dinner 19:00 · before −15 min · after +45 min.")
    await user_photo()
    await tap("✅ Yes, start my reminders")

    # -- 2 · a day of reminders --------------------------------------------------
    header("Day 1 — the reminder moment",
           "Engine ticks every 15 min. Each reminder gets exactly one shot: ✅ Taken / ⏭ Skip, "
           "then the buttons clear silently — no nagging, no reply messages.")
    await tick_at(7, 46)
    await tap("✅ Taken")
    await tick_at(8, 46)
    await tap("⏭ Skip today")
    await tick_at(14, 46)  # Dolo + Doxycycline share the after-lunch group
    await tap("✅ Taken — all 2")
    await tick_at(19, 46)
    await tap("✅ Taken — all 3")

    # -- 3 · single medicine, one slot (a second prescription) --------------------
    header("Photo #2 — a single-medicine prescription (different meals)",
           "Antibiotic course, dinner-only, after food. Becomes a second, separate prescription.")
    await user_photo()
    await tap("✅ Yes, start my reminders")

    # -- 4 · edit loop: fix a medicine --------------------------------------------
    header("Edit — change food timing", "/edit opens the edit loop; plain language is the interface.")
    await user_text("/edit")
    await user_text("Azithromycin is actually before food")
    await tap("✅ Apply")

    # -- 5 · edit loop: meal times & offsets (15-minute grid) ----------------------
    header("Edit — meal time & reminder offsets",
           "All user-facing times sit on 15-minute marks; anything off-grid is rejected.")
    await user_text("/edit")
    await user_text("I usually have dinner at 9:45 at night")
    await tap("✅ Apply")
    await user_text("/edit")
    await user_text("Hmm actually dinner is usually at 8:50")
    await user_text("And remind me 30 minutes before meals instead of 15")
    await tap("✅ Apply")

    # -- 6 · ambiguous edit → one clarifying question ------------------------------
    header("Edit — ambiguous request",
           "…\"Do\" matches Dolo 650 and Doxycycline 100 → exactly one clarifying question.")
    await user_text("/edit")
    await user_text("Make Do before food")
    await user_text("Doxycycline")
    await tap("✅ Apply")

    # -- 7 · bad photo, then typed input ------------------------------------------
    header("Photo #3 — blurry/unreadable image",
           "Extraction finds nothing legible → one generic failure line, and the chat stays open "
           "so the user can just type the medicines.")
    await user_photo()
    await user_text("Fluconazole 150 — one tablet after dinner — 3 days")
    await tap("✅ Yes, start my reminders")

    # -- 8 · gibberish inputs -------------------------------------------------------
    header("Gibberish — while editing, and out of nowhere",
           "Unparseable edit → one AI failure voice. Total nonsense with nothing open → gentle nudge.")
    await user_text("/edit")
    await user_text("asdf qwerty lorem")
    await user_text("hey what's up")

    # -- 9 · pause / resume (at prescription level) ---------------------------------
    header("Pause & resume",
           "Pause lives on the prescription: target one (/pause Azithro) or all (/pause).")
    await user_text("/pause Azithro")
    await user_text("/resume Azithro")
    await user_text("/pause")
    await user_text("/resume")

    # -- 10 · status commands -------------------------------------------------------
    header("Status: /today · /prescriptions", "Raw state, re-read from the DB — no adherence scoring.")
    await user_text("/today")
    await user_text("/prescriptions")

    # -- 11 · cancel & delete --------------------------------------------------------
    header("End of life: /cancel and /delete my data")
    await user_text("/cancel Dolo")
    await user_text("/delete")
    await user_text("no")
    await user_text("/delete")
    await user_text("YES")

    render_html()
    print_text()


# --- output: HTML artifact -----------------------------------------------------

def _btn_rows(e):
    if not e.get("labels"):
        return ""
    rows = "".join(
        "".join(f'<button class="btn">{html.escape(b)}</button>' for b in row)
        for row in e["labels"])
    return f'<div class="btns">{rows}</div>'


def render_html():
    body = []
    for e in ENTRIES:
        side = e["side"]
        if side == "header":
            body.append(f'<h2>{html.escape(e["text"])}</h2>'
                        + (f'<p class="note">{html.escape(e["note"])}</p>' if e.get("note") else ""))
        elif side == "clock":
            body.append(f'<div class="clock">{html.escape(e["text"])}</div>')
        elif side == "tap":
            body.append(f'<div class="tap">{html.escape(e["text"])} — buttons clear, nothing else sent</div>')
        elif side == "note":
            body.append(f'<div class="tap">✍️ {html.escape(e["text"])}</div>')
        elif side == "user":
            body.append(f'<div class="row me"><div class="bubble me">{html.escape(e["text"])}</div></div>')
        else:  # bot
            body.append(f'<div class="row botr"><div class="bubble bot">{html.escape(e["text"])}'
                        f'{_btn_rows(e)}</div></div>')
    html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>ReMed — simulated user chat (exact bot output)</title>
<style>
  body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; background:#17212b; color:#e8edf2;
         max-width: 560px; margin: 0 auto; padding: 24px 12px 80px; }}
  h1 {{ font-size: 20px; }} .meta {{ color:#8fa4b3; font-size:13px; margin-bottom:28px; }}
  h2 {{ font-size: 15px; background:#2b5278; display:inline-block; padding:6px 14px;
       border-radius:14px; margin:34px 0 4px; font-weight:600; }}
  .note {{ color:#8fa4b3; font-size:12.5px; margin:4px 0 10px; }}
  .row {{ display:flex; margin:6px 0; }} .me {{ justify-content:flex-end; }}
  .bubble {{ white-space:pre-wrap; max-width:84%; padding:9px 12px; border-radius:12px;
             font-size:14px; line-height:1.45; }}
  .me .bubble {{ background:#2b5278; border-bottom-right-radius:4px; }}
  .bot {{ background:#232e3c; border-bottom-left-radius:4px; }}
  .btns {{ margin-top:8px; display:flex; flex-wrap:wrap; gap:6px; }}
  .btn {{ background:#2b5278; color:#fff; border:1px solid #3d6a9e; border-radius:8px;
          padding:6px 10px; font-size:13px; cursor:default; }}
  .tap {{ color:#8fa4b3; font-size:12.5px; text-align:center; margin:8px auto; font-style:italic; }}
  .clock {{ color:#6d8194; font-size:11px; text-align:center; margin:10px 0 4px;
            letter-spacing:.06em; text-transform:uppercase; }}
</style></head><body>
<h1>ReMed — simulated chat, real bot output</h1>
<p class="meta">Generated by <code>scripts/simulate_chat.py</code> on 2026-01-20. Keeps the
<b>real handlers, rendering, change-set engine and reminder engine</b>; only the AI responses are
scripted and photos appear as a placeholder. Clock: 2026-01-20, 06:30 IST ·
breakfast 08:00 · lunch 14:00 · dinner 19:00 · before −15 min · after +45 min · Asia/Kolkata.</p>
{"".join(body)}
</body></html>"""
    out = ROOT / "chat-simulation.html"
    out.write_text(html_doc, encoding="utf-8")
    print(f"wrote {out}")


def print_text():
    for e in ENTRIES:
        s = e["side"]
        if s == "header":
            print(f"\n=== {e['text']} ===")
            if e.get("note"):
                print(f"    ({e['note']})")
        elif s == "clock":
            print(f"    -- {e['text']} --")
        elif s == "tap":
            print(f"    [user taps {e['text']}]")
        elif s == "note":
            print(f"    [{e['text']}]")
        elif s == "user":
            print(f"USER  > {e['text']}")
        else:
            print(f"BOT   > {e['text']}")
            for row in e.get("labels", []):
                print(f"        buttons: {[b for b in row]}")


if __name__ == "__main__":
    asyncio.run(main())
