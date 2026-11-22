# ReMed — System Flowchart

Visual companion to tech-design-v0.1.md · v0.1
Legend: [User-facing] · [AI] · [Storage] · [Infra] · 🔒 = human confirmation gate (amber in the doc)

---

## 1 · Big picture

```
 [📱 User]  ⇄  [Channel adapter]  ⇄  [🧭 Router]  →  [🤖 AI layer]
 Telegram app     Telegram now;         photo → intake        ① Extract (image → JSON)
 chat_id = id     WhatsApp later;       edit cmd → edit       ② Interpret edit (text + current object → change-set)
                  same interface        /cmds, buttons        (the AI only proposes — never writes)
                          ↓                                        │ validated + confirmed by ordinary code
                          ↓                                        ↓
                 [🗄 DATABASE — single source of truth]  ←  [⏰ Reminder engine ← every ~1 min]
                  users · prescriptions · medicines ·          pick due jobs → render template
                  dose_events · reminder_queue ·               → send via adapter → delivery ack
                  conversation_state
                          ↓
                 [Telegram API] reminder msg + ✅ Taken / ⏭ Skip buttons
```

## 2 · Flow A — Photo intake

```
1. User sends photo ──► ack: "📷 Got it. Reading…"            (data: image bytes)
2. 🤖 AI Extract          image → JSON:  name · form ·
   tablets_per_dose (default 1) · slots · food · days          (data: medicines[])
3. ✅ Validator (plain code): days 1–90, ≥1 slot, values parseable
   fail → 1 retry → generic error + "type it in your own words" fallback
4. 🔒 CONFIRMATION GATE
   draft shown in fixed format:  NAME — meal slots — before/after food — [#tablets] — # days
   + timezone ("Scheduling in IST") + meal defaults (8:00/14:00/19:00)
   NOTHING is scheduled yet.  [ ✅ Yes, start ]  [ ✏️ Something's wrong → Flow B ]
5. User taps ✅ ──► APPLY (one transaction):
   1 prescriptions row + N medicines rows
   + WHOLE COURSE EXPANDED into dose_events (15 days × 2 slots = 30 rows)
   + reminder_queue rows with precomputed send_at
6. Confirmation msg: today's slots+times, per-medicine end dates
```

## 3 · Flow B — Edit Prescription (free text)

```
1. "Edit Prescription" / /edit  ──► prompt with examples          (works even while paused)
   state = pending_edit in conversation_state (TTL 30 min, in DB)
2. User types anything: "Azithromycin is actually 5 days and before food"
3. 🤖 AI Interpret edit
   IN : free text  +  CURRENT prescription object        (edit against reality)
   OUT: change-set JSON (op · target · new value)
4. Validator + ambiguity check
   fuzzy name match hits 2 medicines → 🔒 one clarifying question, no proposal
   garbage → generic failure copy → user retries (loop until right)
5. 🔒 CONFIRMATION GATE — FULL updated prescription table shown
   [ ✅ Apply ]  [ ✏️ Wrong — try again ]         nothing saved without Apply
6. APPLY: only FUTURE pending dose_events recomputed
   meal/offset/timezone → recompute send times · medicine changes → recreate future rows
   taken/skipped history is IMMUTABLE (adherence record stays honest)
```

## 4 · Flow C — Reminder tick (self-running, every 15 min)

```
1. Engine queries reminder_queue:  send_at ≤ now · state=pending
   · user not paused · prescription not paused (prescriptions.paused, rev A)
   (queue lives in the DB → restarts lose nothing)
2. Render template: slot heading + medicine lines; grouped per slot (one message)
3. Send via adapter + delivery ack
   ok → state=sent ·  fail → ≤3 retries in window → expire SILENTLY (no nag)
4. User taps ✅ Taken / ⏭ Skip
   dose_event.status ← taken/skipped · responded_at stamped
   NO reply message — buttons just clear on the reminder (silent ack)
5. Unanswered · window passes → status=missed, recorded silently
   raw feedback only; no summaries in this version (raw rows power the future doctor product)
```

## 5 · Data model (6 tables)

```
┌ users ───────────────────────────────────────────────────────────────────┐
│ chat_id 🔑 (=auth) · id (internal uuid) · channel                        │
│ timezone (IANA; menu default, not auto-detected — Bot API can't)         │
│ meal_breakfast/lunch/dinner (8:00/14:00/19:00 default, 15-min grid)      │
│ offset_before/after_min (−15/+45) · paused (master switch)               │
└──────────────────────────────────────────────────────────────────────────┘
┌ prescriptions ───────────────────────────────────────────────────────────┐
│ id 🔑 · chat_id (FK) · start_date (default today)                        │
│ end_date = start + max(days) − 1 · status: active | expired | cancelled  │
│ paused (prescription-level pause, rev A)                                 │
└──────────────────────────────────────────────────────────────────────────┘
┌ medicines ───────────────────────────────────────────────────────────────┐
│ id 🔑 · prescription_id (FK) · name · form · tablets_per_dose (default 1)│
│ slots · food (before/after) · duration_days (per-medicine!) · removed    │
└──────────────────────────────────────────────────────────────────────────┘
+ log tables (rev A): ai_calls ( every AI input incl. photo bytes + output +
  model/latency/ok — golden dataset; supersedes "images deleted after
  transcription" ) · user_messages (every inbound command/free text/photo)
                │  expanded AT APPLY TIME (the key mechanic) ↓
┌ dose_events ─────────────────────────────────────────────────────────────┐
│ id 🔑 · medicine_id · event_date · slot · scheduled_time (UTC, precomp.) │
│ status: pending → taken | skipped | missed(silent) · responded_at        │
│ one row per reminder moment — the raw adherence record                   │
└──────────────────────────────────────────────────────────────────────────┘
┌ reminder_queue ──────────────────────────────────────────────────────────┐
│ dose_event_id (FK) · send_at · state: pending|sent|failed|expired        │
│ attempts · message_id                                                    │
└──────────────────────────────────────────────────────────────────────────┘
┌ conversation_state ──────────────────────────────────────────────────────┐
│ chat_id 🔑 · state: pending_draft | pending_edit | pending_delete        │
│ payload (unapplied drafts/change-sets) · expires_at                      │
└──────────────────────────────────────────────────────────────────────────┘
```

## 6 · AI touchpoints

```
┌ ① EXTRACT — once per photo ──────────────────────────────────────────────┐
│ IN : image + fixed schema prompt                                         │
│ OUT: medicines[] JSON                                                    │
│ then: validate → draft → 🔒 confirm → Apply → expand to dose_events      │
└──────────────────────────────────────────────────────────────────────────┘
┌ ② INTERPRET EDIT — once per edit attempt ────────────────────────────────┐
│ IN : free text + CURRENT prescription object                             │
│ OUT: change-set JSON                                                     │
│ then: validate → FULL table → 🔒 Apply → recompute future rows only      │
└──────────────────────────────────────────────────────────────────────────┘
┌ RULE ────────────────────────────────────────────────────────────────────┐
│ The AI only PROPOSES. Code validates & applies. Every mutation passes a  │
│ human confirmation gate — this is what prevents confidently-wrong        │
│ reminders, the worst possible failure mode.                              │
└──────────────────────────────────────────────────────────────────────────┘
```

## 7 · Deployment & the WhatsApp end-state

```
[Hosting]        one small VPS (~$5/mo) or free PaaS · one process (FastAPI)
                 SQLite → Postgres when needed · Docker + Caddy (HTTPS webhook)
                 nightly backups · secrets in env vars only
[Global design]  meal times / offsets / timezone are per-user values — nothing
                 India-specific in the core · English-only copy for now
[WhatsApp path]  SAME core + second adapter: Meta business verification,
                 approved reminder templates, 24-h service window, phone linkage
                 → data model & flows unchanged (that's why the adapter boundary exists)
[Ops on budget]  uptime ping /health · dead-man's-snitch on the reminder loop
```
