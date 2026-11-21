"""Message rendering — templates from conversation-script-v0.2.md.

Line format (locked decision #6):
  NAME — meal slots — before/after food — [# tablets only if ≠ 1] — # days
"""
import json

from . import scheduling
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------- formatting helpers

def _num(n) -> str:
    return f"{n:g}" if isinstance(n, float) else str(n)


def _plural_days(days: int) -> str:
    return f"{days} day" if days == 1 else f"{days} days"


def _plural_tablets(n) -> str:
    if n == 1:
        return "1 tablet"
    if n == 0.5:
        return "half tablet"
    return f"{_num(n)} tablets"


def _slots_join(slots: list[str]) -> str:
    if len(slots) == 1:
        return slots[0]
    if len(slots) == 2:
        return f"{slots[0]} & {slots[1]}"
    return ", ".join(slots[:-1]) + f" & {slots[-1]}"


def fmt_local_time(iso_utc: str, tz_name: str) -> str:
    dt = scheduling.parse_utc(iso_utc).astimezone(ZoneInfo(tz_name))
    return dt.strftime("%-I:%M %p")


def fmt_local_date(iso_date: str) -> str:
    return datetime.fromisoformat(iso_date).strftime("%b %-d")


def tz_display(tz_name: str) -> str:
    now_local = datetime.now(ZoneInfo(tz_name))
    off = now_local.utcoffset() or timezone.utc.utcoffset(now_local)
    total = int(off.total_seconds() // 60)
    sign = "+" if total >= 0 else "−"
    total = abs(total)
    offset = f"UTC{sign}{total // 60:d}:{total % 60:02d}"
    return f"{now_local.tzname() or tz_name} ({offset})"


# ---------------------------------------------------------------- medicine lines

def medicine_line(m: dict, prefix: str = "💊") -> str:
    # prescription/draft tables show names uppercase (script v0.2); reminders keep the
    # original transcription so the box label matches what the patient sees
    parts = [m["name"].upper(), _slots_join(m["slots"]), f"{m['food']} food"]
    if m["tablets_per_dose"] != 1:
        parts.append(_plural_tablets(m["tablets_per_dose"]))
    parts.append(_plural_days(m["duration_days"]))
    return f"{prefix} " + " — ".join(parts)


def reminder_medicine_line(m: dict, show_food: bool = False) -> str:
    parts = [m["name"], _plural_tablets(m["tablets_per_dose"])]
    if show_food:
        parts.append("before food")
    return "💊 " + " — ".join(parts)


def prescriptions_table(medicines: list[dict]) -> str:
    return "\n".join(medicine_line(m) for m in medicines)


# ---------------------------------------------------------------- Flow 2 — draft + confirmation

def draft_message(session: dict) -> str:
    user = session["user"]
    lines = ["Here's what I read. Please check every line 👀", ""]
    for rx in session["prescriptions"]:
        for m in rx["medicines"]:
            if not m.get("removed"):
                lines.append(medicine_line(m))
    lines += [
        "",
        f"⏰ Scheduling in {tz_display(user['timezone'])}, around your meals:",
        f"Breakfast {user['meals']['breakfast']} · Lunch {user['meals']['lunch']} · "
        f"Dinner {user['meals']['dinner']} (defaults — changeable anytime)",
        "",
        "Does this look right?",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- Flow 5 — edit confirmation (FULL table)

def edit_confirm_message(session: dict) -> str:
    actives = [rx for rx in session["prescriptions"] if rx.get("status") != "cancelled"
               and any(not m.get("removed") for m in rx["medicines"])]
    if not actives:
        return ("Here's the updated prescription:\n\n"
                "(no medicines left — Apply will cancel everything)")
    header = ("Here's the updated prescription:" if len(actives) == 1
              else "Here are your updated prescriptions:")
    blocks = [header, ""]
    for rx in actives:
        if len(actives) > 1:
            end = f" → ends {fmt_local_date(rx['end_date'])}" if rx.get("end_date") else ""
            blocks.append(f"📄 Started {fmt_local_date(rx['start_date'])}{end}")
        for m in rx["medicines"]:
            if not m.get("removed"):
                blocks.append(medicine_line(m))
        blocks.append("")
    return "\n".join(blocks).rstrip()


def clarifying_message(names: list[str]) -> str:
    quoted = " and ".join(f'"{n}"' for n in names[:4])
    return f"I see more than one match here: {quoted}. Which one do you mean?"


# ---------------------------------------------------------------- Flow 3 — confirmed → scheduled

def scheduled_confirmation(chat_id: str, prescription_ids: list[str],
                           now: datetime | None = None) -> str:
    from . import db as _db
    user = _db.get_user(chat_id)
    now = now or datetime.now(timezone.utc)
    today = scheduling.today_local(user["timezone"], now)

    marks = ",".join("?" * len(prescription_ids))
    total = _db.query_one(
        f"SELECT COUNT(*) c FROM dose_events de JOIN medicines m ON m.id=de.medicine_id"
        f" WHERE m.prescription_id IN ({marks}) AND de.status='pending'",
        tuple(prescription_ids),
    )["c"]

    # per-medicine course end dates, grouped by date
    ends: dict = {}
    for pid in prescription_ids:
        rx = _db.query_one("SELECT start_date FROM prescriptions WHERE id=?", (pid,))
        start = datetime.fromisoformat(rx["start_date"]).date()
        for m in _db.medicines_for(pid):
            end = start + timedelta(days=m["duration_days"] - 1)
            ends.setdefault(end, []).append(m["name"])

    lines = []
    if ends:
        offsets = [(end - today).days for end in ends]
        lo, hi = max(1, min(offsets)), max(offsets)
        span = _plural_days(hi) if lo == hi else f"{lo}–{hi} days"
        lines.append(f"✅ All set — {total} doses scheduled over the next {span}.")
    else:
        lines.append("✅ All set — nothing scheduled.")

    events = scheduling.todays_remaining(chat_id, now)
    if events:
        lines += ["", "Today:"]
        last_group = None
        for ev in events:
            t = fmt_local_time(ev["scheduled_time_utc"], user["timezone"])
            if ev["reminder_group"] != last_group:
                label = f"{'before' if ev['food'] == 'before' else 'after'} {ev['slot']}"
                lines.append(f"• {t} ({label}): {ev['name']}")
                last_group = ev["reminder_group"]
            else:
                lines[-1] += " · " + ev["name"]
    if ends:
        parts = ["".join([" & ".join(sorted(names)), " ", fmt_local_date(end.isoformat())])
                 for end, names in sorted(ends.items())]
        lines += ["", "Course ends: " + " · ".join(parts)]
    lines += ["", "Tip: tap ✅ on a reminder when you've taken it — that's all I'll ever ask."]
    return "\n".join(lines)


# ---------------------------------------------------------------- Flow 4 — the reminder

def reminder_message(events: list[dict], tz_name: str) -> tuple[str, list[list[dict]]]:
    """events: rows of one reminder_group (same slot+food, carrying offsets)."""
    first = events[0]
    t = fmt_local_time(first["scheduled_time_utc"], tz_name)
    food = first["food"]
    if food == "before":
        mins = abs(int(first["offset_before_min"]))
        when = f"{mins} min before {first['slot']}" if mins else f"at {first['slot']}"
    else:
        when = f"after {first['slot']}"
    lines = [f"⏰ {t} — {when}", ""]
    for ev in events:
        m = {"name": ev["name"], "tablets_per_dose": ev["tablets_per_dose"]}
        lines.append(reminder_medicine_line(m, show_food=(food == "before")))
    text = "\n".join(lines)
    n = len(events)
    taken_label = "✅ Taken" if n == 1 else f"✅ Taken — all {n}"
    group = first["reminder_group"]
    buttons = [[
        {"text": taken_label, "callback_data": f"ack:taken:{group}"},
        {"text": "⏭ Skip today", "callback_data": f"ack:skip:{group}"},
    ]]
    return text, buttons


# ---------------------------------------------------------------- commands

def today_message(rows, tz_name: str) -> str:
    if not rows:
        return "✔ Nothing left today — you're done."
    lines = ["📅 Rest of today:"]
    for ev in rows:
        t = fmt_local_time(ev["scheduled_time_utc"], tz_name)
        label = f"{'before' if ev['food'] == 'before' else 'after'} {ev['slot']}"
        lines.append(f"• {t} ({label}): {ev['name']}")
    return "\n".join(lines)


def prescriptions_message(chat_id: str) -> str:
    from . import db as _db
    from .config import settings  # noqa: F401
    rows = _db.prescriptions_for(chat_id, ("active", "cancelled", "expired"))
    if not rows:
        return "No prescriptions yet. Send me a photo of one to get started 📷"
    lines = ["📄 Your prescriptions:", ""]
    for i, rx in enumerate(rows, 1):
        status = {"active": "active", "cancelled": "cancelled", "expired": "ended"}[rx["status"]]
        lines.append(f"{i}. {fmt_local_date(rx['start_date'])} → {fmt_local_date(rx['end_date'])} ({status})")
        for m in _db.medicines_for(rx["id"]):
            slots = json.loads(m["slots"])
            lines.append("   " + medicine_line(dict(m) | {"slots": slots}).replace("💊", "·", 1))
    return "\n".join(lines)


def help_message() -> str:
    return (
        "ReMed — reminders for medicines 💊\n"
        "\n"
        "Send a photo of your prescription and I'll read it and set up reminders "
        "after you confirm.\n"
        "\n"
        "/today — today's remaining doses\n"
        "/edit — edit your prescription (free text)\n"
        "/prescriptions — list my prescriptions\n"
        "/pause — pause all reminders\n"
        "/resume — resume reminders\n"
        "/cancel [name] — cancel one prescription\n"
        "/delete — delete all my data\n"
        "/help — this list"
    )


def start_message() -> str:
    return (
        "Hey 👋 I'm ReMed — reminders for medicines.\n"
        "\n"
        "Send me a photo of your prescription and I'll read it, confirm it with you, "
        "and remind you to take the right medicine at the right time until the course ends."
    )


# ---------------------------------------------------------------- misc copy

GENERIC_FAILURE = (
    "Hmm, that didn't work. 🙁\n"
    "\n"
    "Please try again — a clear, well-lit photo of just the medicines list works best, "
    "or type it in your own words, e.g.\n"
    "   \"Azithromycin 500, before breakfast and dinner, 5 days\""
)

EDIT_FAILURE = (
    "Hmm, that didn't work. 🙁\n"
    "\n"
    "Please try rephrasing — plain words work best, e.g.\n"
    "   \"Dinner is usually at 8:30 PM\""
)

EDIT_ONBOARDING = (
    "Sure — tell me what to change, in your own words. For example:\n"
    "  • \"Azithromycin is actually 5 days, and it's before food\"\n"
    "  • \"Dinner is usually at 8:30 PM\"\n"
    "  • \"Remind me 20 minutes before meals instead of 15\"\n"
    "  • \"Stop Dolo 650 — the doctor cancelled it\""
)

EDIT_ONBOARDING_DRAFT = (
    "Tell me what's wrong, in your own words. For example:\n"
    "  • \"Azithromycin is actually 5 days, and it's before food\"\n"
    "  • \"That's 2 tablets of Pantoprazole\"\n"
    "  • \"Stop Cheston Cold\""
)

NUDGE = "I'm not sure what to do with that. Send /help to see what I can do, or 📷 a prescription photo to get started."
STATE_EXPIRED = "That session expired — let's start over. Send a photo or use /edit."
