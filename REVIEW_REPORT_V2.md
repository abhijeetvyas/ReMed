# ReMed Review Report v2 — Product + Engineering

**Review date:** 2026-01-20
**Reviewers:** dual review — senior PM lens + senior code-reviewer lens
**Scope:** all of `app/` (~2,600 LoC), both AI prompts, all 5 test files (58 tests, executed: pass), all specs (`prd-v0.4`, `tech-design-deep-dive`, `conversation-script-v0.2`), README, landing page + ToS, deploy files.
**Method:** full read-through of every module; 58-test suite executed; **3 suspected defects reproduced empirically** (repro scripts in `tmp/verify_remed*.py`, outputs in Appendix A); every claim in the prior `REVIEW_REPORT.md` spot-checked.

> This report supersedes the prior `REVIEW_REPORT.md` (kept untouched). The prior report's verdict ("EXCELLENT — production-ready") was tested against the code and does not hold on several points — see §6.

---

## 1. Verdict (TL;DR)

The **architecture is sound and the safety spine (AI-proposes → validate → confirm-gate → transactional apply, immutable history, restart-safe queue) is real and well-tested**. Confirming-gate discipline, the 15-minute grid, deterministic precomputed UTC scheduling, guardrails, and observability tables are all genuinely built, not just documented.

But the review found **1 P0 correctness defect in the scheduling math** (silently shortens a course), **1 P1 AI-reliability hole** (malformed model JSON escapes every handler → the user is left in silence, and the model fallback chain is abandoned), and **2 P1 product gaps that break the PRD's own assumptions** (the duplicate-prescription "reconciliation happens in the confirm step" was never built; images sent as documents get total silence) plus a set of P2s (pause/resume lies, `reminder_group` instability after edits, "expired" status never computed, timezone never surfaced, etc.).

**Nothing here argues for a redesign.** Most fixes are <100 LoC total. But three of them are exactly the "unsafe incorrect schedule" failure the PRD names as a success metric ("zero unsafe incorrect schedules in a spot-audit of 20 docs") — so they belong before any real-user exposure.

| ID | Severity | Lens | One-liner |
|----|----------|------|-----------|
| E1 | **P0** | Eng | `start_date` comes from the **server clock**, not the user's timezone → a 5-day course confirmed late evening (IST) silently becomes 4 days |
| E2 | **P1** | Eng | Malformed/truncated AI JSON raises raw `JSONDecodeError` → intake/edit leave the user in **silence**; fallback chain abandoned |
| PM1 | **P1** | Product | Photos sent **as document/other media are silently ignored** — no reply at all |
| PM2 | **P1** | Product | Duplicate-prescription reconciliation ("happens in the confirm step" per PRD) **was never built** → second photo = silent double reminders |
| E3 | **P1** | Both | `prescriptions.status='expired'` is **never written** → `/prescriptions` shows ended courses as "active"; `MAX_ACTIVE_PRESCRIPTIONS` (10) eventually **locks long-term users out** |
| E4 | P2 | Product | `/resume Azithro` says "reminders back on" while the user-level master pause still blocks everything |
| E5 | P2 | Eng | Every recompute regenerates `reminder_group` UUIDs → pressing ✅ on a reminder sent *before* an edit silently does nothing (buttons never clear, dose re-reminds) |
| PM3 | P2 | Product | The per-user **timezone is never shown anywhere** (PRD says shown at confirmation; `tz_display` is dead code) |
| PM4 | P2 | Both | Bot's own edit-onboarding example ("Remind me **20** minutes before") is **rejected** by the 15-minute-grid validator |
| E6 | P2 | Eng | Webhook returns HTTP 200 (`ok:false` body) on secret mismatch / oversize → Telegram **won't redeliver**; default dev-secret is a known constant |
| PM5 | P2 | Product | Syrup/drop doses always render as "**tablets**" (form/ml not modeled in the line format) |
| PM10 | P2 | Product | ToS "we store only what the Service needs" contradicts the deliberate **photo-retention (golden-set) decision** — disclosure gap |
| E8 | P3 | Eng | `ensure_user` TOCTOU race (concurrent first messages can crash an event) |
| E9 | P3 | Eng | `guardrails._chat_locks` grows without eviction |
| Others | P3 | — | Fractional-day truncation, confirm-button UX, late reminders after resume, model-name chain verify, doc drift |

---

## 2. What is genuinely well-handled (verified, not assumed)

These were checked in code **and** in the test suite (all 58 pass):

- **Confirmation gate everywhere it matters.** Photo intake and every AI edit produce a confirmable payload; nothing writes to live data without Apply; change-sets are validated against a deep-copied snapshot, all-or-nothing (`changesets.apply_operations`). The "AI never writes directly" principle holds end-to-end.
- **Scheduling core.** Precomputed UTC `send_at`, meal-anchor + food-offset math, DST-safe regeneration, skip-past rule (afternoon confirm never bursts old slots), answered-dose guard (taken/skipped keys can never produce a second reminder after an edit), prescription-level + user-level pause both enforced in the due query (`engine.py:47`).
- **Reminder engine is restart-safe.** Queue lives in SQLite; `state='sending'` stranded rows are re-released by the sweep (`engine.py:23`); retries are capped at 3 and silently expire inside the window; the window itself can mark doses `missed` with no user message — matching PRD v0.4.
- **Ack semantics.** Groups are per `(day, slot, food)` — a ✅ can never ack a future day's dose (e2e test #5–7 covers replay idempotency). Silent ack (no reply message) matches the founder's rev B copy rules.
- **Guardrails are layered and realistic.** Per-chat hour/day/gap limits per bucket, photo-failure backoff, global AI daily budget (counted off `ai_calls` rows), auto-quarantine at 10 trips/hour, super-user bypass, text/image/pixel caps, magic-byte check, send pacing with deterministic per-chat jitter.
- **Ambiguity handling in edits**: sub-string matching that promotes to a single clarifying question (`Ambiguous`) instead of guessing — Flow 5 as scripted.
- **Observability.** `ai_calls` (full I/O, latency, model actually used), `user_messages`, `activity_log` are written consistently — the golden dataset is real and usable; `/delete` wipes all three plus everything else in one transaction.
- **Test infrastructure.** Simulated clock + `FakeTelegram` adapter is the right call; e2e covers photo→apply→remind→ack→missed and edit→recompute.

---

## 3. Engineering-lens findings

### E1 · P0 — Start date uses the server clock, not the user's timezone (reproduced)

`changesets.draft_session` (`app/changesets.py:74`) sets `today = date.today().isoformat()` — that is **the process's local date** (UTC in Docker), not the user's date. `_new_prescription` (edit path, `app/changesets.py:170`) has the same bug.

The repo's own tz default makes this a *live* bug for the primary market: between **00:00 and 05:30 IST**, a UTC-server `date.today()` is *yesterday* for the user.

**Reproduced** (Appendix A, script 2): user in `Asia/Kolkata` confirms at 02:00 IST on Jan 21 → stored `start_date = 2026-01-20` (`end_date = 2026-01-24`) → **the 5-day course expands to 4 dose-earning days**; the final day's doses (an antibiotic's last dose) silently vanish. All past-day-1 events are dropped by the legitimate skip-past rule, and nothing tells the user the course became one day shorter.

Why the test suite misses it: `tests/conftest.py:fixed_today` pins `changesets.date.today()` to the *same* date the simulated clock implies, so server-tz vs user-tz is never exercised. The module-level `chagesets.date` import is also exactly the seam the tests patch — a smell that this computation had no owner.

**Impact:** a medically wrong (shorter) schedule, silently. This is the precise "unsafe incorrect schedule" the PRD's success metrics zero-rate.

**Fix (small):** pass `now_utc` + user tz into `draft_session`/`_new_prescription` and use `scheduling.today_local(user["timezone"], now_utc)`. Add a regression test where server-date ≠ user-date (e.g., 20:30 UTC). For existing rows: recompute `start_date` once on deploy, or leave and notify affected users.

### E2 · P1 — Malformed model JSON escapes the AI contract; user gets silence; fallback chain abandoned (reproduced)

In `AIClient.complete_json_chain` (`app/ai.py:60-90`):
- `_parse_json_object` raises `AIError` only when **no braces** are present. If the content has braces but is invalid inside — which is exactly what **truncation at `max_tokens=1600`** produces on a long prescription table — `json.loads` raises raw `JSONDecodeError`, which is neither `AIError` nor a transport error → it propagates out of the loop **and the fallback chain dies on model #1** (Appendix A, script 1).
- Downstream: `_intake_photo` catches `(AIError, ValidationError, TelegramError, ImageTooLarge)` — `JSONDecodeError` is none of these. `_handle_edit_text` likewise. The exception then escapes `handle_event`, is swallowed by `_safe_handle` (webhook mode, `main.py:70`), and since the webhook already returned 200, **Telegram never redelivers**. Net user experience: the promised "📷 Got it. Reading your prescription…" acknowledgment, then **nothing, forever** — precisely the failure class the conversation script's "one generic failure voice" rule exists to prevent.
- Relatedże: `resp.json()["choices"][0]...` — an unexpected API payload raises `KeyError`/raw exceptions the same way; `extract_event`'s own route is safe but `httpx.HTTPStatusError` thrown by `download_photo`'s `raise_for_status()` is also uncaught by `_intake_photo`'s except-list.

**Fix (small):** wrap the parse in `AIError` (and try the next model on parse failure), `except Exception` → `AIError` at the client boundary; add a top-level failure fallback in `_intake_photo`/`_handle_edit_text` that sends `GENERIC_FAILURE` rather than losing the message silently. Add the "AI returns invalid JSON" test the old report claims already exists (it does not).

### E3 · P1 — Prescriptions never expire; the cap eventually locks loyal users out

Verified by grep: **no code path ever writes `prescriptions.status='expired'`**. The schema, deep-dive §6, PRD ("expiry derives from duration → active vs expired"), and rendering (`{... "expired": "ended"}`) all assume a computed expiry — but the computation is missing. Consequences:

1. `/prescriptions` shows a course ended 3 weeks ago as **"active"**.
2. `MAX_ACTIVE_PRESCRIPTIONS` (default 10) counts them: a user on their 11th course is **blocked from applying** ("You already have 10 active prescriptions — use /edit or /cancel first") with no indication that old, finished courses are the cause. The only remedy the message suggests (/cancel) works, but nothing tells them to do it.
3. Semantics: `engine`'s due query filters `p.status='active'`; today that's harmless, but the "expired" state is load-bearing in the spec's model.

**Fix:** trivial sweep in `_missed_sweep` or `maintenance()` — `UPDATE prescriptions SET status='expired' WHERE status='active' AND end_date < ? (today local, safe: end_date+1)`. Or compute status at display/cap time. Plus a course-finished decision item (§5, PM12).

### E4 · P2 — `/resume <name>` lies while the user master-pause is on

`/pause` (no arg) sets **both** `users.paused=1` and all prescriptions' `paused=1` (`handlers.py:210-211`). `/resume Azithro` clears only the prescription flag. The engine requires `u.paused=0 AND p.paused=0` — so after `/pause` then `/resume Azithro`, the bot replies "▶️ reminders back on — Dolo 650" **and nothing will ever send** until `/resume`. Same shape: `/pause Dolo` (one) after user-level `/resume` behaves incomprehensibly in the other direction. The data model (two switches) is per founder-rev A — fine — but the reply copy and/or `/resume` semantics must account for the master switch.

**Fix:** on any `/pause`/`/resume` with an arg, mention the master switch state ("Master pause is still on — use /resume to enable reminders"); or make `/resume <name>` also clear `users.paused` when nothing else is paused. Also worth unit-testing the matrix (user×rx × pause/resume×named).

### E5 · P2 — `reminder_group` UUIDs are regenerated on recompute → a just-sent reminder's ✅ silently dies

`expand_events` mints fresh `reminder_group` UUIDs at every expansion (`scheduling.py:126`). The edit path deletes future pending rows and re-inserts them → **new group ids**. Scenario: a reminder is delivered at 08:45 with buttons → user taps ✏️/sends an edit (perfectly timed, actually *because* of the reminder) → recompute replaces the pending rows for that (day, slot, food) with new groups → user then taps ✅ on the *delivered* message → `_ack_group` finds no rows for that group → returns early **without clearing the buttons** and without recording anything; the equivalent new row re-reminds later.

The README's guarantee "✅ can never ack a future day's dose" holds; the inverse problem — *an ack that should land never does* — does not. And it fails **silently** (no reply per design), so the user believes they've acked twice.

**Fix (cheap and clean):** make `reminder_group` **deterministic** — e.g. `uuid5(ns, chat_id|prescription_id|event_date|slot|food)` — so groups survive recompute (same rx, same slot-day) and old buttons keep working. Nothing else references the UUID shape. Alternatively: keep a `legacy_group_mapping` table across recompute, or on ack-miss, fall back to matching `(event_date, slot, food)` directly instead of the cached group id. A test: reminder sent → /edit applied → ack on the old message → dose marked, buttons cleared.

### E6 · P2 — Webhook error contract: 200-OK-with-ok:false means Telegram never announces failure

`main.py:52-58` returns HTTP 200 (`{"ok": false}` body) for: wrong URL secret, missing/incorrect `X-Telegram-Bot-Api-Secret-Token` header, and oversize bodies. Telegram only redelivers on **non-2xx**; so during a bot-side secret rotation, deliveries are "accepted" and dropped with no retry and no signal anywhere. Same story for a malformed JSON body (unhandled → 500, watermark untouched → retry loop). Also:
- `WEBHOOK_SECRET` defaults to `"remed-dev-secret"` — a deployed-but-unconfigured bot accepts anyone who read the README (test constants enshrine it). Deploy should refuse to serve the webhook with the default secret; `.env.example` should say "generate one."
- Dedup watermark is advanced **before** handling and handling is a fire-and-forget task — at-most-once semantics. Acceptable for reminders; for a user's photo (paid AI + expectation of a reply) the top-level `GENERIC_FAILURE` fallback from E2 is what makes this acceptable. Also consider a small set of recent `update_id`s instead of a single high-watermark so an out-of-order redelivery isn't dropped after a higher id advanced it.

### E7 · P2/P3 — AI reliability details worth a pass before launch
- **Model-name chain** defaults to `gemini-3.5-flash-lite → gemini-3.1-flash-lite → gemini-2.5-flash-lite` (config + `.env.example`). These names look speculative — the chain is data, and 404 already falls through (good), but if *all* names 404 the product is dead on arrival while `ai_calls` fills with failed rows; the daily budget even counts them. **Verify the chain against the live API at deploy time** and treat 404-for-all-models as a boot-time health check, not a per-call discovery. Failed calls currently count toward the global daily budget — consider only counting `ok=1` (or excluding 404-type failures) toward the budget.
- **max_tokens=1600 truncation** is the main *cause* of E2's invalid-JSON class for many-medicine prescriptions; consider raising it (output JSON for 15 meds is deep under it, but a chatty model preamble can burn tokens before the JSON) — and/or asking for JSON-only output format if the provider supports it (structured output / response_format).
- `_parse_json_object` raising `AIError` (missing braces) also **aborts the chain** (it re-raises inside the per-model loop) — if you want the chain to be resilient, parse failures should also fall through to the next model.
- `interpret_edit` passes the prompt as a plain string content; the second `prompt:` argument of `complete_json_chain` is dead — trimming the dead param removes a reading trap.

### E8 · P3 — `ensure_user` TOCTOU
`handle_event` calls `db.ensure_user(chat_id)` **outside** the per-chat slot, and the insert has no `ON CONFLICT`. Two near-simultaneous first messages from a chat (double-tap on a button + a message; concurrent webhook tasks) can both pass the existence check; one `INSERT` crashes the handler → that event is lost (200 already returned). One-line fix: `INSERT ... ON CONFLICT(chat_id) DO NOTHING` (then re-read). Bonus: `changesets.apply_session`'s user-insert path omits the `id` uuid — unreachable today (ensure_user always runs first) but worth tightening.

### E9 · P3 — `_chat_locks` never evicted
`guardrails._chat_locks` (and its `_sem` swap per event loop) has no cap/eviction, unlike the window table's 80%-trigger eviction. One small object per chat per loop; harmless at hobby scale, but it contradicts the module's own "7. Memory cap" contract — reuse the same eviction policy.

### Misc engineering notes (fine for MVP, worth knowing)
- The whole app is single-threaded async over a **synchronous** SQLite connection; the `threading.Lock` in `tx()` is belt-and-braces. Works; document the assumption in case a worker process is ever added (WAL is per-host; two uvicorn workers on one DB would also race the in-memory guardrail windows).
- `REMINDER_WINDOW_MIN=180`: a dose missed silently 3 h after scheduled. Combined with restart-backlog **late sends**: any pending dose with `send_at ≤ now` sends immediately after a restart — meaning up to ~3 h-old "before breakfast" reminders can arrive. Bounded and arguably correct; document it (see PM8).
- Fractional-day courses: `days = quantity / doses_per_day` in the extract prompt + `int()` truncation in `validate_medicine` → "10 tablets, 3/day" becomes 3 days, not 3.33. Truncation shortens courses (same medical direction as E1); decide the rounding rule (round up = safer) and encode it.
- `images.py` converts non-RGB (incl. alpha PNGs) to RGB on black — harmless for camera photos; a document-type PNG scan with transparency could lose legibility. Cosmetic.
- `landing` CTA uses `?start=web` deep-link payload; the bot never reads deep-link args — attribution no-op (fine), just know the parameter does nothing today.
- README drift: the repo tree comment says "28 pytest tests", later says "58" (58 is correct); the layout names `rendering.py` where it's `rendering/`; README says "docker-compose.yml" + Caddy but the compose file is the whole story (fine). Cosmetic, but this README is the founder-runbook — keep it exact.

---

## 4. Product-lens findings

### PM1 · P1 — Unsupported content types are a silent void (repro'd by code-read)

`extract_event` (`app/telegram.py:80-101`) handles `message.photo` and `message.text` only. A user who sends:
- a **photo as a document/file** (very common: people send "original quality" to avoid compression, especially for something they need read accurately — *recte*: a prescription),
- a **PDF scan** of a prescription,
- a sticker/voice/animation,

…gets **no response whatsoever**. The one response that costs nothing to send and saves the entire flow is the generic failure copy ("clear photo of just the medicines list, or type it in your own words"). Given the PRD's core metric — "% of prescriptions photo → confirmed schedule in ≤ 3 exchanges" — this is a direct hit on it. **Fix:** reply with a targeted variant of `GENERIC_FAILURE` for documents/images-as-file ("Send the image as a photo, not a file — just tap the image before sending") and the plain nudge otherwise. ~20 LoC + a test.

### PM2 · P1 — The PRD's duplicate-prescription assumption was never implemented

The conversation script says: "No replace flow: sending another photo simply goes through the normal path (**medicine-count reconciliation happens in the confirm step**)." There is no reconciliation of any kind in the confirm step: the draft table shows only the *new* photo's contents; nothing compares against `active_prescriptions`. Re-sending the same prescription (re-check after a doctor visit, a family member's copy, "did that work?") silently schedules **a second, fully overlapping reminder set**. And with E3 (nothing ever expires), cumulative overlap gets worse over weeks. Double reminders for the same dose is a mild-medications problem but a severe trust problem — and for users tracking a real course it invites taking doses twice ("the bot said 2 reminders, I'll take 2").

**Fix options (pick now, cheap):** at draft time, if any extracted medicine name matches an active prescription's medicine (fuzzy-normalized), add one line above the confirm table: "⚠️ You already have an active reminder for Azithromycin 500 (scheduled Jan 20–24). Cancel it first, or confirm to keep both." That's honest, tiny, and keeps the full replace/edit flows for later (already parked in PRD).

### PM3 · P2 — The assumed timezone is never shown (PRD explicitly requires it)

PRD v0.4: "Timezone … used for all scheduling (**shown as 'Scheduling in IST'**)". README: "shown at confirmation". In code, `rendering.tz_display` exists and **is dead code** — `draft_message` shows meal times but never the timezone. Every user outside IST is being scheduled in IST with zero indication, and the PRD even names the escape hatch ("change it via Edit Prescription — 'I moved to New York'") — but a user never learns they need it. **Fix:** one line on the draft confirmation: "Scheduling in IST (UTC+5:30) — tell me if that's wrong." Also genuinely useful: the meal-times block already renders; append the tz line there.

### PM4 · P2 — The bot's own onboarding example is rejected by its own validator

`EDIT_ONBOARDING` shows: "**Remind me 20 minutes before** meals instead of 15". The change-set validator enforces the 15-minute grid on offsets (`changesets._require_grid_minutes`) — reproduced: `-20` → "offset must be on a 15-minute mark". So the bot *advertises* an input it *breaks* on. The grid rule itself is a defensible design decision (tests enshrine it) — but then (a) fix the example copy, and (b) consider whether the grid really applies to offsets (PRD says "Offsets configurable per user, minutes only" with no grid), since "20 minutes before food" is an extremely natural request and grid-applied rejections will surface (failure copy doesn't even say why — `EDIT_FAILURE` says "try rephrasing"). Recommend: allow any integer minute offset (send-time precision loss of a few minutes is invisible; the tick is 15 min anyway), keep the grid for meal anchors only. At minimum make the validator's message user-facing-honest.

### PM5 · P2 — Liquid doses render as "tablets"

The schema carries `form: tablet|capsule|syrup|drop|injection|other`, but the line format never renders form or volume: `Zinc syrup — 5 ml/day` arrives as "ZINC — breakfast — after food — **5 tablets** — 7 days" (ml→`tablets_per_dose` via the extract prompt is inevitable given the schema; numbers then read as tablets). "Half tablet" is special-cased (`_plural_tablets`), so the infra knows the unit matters. **Minimal fix:** map `form` to the unit word in `_plural_tablets` (tablet/capsule/dose/…) and instruct the extract prompt to keep volumes in `tablets_per_dose` **only for solid forms** and use a `dose_text`-style field for liquids (schema change → validation + rendering + prompt, ~1 h). Health-comms credibility problem otherwise ("5 tablets" of syrup erodes all trust in confirmed schedules).

### PM6 · P2 — Confirmation-table buttons never clear (inconsistent with acks)

Draft/edit confirmation messages keep their ✅/✏️ buttons after Apply/Cancel; a stray second tap yields "That session expired — let's start over…" noise (or a fresh edit-onboarding if the state happens to still exist for draft_edit). Acks already do the right thing (`update_message_buttons` → cleared). **Fix:** after `_apply_draft`/`_apply_edit` succeed, clear the buttons on the originating message (callback context has `message_id` already). Same for `cancelrx:` picker after selection. Also prevents the auto-quarantine footgun: `apply_min_gap=60 s` + double-taps each cost a trip toward `AUTO_BLOCK_TRIPS=10`.

### PM7 · P2 — Landing/ToS says less about data than the product does

`terms.html` §4: "We store only what the Service needs to remind you: prescriptions, meal times, dose history, and minimal technical logs." The deliberate Rev A decision **retains the full prescription photo + AI I/O** in `ai_calls` (until capacity pruning or `/delete`). The sentence above arguably excludes that and a privacy-minded reader (or a regulator, or a journalist) can read the landing copy as "we don't keep your photo." For a health-adjacent product this is the one place over-brevity is a liability. **Fix:** add one honest sentence: "your prescription photo and the AI's reading are kept locally on our server (never shared) to improve the recognition quality, and are deleted immediately by /delete or at latest when storage limits require." ≥ Also: the PRD calls this an exception to a prior rule — the *user-facing* docs should state the current rule, not the history.

### PM8 · Known and accepted, but confirm the/product behavior (no code change requested)
- **Late reminders after resume/restart.** Pause doesn't cancel the pending send within the 3 h window: `/pause` 08:00 → `/resume` 11:00 → the 08:00 "before breakfast" reminder still fires (engine sends whenever `send_at ≤ now` unless the dose was already swept to `missed`). Bounded, arguably helpful, but it means the "one reminder per slot at the right time" promise has a documented-ish asterisk. The deep-dive says "a slot whose window passes while paused expires silently" — the 0–3 h sub-window behavior is unspecified. Decide + write it down (a line in README/PRD).
- **Course-finished silence.** "Reminders just stop" (deferred per PRD) is doubled-down by E3: nothing marks the course ended, so a user checking `/prescriptions` sees "active" forever. Cheap de-queer: when the sweep expires the *last pending dose* of a prescription, log it, and (founder call — v0.5) send the out-of-scope-but-one-line "That was the last dose of ₊ — course done ✅". Even without the message, fix E3 so the state is at least true.
- **"Skip today" on grouped reminders.** Label doesn't echo the "— all N" that ✅ shows; and for a group of 3 it skips 3 different medicines at once. Copy-tune when open item #2 is settled.
- **Group chats are first-class users.** Any group where the bot is added becomes a "patient" (one patient per chat per PRD is a convention, not enforced). Acceptable; if you want to fence it now, answer only `private` chats with a one-line "I work in one-on-one chats."

### PM9 · Assumptions audit (the "right assumptions" checklist you asked for)

| # | PRD/script assumption | Status in code |
|---|---|---|
| 1 | Chat ID = identity; all reads/edits only from the authenticated chat | ✅ True. Group-ack/cancel callbacks are inherently chat-scoped by Telegram's API; would still add a cheap `WHERE chat_id=?` belt-and-suspenders on `cancelrx:` (`handlers.py:494`) |
| 2 | Confirmation gate non-negotiable | ✅ Enforced everywhere a mutation is proposed; ⚠️ but confirmation-table buttons persist (PM6) and edit-confirm recompute regenerates ack groups (E5) |
| 3 | Medicine names are the OCR-weak part; structure is reliable | ⚠️ No structural trap in code, but E1 (server-tz day) and fractional-day truncation are *structural* correctness holes independent of OCR quality |
| 4 | "Reconciliation happens in the confirm step" (duplicates) | ❌ Not implemented (PM2) |
| 5 | Timezone shown at confirmation | ❌ Not implemented (PM3) |
| 6 | All AI failures share one generic voice | ✅ for AIError/ValidationError; ❌ for wrong-typed exceptions (E2) and unsupported content kinds (PM1) |
| 7 | "Offsets configurable, minutes only" | ⚠️ Contradicted by the 15-min grid validator + the onboarding example (PM4) |
| 8 | Ack idempotent; can never ack a future day | ✅ True (tests verify); ⚠️ but acks of stale groups silently no-op (E5) |
| 9 | `/pause` never blocks `/edit` | ✅ True |
| 10 | Expiry derives from duration → active vs expired | ❌ Status never computed (E3) |
| 11 | Photos retained (Rev A) for the golden set | ✅ Implemented; ⚠️ not disclosed to users in ToS (PM7) |
| 12 | One reminder per (day, slot, food); ≤6/day | ✅ True structurally; ⚠️ no cross-prescription dedup (parked, but PM2 makes it sharper) |
| 13 | Cost guardrails (AI budget, rate caps) | ✅ Implemented well; ⚠️ failed calls burn budget; per-model 404s waste the chain's only real model if the default names are wrong (E7) |
| 14 | Restart loses nothing | ✅ Queue/DB-backed; watermark dedup; ⚠️ at-most-once event handling (E6) |
| 15 | "58 tests pass" | ✅ Verified, they do — but they don't cover E1/E2/E4/E5/PM1/PM2 classes yet |

---

## 5. Prioritized recommendation list

**P0 — before any real user (all small, one PR):**
1. **E1** — `start_date`/`_new_prescription` use user-tz "today" (`today_local`). + regression test at 20:30 UTC.
2. **E3** — expiry sweep (`status='active' AND end_date < today → 'expired'`) so `/prescriptions` and the 10-rx cap are truthful.
3. **E2** — wrap parse errors as `AIError`; try next model on parse failure; catch-all in intake/edit that replies `GENERIC_FAILURE`. Add the invalid-JSON drill test the old report claims exists.

**P1 — the user-trust cohort, follow-up PR (still small):**
4. **PM1** — reply (kindly) to documents/non-text messages instead of silence.
5. **PM2** — duplicate-prescription warning line at confirm.
6. **E4** — `/resume <name>` copy honest about the master pause (and/or clear it).

**P2 — hardening + truthfulness of the surface (1–2 days):**
7. **E5** — deterministic `reminder_group` ids (uuid5) so edits don't orphan delivered buttons.
8. **PM3** — show timezone in the confirm table (wire up the dead `tz_display`).
9. **PM4** — decide offsets policy: allow non-grid minutes (recommended) or fix the example copy + validator message.
10. **PM5** — form-aware dose unit words (tablets/capsules/mg-ml text).
11. **PM6** — clear confirm buttons after Apply; `cancelrx` too.
12. **E6** — webhook: 403 on secret mismatch, reject default secret at startup when serving `/webhook/…`, keep a recent-`update_id` set instead of one watermark.
13. **PM7** — ToS data-retention sentence.
14. **E7** — verify the default model-name chain against the live API at boot; only count successful calls toward the daily budget.

**P3 — as touched during normal work:**
15. **E8** upsert user; **E9** lock-table eviction; fractional-day rounding rule; README drift fixes; `cancelrx:` chat_id check; PM8's late-reminder note; document at-most-once semantics.

**Process:** the two "spec smells" that generated bugs — (a) date/time computed from ambient server state instead of a passed-in clock+tz (only scheduling passes `now`; changesets doesn't), and (b) user-facing copy drift from validators (the 20-minute example) — are both worth a standing convention (inject `now_utc` everywhere; copy-review against validators before shipping examples).

---

## 6. Reconciling with the existing REVIEW_REPORT.md

The prior report is directionally positive and right about architecture and test *coverage breadth*; kept for reference. Corrections needed:

| Prior report claim | Reality |
|---|---|
| "✅ Comprehensive: try/catch at all external boundaries" | No — wrong-typed exceptions (JSONDecodeError, httpx.HTTPStatusError, KeyError) escape intake/edit handlers (E2) |
| "✅ Failure drills: AI invalid JSON, DB restart mid-Apply" | No such tests exist (grep across tests/); the invalid-JSON path is in fact broken (E2). The mid-Apply restart drill also isn't in the suite |
| "❌ Missing: Image Decompression Bomb Test" | Wrong — `test_compress_pixel_guard` exists and passes |
| "✅ Prescriptions — status … expired (computed)" | Never computed (E3) |
| "✅ Timezone … shown as 'Scheduling in IST'" | Never shown; `tz_display` is dead code (PM3) |
| "✅ /resume Un-pause (same, /resume Azithro or /resume)" | Master-pause hole (E4) |
| "✅ Edit onboarding 'Remind me 20 minutes before…'" |rejected by validator (PM4) |
| Overall "production-ready" | Close — but E1 (wrong course length) and E2 (silent loss) are exactly what "ready" must exclude. Fix the P0/P1 list first. |

---

## Appendix A — Repro outputs (executed 2026-01-20)

**Script 1 — malformed model JSON (`{ broken json`, braces-but-invalid, truncated):**
```
1)  escapes as: AIError (chain abandoned — next model never tried)
1b) truncated JSON raises: AIError
scenarios with braces-but-invalid content → JSONDecodeError (AIError? False);
models attempted: ['m1']  → fallback chain abandoned, raw JSONDecodeError escapes
handlers catch (AIError|ValidationError|TelegramError|ImageTooLarge) → NOT caught
→ webhook already 200 → Telegram won't redeliver → user: "📷 Got it…" then silence
```

**Script 2 — start date from server clock (user Asia/Kolkata, server UTC):**
```
user local date at confirm = 2026-01-21 (started 'today' at 02:00 IST)
stored start_date = 2026-01-20, end_date = 2026-01-24
dose_events for the '5-day' course = 4  (+ day-1 events silently dropped)
```

**Script 3 — offset contradiction:**
```
-20 min offset rejected: "offset must be on a 15-minute mark (…:00, …:15, …:30, …:45)"
bot's own onboarding copy suggests it: "Remind me 20 minutes before meals instead of 15"
```

*(Repro scripts: `tmp/verify_remed.py`, `tmp/verify_remed2.py` — run from repo root with the project venv on `PYTHONPATH`).*
