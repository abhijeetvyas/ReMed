"""Message rendering — one template module per channel, shared helpers.

Channels (locked decision #1: Telegram only for MVP, WhatsApp is v2):
  telegram.py — the Telegram template set (all user-facing copy today)
  whatsapp.py — parked; will reuse the shared helpers, own templates

handlers/imports stay `from . import rendering` — the package dispatches to the
active channel's templates.
"""
from .telegram import *  # noqa: F401,F403
from .telegram import (  # explicit re-exports for tooling
    GENERIC_FAILURE, EDIT_FAILURE, EDIT_ONBOARDING, EDIT_ONBOARDING_DRAFT,
    NUDGE, STATE_EXPIRED, clarifying_message, draft_message, edit_confirm_message,
    fmt_local_date, fmt_local_time, help_message, medicine_line,
    prescriptions_table, prescriptions_message, reminder_message,
    scheduled_confirmation, start_message, today_message, tz_display,
)
