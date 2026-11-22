# ReMed — reminders for medicines (Telegram MVP)

Send a photo of a doctor's prescription → the bot reads it into a structured, confirmable schedule → sends one well-timed reminder per dose until the course ends. No app install, no accounts: the Telegram chat is the product and the chat ID is the identity.

Built against `docs/tech-design-deep-dive.md` (tables, state machines, AI contracts), with `docs/conversation-script-v0.2.md` as the message surface and `docs/prd-v0.4.md` as the scope fence.

## Where things live

```
ReMed/
├── app/                          # the bot (Python) — nothing else imports from here
│   ├── prompts/                  #   the 2 AI prompts, one file each (edit freely)
│   ├── rendering/                #   message templates, one module per channel
│   │   └── telegram.py           #     (all copy today; whatsapp.py is the parked v2)
│   ├── config.py … main.py       #   config → db → scheduling → changesets → ai →
│   │                             #   rendering → handlers → engine → main
├── tests/                        # 72 pytest tests (simulated clock, fake Telegram)
├── scripts/
│   └── simulate_chat.py          # regenerates artifacts/chat-simulation.html
├── docs/                         # the specs (read in this order for a fresh start)
│   ├── remed-flowchart.md        #   1 · visual orientation map
│   ├── prd-v0.4.md               #   2 · scope fence (what's deliberately out)
│   ├── conversation-script-v0.2.md  # 3 · exact user-facing messages
│   ├── tech-design-v0.1.md       #   4 · architecture one-pager
│   ├── tech-design-deep-dive.md  #   5 · full reference (authoritative)
│   └── history/                  # decision trail only — do not build from
│       └── med-reminder-brainstorming.md
├── landing/                      # static marketing site, remit.appsforlife.app
│   ├── index.html                #   landing page (Telegram CTA deep link)
│   ├── terms.html                #   Terms of Service
│   ├── assets/style.css          #   shared styles
│   └── Dockerfile                #   nginx static server — deploy via Coolify
└── artifacts/                    # viewable outputs
    ├── chat-simulation.html      # simulated user journey, exact bot output
    └── remed-flowchart.html      # as-built system flowchart
```

## Status — first working slice (MVP core, tested)

- ✅ Photo intake → AI extraction → validation → draft → **confirmation gate** → Apply → whole course expanded into `dose_events` + precomputed `reminder_queue` (deep dive §5 scheduling math; no send-time math, DST-safe, past times never re-sent). Start date is the **user's calendar day in their timezone** (never the server clock — 00:00–05:30 IST must not shorten a course), fractional-day quantities round up, and a re-sent photo of an already-covered medicine shows a "you already have active reminders covering…" warning at the confirm gate instead of silently double-scheduling.
- ✅ Reminder engine: 15-min DB-backed loop (`TICK_SECONDS`, all times on a 15-minute grid), delivery ack, ≤3 silent retries, silent missed after the window, pause-aware (pause is prescription-level: `prescriptions.paused` + user master switch; `/pause Azithro` pauses one, `/pause` pauses all), and an **expiry sweep**: once a user's local date passes a course's `end_date`, its status flips to `expired` (shown as "ended" in /prescriptions) and it stops counting toward the active-prescription cap. Restarts lose nothing (queue lives in SQLite).
- ✅ Taken/Skip buttons → **silent ack** (no reply message, buttons clear, idempotent — a replayed press is ignored; ✅ can never ack a future day's dose).
- ✅ Edit Prescription: free text → AI change-set against the current object → ambiguity → one clarifying question → FULL updated table → Apply → only future pending rows recomputed; taken/skipped history immutable.
- ✅ Commands: /start /today /edit /prescriptions /pause /resume /cancel /delete /help. Stray free text honored as "type it in your own words" input. Webhook dedup by `update_id`; typed-YES `/delete` wipes everything in one transaction.
- ✅ **Guardrails (anti-abuse, `app/guardrails.py`):** per-chat sliding-window rate limits — photos 6/hour + 24/day + 60s gap (each is a paid vision call + a persisted BLOB), edits 6/hour + 24/day + 60s gap (paid interpret calls), applies same as edits; backoff after repeated unreadable photos; max text length; image size + JPEG/PNG magic-byte check on download; pixel-count (decompression-bomb) guard before decode; caps on medicines per draft and active prescriptions per user so one apply can't flood the queue; bounded handler concurrency with per-chat ordering. A chat that sends `/super <SUPER_USER_KEY>` (env; empty = off) bypasses per-chat limits — for founder testing. **Global AI daily budget** (`AI_DAILY_BUDGET`, default 800): when today's `ai_calls` rows hit it, AI calls short-circuit with an "at capacity" message — protects the one shared free-tier Gemini key every user spends from. **Auto-quarantine**: AUTO_BLOCK_TRIPS guardrail blocks/hour → chat marked blocked (events silently ignored); founder `/block <chat_id>` / `/unblock` (requires `/super` verification). Reminders are send-paced (spacing + deterministic per-chat jitter inside the tick) so the whole user base's identical default meal grid can't burst Telegram's 30 msg/s limit.
- ✅ **Capacity-triggered retention (`app/retention.py`, founder policy):** no age-based pruning — stores grow freely until 70% of their cap (warning logged), at 80% the oldest 10% is dropped: `ai_calls.input_image` by bytes (`AI_IMAGE_BUDGET_KB`, default 1 GB — export the golden set first, pruned images never come back), `user_messages` / `activity_log` by rows (`LOGS_MAX_ROWS`, default 1M), guardrail window table likewise in memory. Runs once/day off the engine tick; auto_vacuum=INCREMENTAL (new DBs) returns freed pages to the OS; daily DB-size + row-count log line.
- Photos are downscaled/re-encoded (Pillow, `IMAGE_MAX_DIM_PX` 1600 / `IMAGE_JPEG_QUALITY` 85, `app/images.py`) *before* the AI call and before persisting — fewer vision tokens, faster uploads, ~5-10x smaller golden-set blobs, and the stored bytes are exactly what the model saw. All tunable via env. Rate windows in-memory (single process); a restart resets them (blocked/super flags persist in the DB).
- ⛔ Not built (deliberately, per design decision doc): course/expiry notifications, catch-up messages, time-zone change prompts, merged multi-prescription reminders, WhatsApp adapter.

**72 tests** pass (`pytest tests/`): scheduling math, template shapes, change-set all-or-nothing validation, 15-minute-grid rules, prescription-level pause, and simulated-clock end-to-end flows (photo→apply→remind→ack→missed; /edit→apply→recompute) plus webhook dedup/secret and retry-expiry drills — including regressions for the review fixes: user-tz start dates, fractional-day rounding, AI-chain fall-through on malformed model output, honest named-`/resume` under master pause, expired-status sweep vs the active cap, duplicate-coverage warning, and cancel-button ownership.

**Observability tables** (not user-facing): `ai_calls` — every AI call with input, output, model, latency, ok/error (photo bytes included, kept deliberately for the golden dataset — the one privacy exception, now PRD rev A); `user_messages` — every inbound command / free text / photo; `activity_log` — every prescription change (created / updated / cancelled / paused / resumed) with a short detail. All wiped by `/delete`.

**Prompts** live one file each under `app/prompts/` (`extract.md`, `interpret_edit.md`) and are loaded by `app/ai.py` — edit them without touching code.

`artifacts/chat-simulation.html` is a generated artifact (script: `scripts/simulate_chat.py`) showing exact bot outputs for a full simulated user journey.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env         # then fill BOT_TOKEN + GOOGLE_AI_API_KEY (both need founder accounts)
.venv/bin/python -m app.polling          # local dev: long-polling, no TLS needed
```

Production (webhook mode): run `uvicorn app.main:app` behind TLS (see `docker-compose.yml`; Caddy or any proxy terminates HTTPS), then register once:

```bash
python -m app.webhook_setup https://your-domain/webhook/<WEBHOOK_SECRET>
```

`GET /health` for uptime checks; the reminder loop is the dead-man's-snitch candidate (ops: deep dive §10).

## Secrets the founder must supply (nothing else is external)

1. **Telegram bot token** — @BotFather → /newbot (check `@ReMedBot` availability at creation; open item #1).
2. **AI provider key** — Google Gemini by default (OpenAI-compatible endpoint): set `GOOGLE_AI_API_KEY` from [AI Studio](https://aistudio.google.com/apikey). The model fallback chain (`AI_MODELS`, comma-separated, tried in order) moves to the next model on 429/5xx/timeouts, unknown-model 404s, and malformed/unparseable output; only hard 4xx (bad key, malformed request) aborts the chain. Defaults to `gemini-3.5-flash-lite → gemini-3.1-flash-lite → gemini-2.5-flash-lite`. Any OpenAI-compatible vision-chat endpoint works via `AI_BASE_URL`/`AI_API_KEY`. Used for exactly two call types: image extraction and edit interpretation; the AI only proposes, ordinary code validates, and nothing schedules without your explicit confirm.
3. **Super-user key** (optional) — any secret string in `SUPER_USER_KEY`; a chat sends `/super <key>` once to lift all rate limits for itself (testing). Leave unset to disable.

## Layout

```
app/
  config.py       env-driven settings (meals/offsets/tz are per-user values in DB; these are defaults)
  db.py           SQLite, 6 tables, plain SQL, WAL; conversation state with TTL
  scheduling.py   expand/recompute math: meal anchor ± offset → UTC; skip-past rule; answered-dose guard
  changesets.py   AI-proposed op validation (ambiguity→clarify), snapshot apply, one-transaction persist
  ai.py           2 contracts: extract (image→JSON), interpret_edit (text+state→change-set)
  rendering.py    every user-facing message (conversation-script-v0.2 templates)
  handlers.py     router + flows: intake (A), edit (B), commands, callbacks
  engine.py       reminder loop: due pick → render → send → ack; silent missed sweep
  main.py         FastAPI webhook (secret token + update_id dedup) + /health
  polling.py      local-dev entry (long-polling)
  telegram.py     the only module that knows the Bot API (WhatsApp-ready adapter boundary)
tests/            pytest suite (simulated clock; FakeTelegram adapter)
```

## Design decisions worth knowing

- **Timezone**: the Bot API does not expose the user's timezone, so new users get `DEFAULT_TIMEZONE` (shown at confirmation, e.g. "Scheduling in IST (UTC+5:30)") and can change it via Edit Prescription ("I moved to New York") — per deep dive §5 the change recomputes future rows only.
- **Grouped reminders**: one message per (day, slot, food); grouped slots share one "✅ Taken — all N" button (open item #2 currently: combined).
- **Log hygiene**: no medicine names or message contents in log lines; chat_id allowed (support).
- **Mid-day confirms** never burst old reminders: expansion skips times already past; a dose the user explicitly took/skipped is never re-reminded even if meal times change later.
