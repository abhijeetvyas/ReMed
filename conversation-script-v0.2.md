# Conversation Script — ReMed (Telegram MVP)

Status: DRAFT v0.2 — bot renamed to ReMed; edits reworked; flows trimmed per founder feedback.
Companion to prd-v0.4.md. Message text is indicative tone/structure.

---

## Flow 0 — First contact (/start)

```
Hey 👋 I'm ReMed — reminders for medicines.

Send me a photo of your prescription and I'll read it, confirm it
with you, and remind you to take the right medicine at the right
time until the course ends.
```

Design notes:
- Minimal. No "things to know" section — details surface when relevant (Flow 2).
- No forms, no registration. Chat ID is identity, created silently.
- Timezone taken from this first message; no user-facing prompt (change notification is future).

## Flow 1 — Photo received

Immediate ack (within ~2s, before OCR):
```
📷 Got it. Reading your prescription…
```

## Flow 2 — Draft schedule + confirmation

Case A — read succeeded:

```
Here's what I read. Please check every line 👀

💊 AZITHROMYCIN 500 — breakfast & dinner — after food — 5 days
💊 PANTOPRAZOLE 40 — breakfast — before food — 2 tablets — 15 days
💊 DOLO 650 — breakfast, lunch & dinner — after food — 3 days
💊 CHESTON COLD — dinner — after food — 3 days

⏰ Scheduling in IST (UTC+5:30), around your meals:
Breakfast 8:00 · Lunch 14:00 · Dinner 19:00 (defaults — changeable anytime)

Does this look right?
[ ✅ Yes, start my reminders ]   [ ✏️ Something's wrong ]
```

Design notes:
- Line format fixed: NAME — meal slots — before/after food — [# tablets if ≠ 1] — # days.
- Default dose of 1 tablet is never shown; only printed when the prescription says otherwise, placed just before the day count.
- "Something's wrong" opens Flow 4 (edit loop) before anything is scheduled.
- Start-day selection ("start tomorrow") is NOT in this MVP. The data model carries a start date per prescription so it can be added later (tech spec must keep the field).

Case B — image could not be processed:

```
Hmm, that didn't work. 🙁

Please try again — a clear, well-lit photo of just the medicines list
works best, or type it in your own words, e.g.
   "Azithromycin 500, before breakfast and dinner, 5 days"
```

Design notes (failure copy, per founder): ONE generic failure template style used for all AI processing failures — unreadable images AND unparseable edit text share the same voice; no internals, no "the AI said…". Only the example line differs (medicine wording vs rephrase nudge).

## Flow 3 — Confirmed → scheduled

```
✅ All set — 10 reminders scheduled over the next 3–15 days.

Today:
• 7:45 AM (before breakfast): Pantoprazole 40
• 8:45 AM (after breakfast): Azithromycin 500 · Dolo 650
• 1:45 PM (before lunch): Pantoprazole 40
• 2:45 PM (after lunch): Azithromycin 500 · Dolo 650
• 6:45 PM (before dinner): Pantoprazole 40
• 7:45 PM (after dinner): Azithromycin 500 · Dolo 650 · Cheston Cold

Course ends: Dolo 650 & Cheston Cold Jan 23 · Azithromycin Jan 25 · Pantoprazole Feb 4
Tip: tap ✅ on a reminder when you've taken it — that's all I'll ever ask.
```

## Flow 4 — The reminder (the core moment)

Before-food slot (7:45 AM):

```
⏰ 7:45 AM — 15 min before breakfast

💊 Pantoprazole 40 — 1 tablet — before food

[ ✅ Taken ]   [ ⏭ Skip today ]
```

After-food slot (8:45 AM, grouped):

```
⏰ 8:45 AM — after breakfast

💊 Azithromycin 500 — 1 tablet
💊 Dolo 650 — 1 tablet

[ ✅ Taken — all 2 ]   [ ⏭ Skip today ]
```

On ✅ Taken / ⏭ Skip: **no reply message.** The bot only updates the button state on its own message (buttons clear / message dims). Reminder time = meal anchor + configured offset (default −15 before-food / +45 after-food).

## Flow 5 — Edit Prescription (free text, AI-processed)

Entry: "Edit Prescription" (or /edit) at any time, including while paused.

```
Sure — tell me what to change, in your own words. For example:
  • "Azithromycin is actually 5 days, and it's before food"
  • "Dinner is usually at 8:30 PM"
  • "Remind me 20 minutes before meals instead of 15"
  • "Stop Dolo 650 — the doctor cancelled it"
```

User replies free text. The AI proposes changes against the structured prescription object; **confirmation always shows the FULL updated prescription table** (not a change-list):

```
Here's the updated prescription:

💊 AZITHROMYCIN 500 — breakfast & dinner — before food — 5 days
💊 DOLO 650 — breakfast, lunch & dinner — after food — 5 days
💊 PANTOPRAZOLE 40 — breakfast — before food — 2 tablets — 15 days

[ ✅ Apply ]   [ ✏️ Wrong — try again ]
```

Ambiguous input → one clarifying question, no guessing:
```
I see two medicines starting with "Do": DOLO 650 and DOXICILLIN. Which one?
```

Unparseable input → generic failure copy, then Retry loop until [ ✅ Apply ]. Nothing saves without Apply. This same path covers medicines, meal times, reminder offsets, and cancelling a medicine.

## Flow 6 — Commands (full surface)

| Command | What it does |
|---|---|
| /start | Welcome (Flow 0) |
| /today | Today's remaining slots and times |
| /edit | Alias for Edit Prescription (Flow 5) |
| /prescriptions | My prescriptions: active + expired, with date ranges |
| /pause | Pause reminders — all prescriptions, or just one (`/pause Azithro`) (travel, hospital…). Edits still allowed. |
| /resume | Un-pause (same, `/resume Azithro` or `/resume`) |
| /cancel [name] | Cancel one prescription's reminders entirely |
| /delete | Delete ALL my data (typed "YES" confirmation) |
| /help | Show this list |

Notes:
- /cancel without a name replies with a picker of active prescriptions.
- Paused does not block /edit or /prescriptions.
- No "replace" flow: sending another photo simply goes through the normal read → confirm path (medicine-count reconciliation happens in the confirm step). Reminder merging across prescriptions is future.

## Standing rules
- One reminder per dose slot; grouped by time; no reply messages on taken/skip.
- Natural message volume: up to ~6 reminders/day (before-food × 3 meals + after-food × 3 meals) plus confirmations. No artificial cap below that.
- All edits from the authenticated chat only; phone number never required.
- Prescription photos + AI inputs/outputs retained in the internal `ai_calls` log (rev A, superseded the old delete-after-transcription rule) — never shared, wiped by /delete.

## Parked as future improvements (explicitly NOT in this MVP)
- Course-completion and expiry notifications ("last dose of Dolo 650", "prescription finished")
- Downtime catch-up messages after server incidents
- "Start tomorrow" onboarding choice at confirmation time
- Duplicate-prescription disambiguation ("new" vs "existing")
- Timezone-change detection prompt
- Merged reminders across multiple active prescriptions; web view with Telegram OTP; doctor dashboard; WhatsApp channel; adherence reports

## Open items for founder review
1. Bot handle: @ReMedBot or similar — availability to check when we set up the Telegram bot.
2. Grouped reminder button labels: one "✅ Taken — all N" button or per-medicine buttons? (Currently: one, for speed.)