# Med-Reminder — Brainstorming & Decisions Log

Status: DRAFT — points below are proposals to agree on, not final decisions.
Last updated: 2026-11-21

---

## 1. Problem statement

- Patient gets a paper prescription: several medicines, a course of N days, doses at morning/afternoon/evening (sometimes other times), taken **before food / after food / with food**, plus a few special cases (empty stomach, only if pain, half tablet, alternate days, etc.).
- Pain points:
  1. Patient must keep re-reading the prescription to know what to take when.
  2. Adherence is poor: people genuinely forget doses, and "before food" doses are almost always taken late (remembered only once they start eating).
  3. Nothing today closes this loop for a regular person without forcing them to install a dedicated health app.
- Core insight: the phone (and the chat app the patient already lives in) is always with the patient. Use it.

## 2. Product idea (one line)

Send a photo of your prescription to a chat bot (Telegram first); it reads the prescription, converts it into a structured, confirmable medication schedule, and then nudges you at the right times for the right medicine until the course ends.

## 3. Articulated user journey (MVP)

1. User opens a chat with the bot (Telegram to start).
2. User sends a photo of the prescription (optionally with a line like "start tomorrow").
3. System OCR/reads the image, extracts medicines, doses, timings, food instructions, and course duration.
4. System replies with a readable summary and asks the user to **confirm or correct** (e.g. "Is this correct? Reply YES or tell me what to fix").
5. On confirmation, the schedule is stored; reminders begin:
   - a pre-reminder ~10–15 min before (especially for "before food" doses — this is the killer feature),
   - the on-time reminder at the scheduled slot,
   - a polite follow-up/escalation nudge if the user marks a dose as missed or ignores it.
6. User acknowledges each dose in the chat (button tap: ✅ Taken / ⏭ Skip / ⏰ Remind me in 10 min).
7. At the end of the course (or each day/week), user can ask for an adherence summary of their own history.

## 4. Key design principles (proposed)

- **No app install.** Meet users in the channel they already use. Telegram first (best free bot API, no approval process, works in webhooks), WhatsApp as India-first follow-up (better reach there, but requires Meta Business API + template approvals + costs per conversation), Google Calendar as an option (events with reminders, but no easy two-way confirmation). Keep the channel layer abstracted so a second channel is a small addition.
- **Confirm before scheduling.** OCR will be wrong sometimes (handwriting!). Never schedule from an unconfirmed parse. The confirmation step is also how the user catches missed lines like "half tablet" or "alternate days".
- **The reminder must contain everything needed to act** without opening the prescription: medicine name, dose (1 tab / 5 ml / half pill), before/after food, and if possible a photo/simple reason ("antibiotic — finish the full course").
- **Handle the diabetes/breakfast style dependency:** "before food" really means "before a meal that you control the timing of". Options: ask the user at setup "when do you usually eat breakfast/lunch/dinner?" and anchor schedule to those times, or ask each morning "what time is breakfast?" Default to typical meal anchors with user-editable times.
- **Low friction above all.** One tap to acknowledge a dose. If acknowledging is work, users stop doing it and the adherence data (and future doctor product) dies.
- **Privacy-first posture.** Health data: store minimal PII (phone/chat ID + first name), delete prescription images after transcription (or keep encrypted), never use message content for training, clear delete-all-my-data command. Even in an MVP, this is cheap to do now and impossible to retrofit later.

## 5. Edge cases to design for

- Illegible handwriting → fall back to asking the user to type the medicine list (guided format), still better than failing silently.
- Multiple medicines at the same slot → group into one reminder, checklist per medicine.
- Courses with different lengths (e.g. antibiotic 5 days, supplement 30 days) → each medicine tracks its own end date.
- Half tablets, sachets, syrups, injections → keep dose as free text from the prescription + user confirmation, don't try to normalize too eagerly.
- Dose times change mid-course ("doctor said 2x/day now") → simple command to edit schedule.
- User in different timezone / travels → anchor to user's local timezone; allow "snooze 30–60 min".
- User starts the course later than the prescription date → ask "which day do you start?" and count days from there.
- Repeated (missed) reminders → cap escalation, don't spam (a user who mutes the bot is lost forever).
- Duplicate prescriptions / user sends same photo twice → detect near-identical active schedules and ask.

## 6. Track of course / schedule model (conceptual, no code yet)

- Prescription → list of Medicine entries: name, form (tablet/syrup/...), dose per occasion, frequency (slots), food relation (before/after/with/none), start day, duration in days, extra notes.
- Schedule timeline: for each medicine × each day × each slot → one Slot-Dose event: scheduled time, reminders to send, status (pending / taken / skipped / missed).
- Adherence = fraction of Slot-Doses marked taken on time (definition to fix: exact time? within X minutes? marked at all?).

## 7. Build scope for MVP (proposal)

In scope:
- Telegram bot with photo intake, automated read + confirmation, confirmable schedule, slot reminders with quick-action buttons, daily/weekly personal digest, edit & stop commands.
Out of scope (MVP):
- WhatsApp, Calendar, payments, doctor-side product, multi-language (maybe Hinglish formatting nicety), refill reminders, symptom tracking.

## 8. Later (v2+ — future, Park it)

- **Doctor ↔ patient pairing:** patient shares a link/code with the doctor; doctor gets a readonly adherence dashboard. Value: real evidence of adherence for chronic/severe conditions — this is the plausible money (doctors/clinics pay), the patient side stays free.
- WhatsApp channel for India reach (needs Meta business verification; consider it the growth channel, not MVP).
- Calendar export (one-tap .ics or Google Calendar events) for people who live on their calendar.
- Reminders to buy / refill a course before it runs out.
- Multi-prescription overlap management (one bot, all active courses in one daily view).

## 9. Cost / hosting notes

- Target: hosted on a cheap VPS / serverless function, total infra cost near zero at personal scale. The only meaningful variable cost is the OCR/LLM read of each prescription photo (~once per prescription, maybe a few cents) — acceptable.
- Keep the reminder scheduler dumb and reliable: one DB table of pending events + a tick loop / timed jobs; no clever infrastructure.

## 10. Open questions to settle before building

1. **Reminder timing defaults:** 15 min before + at slot time + 10 min after? (proposed: 10 min before for before-food doses, on-time, one nudge if unacknowledged for 20 min).
2. **Meal anchor times:** fixed defaults (8am/1pm/8pm, user-editable at setup) vs. asking daily? (proposed: editable fixed anchors, ask to shift only via command).
3. **Adherence definition:** does "taken" only count if acknowledged before the next slot? (proposed: yes — acknowledged, else missed; skip is its own status).
4. **Data retention:** how long to keep images and schedules, and auto-delete rule.
5. **Language/tone of the bot:** English, or bilingual Hinglish nudge copy for India.
6. **Identity model MVP:** is one Telegram account = one patient enough (no multi-patient family mode yet)? (proposed: yes — single user per chat, family mode is v2).
7. **Which OCR/LLM setup to use** (provider-agnostic interface so it can be swapped; decision needed only at build time).
8. **Name for the bot** (nice to have; affects Telegram bot handle availability).

## 11. Agreed decisions

_(updated as we discuss — nothing final yet)_

- [x] Channel: Telegram first, WhatsApp later (India), Calendar as add-on — pending final confirmation.
- [x] Flow: photo → read → confirm → schedule → reminders → acknowledge.
- [x] Pre-dose reminder is a core feature, not a nicety (especially before-food doses).
- [x] MVP is free, personal use, minimal infra; doctor dashboard is future work, not MVP.
