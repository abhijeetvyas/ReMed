# Product Requirements — ReMed (one-pager)

Status: DRAFT v0.4 (founder feedback on conversation script folded in: full-table edit confirmation, natural message volume, trimmed flows)
Owner: product founder (solo, learning-oriented MVP)

## Problem
Patients with multi-day, multi-medicine prescriptions forget doses, take before-food doses late, and must keep re-reading the prescription. Existing solutions require installing an app, which nobody does for an occasional need.

## Vision
A Telegram chat assistant. Send one photo of a prescription; get reliable, well-timed reminders for every dose until the course ends.

## Target user (MVP)
One patient per chat; smartphone owner; on Telegram; taking a time-bound course of 1+ oral medicines. Family/multi-patient mode is future.

## Identity & timezone
- **Uniqueness & authentication = Telegram chat ID.** All reads and edits only from the authenticated chat. Phone number never required.
- Timezone detected at first message and used for all scheduling (shown as "Scheduling in IST"). Change detection/prompting is future; the per-user timezone field exists from day one.

## Meal anchors & reminder policy
- Default meal times: breakfast 8:00, lunch 14:00, dinner 19:00; stated at first confirmation, editable anytime.
- **One reminder per dose slot.** Before-food medicines remind at (meal − 15 min); after-food at (meal + 45 min). No second ping, no follow-up.
- Offsets configurable per user, minutes only (default −15 / +45).
- Natural message volume: up to ~6 reminder messages/day (before/after × 3 meals), grouped by slot. No artificial cap below that.
- Acknowledgment (✅ Taken / ⏭ Skip) produces **no reply message** — only button-state updates. No "skip = miss" coaching copy.
- Every reminder is self-sufficient: name, meal context, food instruction.

## Intake & OCR expectations
- Photo → draft schedule → explicit confirmation before anything is scheduled. Confirmation gate is non-negotiable.
- Confidence asymmetry assumed: medicine names are the OCR-weak part; structure (slots, food relation, day counts) is extracted reliably.
- Dose is modeled per medicine; default of 1 tablet is implicit and only rendered when different, positioned before the day count.
- Draft line format: NAME — meal slots — before/after food — [# tablets] — # days.
- All processing failures (unreadable image, unparseable edit) use **one generic error voice** — single AI failure point, no internals exposed.

## Editing — AI-processed free text (decided)
- One command: **Edit Prescription**, then any free text ("Azithromycin is actually 5 days and before food"). Covers medicines, meal times, offsets, cancelling a medicine, timezone.
- The AI proposes a change-set against the structured prescription object; on confirmation the bot shows the **FULL updated prescription table** with Apply / Try-again buttons.
- Ambiguity → one clarifying question instead of guessing. Unparseable → generic failure copy, retry until Apply. Nothing saves without Apply. Change-sets are schema-validated before display; the AI never writes directly to live data.
- Edits are allowed while reminders are paused.
- Strict-format edit commands: none.

## Prescription data model
- Every prescription is one object: start date (default today; UI for other start days is future but the field exists), medicines (name, dose form, dose per occasion, slots, food relation, duration in days), computed end date.
- Expiry derives from duration → active vs expired; all prescriptions stored per user and listable in chat.
- Multiple confirmed prescriptions may coexist; reminder merging across them is future.
- Taken/skip feedback recorded raw per dose; no adherence summaries in this version.

## Commands
/start · /today · /edit · /prescriptions · /pause · /resume · /cancel [name] · /delete · /help. Pausing blocks reminders only, never edits.

## Explicitly out of scope (this version)
Course-completion/expiry notifications, downtime catch-up messages, start-day choice, duplicate-prescription disambiguation, timezone-change prompts, adherence summaries, doctor dashboard, WhatsApp/Calendar, payments, refill reminders, drug interaction/brand logic, multi-language, family mode, web editing.

## Future (parked)
All out-of-scope items above, plus: merged reminders across active prescriptions; web view with Telegram-delivered OTP; doctor–patient pairing and adherence dashboard (eventual monetization); WhatsApp as India-reach channel; calendar export; refill nudges.

## Non-functional requirements
- **Correctness over cleverness.** Every mutation (OCR intake or AI edit) passes the confirmation gate.
- **Deliverability.** Reminder reliabilityengineering: delivery acknowledgment, downtime recovery (silently, user notified later), per-user timezone storage, server-timezone discipline.
- **Privacy.** Minimal PII (chat ID); images deleted after transcription; bounded retention; /delete immediate.
- **Cost.** Free/cheap hosting; OCR order of ₹ 1–5 per doc; one LLM call per edit iteration.
- **No install.** Works entirely inside the chat; onboarding is one message.

## Success metrics (personal-use MVP)
- % of prescriptions photo → confirmed schedule in ≤ 3 exchanges.
- % of AI-edit iterations resolved in ≤ 2 tries.
- % of doses acknowledged (vs missed) over the course.
- % of courses where reminders stayed un-muted.
- Zero unsafe incorrect schedules in a spot-audit of 20 docs.
