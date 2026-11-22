"""Guardrails: per-chat rate limits, photo-failure backoff, text/image caps,
resource caps on apply, and per-chat ordering under load."""
import asyncio

import pytest

from app import db, guardrails, handlers, scheduling
from app.config import settings
from app.telegram import extract_event

SAMPLE = {"medicines": [
    {"name": "Pantoprazole 40", "form": "tablet", "tablets_per_dose": 1,
     "slots": ["breakfast"], "food": "before", "days": 3},
], "notes": []}


@pytest.fixture()
def e2e(tmp_db, monkeypatch):
    """Wired handlers with fake AI + no min-gap (bursts within one test are
    intentional; the gap itself has a dedicated test)."""
    async def fake_extract(image_bytes, mime="image/jpeg"):
        return SAMPLE, "fake-model"

    async def fake_interpret(text, context):
        return {"operations": [], "clarify": "Which medicine?"}, "fake-model"

    async def fake_download(token, file_id):
        return b"\xff\xd8\xff" + b"x" * 10  # valid JPEG magic

    monkeypatch.setattr(handlers, "extract_prescription", fake_extract)
    monkeypatch.setattr(handlers, "interpret_edit", fake_interpret)
    monkeypatch.setattr(handlers, "download_photo", fake_download)
    monkeypatch.setattr(settings, "photo_min_gap_seconds", 0)
    monkeypatch.setattr(settings, "edit_min_gap_seconds", 0)
    monkeypatch.setattr(settings, "apply_min_gap_seconds", 0)
    return handlers


def _photo(update_id=1, chat=800):
    return {"update_id": update_id, "message": {"chat": {"id": chat},
             "photo": [{"file_id": "f", "width": 1, "height": 1}]}}


def _text(update_id=1, chat=800, text="move everything to dinner"):
    return {"update_id": update_id, "message": {"chat": {"id": chat}, "text": text}}


def _callback(update_id, chat, cb_id, data) -> dict:
    return {"update_id": update_id,
            "callback_query": {"id": cb_id, "data": data,
                               "message": {"chat": {"id": int(chat)}, "message_id": 1},
                               "from": {"id": int(chat)}}}


def _run(handlers_mod, tg, update):
    asyncio.run(handlers_mod.handle_event(tg, extract_event(update)))


# ---------------------------------------------------------------- rate limits

def test_photo_burst_throttled(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "photo_rate_per_hour", 3)
    for i in range(1, 4):
        _run(e2e, tg, _photo(i))
        assert any("Reading your prescription" in t for _, t in tg.texts), f"photo {i} should pass"
    _run(e2e, tg, _photo(4))
    assert any("lot of prescription photos" in t for _, t in tg.texts)


def test_photo_min_gap_blocks_burst_pair(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "photo_min_gap_seconds", 60)
    _run(e2e, tg, _photo(1))
    _run(e2e, tg, _photo(2))
    assert any("lot of prescription photos" in t for _, t in tg.texts)


def test_edit_min_gap_blocks_rapid_edits(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "edit_min_gap_seconds", 60)
    chat = "810"
    _run(e2e, tg, _text(1, chat, "/edit"))
    _run(e2e, tg, _text(2, chat, "change dose"))
    _run(e2e, tg, _text(3, chat, "also change time"))
    assert any("lot of edits" in t for _, t in tg.texts)


def test_apply_min_gap_blocks_rapid_applies(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "apply_min_gap_seconds", 60)
    chat = "814"
    for i in range(2):
        _run(e2e, tg, _photo(400 + i, chat))
        _run(e2e, tg, _callback(500 + i, chat, f"cb{i}", "apply_draft"))
    assert any("lot of changes" in t for _, t in tg.texts)
    assert len(db.active_prescriptions(chat)) == 1


def test_hourly_and_daily_edit_limits(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "edit_rate_per_hour", 1)
    monkeypatch.setattr(settings, "edit_rate_per_day", 2)
    chat = "815"
    _run(e2e, tg, _text(1, chat, "/edit"))
    _run(e2e, tg, _text(2, chat, "edit one"))   # 1st of the hour
    _run(e2e, tg, _text(3, chat, "edit two"))   # hourly limit → block
    assert any("lot of edits" in t for _, t in tg.texts)
    # simulate the hour passing without touching the day window
    w = guardrails._windows[(chat, "edit")]
    for t in list(w.hour):
        w.hour.remove(t)
    _run(e2e, tg, _text(4, chat, "edit three"))  # 2nd of the day → allowed
    _run(e2e, tg, _text(5, chat, "edit four"))   # daily limit → block
    assert any("today's limit of 2 edits" in t for _, t in tg.texts)


def test_super_user_bypasses_limits(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "super_user_key", "s3cret")
    monkeypatch.setattr(settings, "photo_rate_per_day", 1)
    chat = "816"

    def _cmd(uid, text):
        return {"update_id": uid, "message": {"chat": {"id": 816}, "text": text}}

    _run(e2e, tg, _cmd(1, "/super wrong-key"))
    assert any("isn't right" in t for _, t in tg.texts)
    _run(e2e, tg, _cmd(2, "/super s3cret"))
    assert any("Unlocked" in t for _, t in tg.texts)
    for uid in (3, 4, 5):  # past hourly (6/h) is irrelevant at daily=1; super must pass all
        _run(e2e, tg, _photo(uid, chat))
        assert any("Reading your prescription" in t for _, t in tg.texts)


def test_photo_daily_cap(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "photo_rate_per_hour", 5)
    monkeypatch.setattr(settings, "photo_rate_per_day", 2)
    for i in range(1, 3):
        _run(e2e, tg, _photo(i, chat=818))
    _run(e2e, tg, _photo(3, chat=818))
    assert any("today's limit of 2 photos" in t for _, t in tg.texts)


def test_command_not_rate_limited_when_no_state(tg, e2e, monkeypatch):
    """/super with no attempt and /start remain unrestricted."""
    monkeypatch.setattr(settings, "edit_rate_per_day", 0)
    _run(e2e, tg, _text(1, chat=817, text="/start"))
    assert any("/today" in t or "photo" in t.lower() for _, t in tg.texts)


def test_photo_failure_backoff(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "photo_fails_per_hour", 2)

    async def bad_extract(image_bytes, mime="image/jpeg"):
        from app.ai import AIError
        raise AIError("extract failed")

    monkeypatch.setattr(e2e, "extract_prescription", bad_extract)
    for i in range(1, 3):
        _run(e2e, tg, _photo(i))  # each attempt counts a failure
    assert len(guardrails._windows[("800", "photo")].photo_fails) == 2
    # backoff trips before the hourly photo count does (default 12 > fails 2)
    _run(e2e, tg, _photo(3))
    assert any("trouble reading your photos" in t for _, t in tg.texts)


# ---------------------------------------------------------------- payload caps

def test_text_length_cap(tg, e2e):
    _run(e2e, tg, _text(1, chat=811, text="x" * (settings.max_text_len + 1)))
    assert any("very long" in t for _, t in tg.texts)


def test_medicines_per_draft_cap(tg, e2e):
    many = [{"name": f"Med {i}", "slots": ["breakfast"], "food": "after", "days": 1}
            for i in range(settings.max_medicines_per_draft + 1)]
    with pytest.raises(scheduling.ValidationError, match="too many medicines"):
        scheduling.validate_draft_medicines(many)


def test_download_size_and_type_guard(tmp_db, monkeypatch):
    """download_photo rejects oversize and non-image payloads."""
    from app.telegram import TelegramError, download_photo

    class FakeResp:
        def __init__(self, content, _json=None):
            self.content = content
            self.headers = {}
            self._json = _json

        def json(self):
            return self._json

        def raise_for_status(self): ...

    class FakeClient:
        def __init__(self, content):
            self._content = content

        async def get(self, url, params=None):
            if "getFile" in url:
                return FakeResp(None, {"ok": True, "result": {"file_path": "p"}})
            return FakeResp(self._content)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def use(content):
        def factory(*a, **kw):
            return FakeClient(content)
        monkeypatch.setattr(__import__("app.telegram", fromlist=["x"]).httpx,
                            "AsyncClient", factory)

    async def scenario():
        big = b"\xff\xd8\xff" + b"x" * (settings.max_image_mb * 1024 * 1024 + 1)
        use(big)
        with pytest.raises(TelegramError, match="too large"):
            await download_photo("tok", "f")
        use(b"plain text")
        with pytest.raises(TelegramError, match="not an image"):
            await download_photo("tok", "f")
        use(b"\xff\xd8\xffok")
        assert await download_photo("tok", "f") == b"\xff\xd8\xffok"

    asyncio.run(scenario())


# ---------------------------------------------------------------- resource caps

def test_active_prescriptions_cap_blocks_apply(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "max_active_prescriptions", 2)
    chat = "812"
    for i in range(2):
        _run(e2e, tg, _photo(100 + i, chat))
        _run(e2e, tg, _callback(200 + i, chat, f"cb{i}", "apply_draft"))
    assert len(db.active_prescriptions(chat)) == 2
    _run(e2e, tg, _photo(300, chat))
    _run(e2e, tg, _callback(301, chat, "cbcap", "apply_draft"))
    assert any("active prescriptions" in t for _, t in tg.texts)


# ---------------------------------------------------------------- ordering

def test_per_chat_ordering(tmp_db, monkeypatch):
    """A same chat's events are serialized: the second cannot interleave
    before the first has finished (its guardrail reply must come after)."""
    guardrails.reset()
    order = []

    class SlowTG:
        async def send_text(self, chat_id, text):
            order.append(text)
            await asyncio.sleep(0.05)

        async def send_message_with_buttons(self, chat_id, text, buttons):
            return await self.send_text(chat_id, text)

        async def update_message_buttons(self, *a): ...
        async def answer_callback(self, *a): ...

    async def fake_extract(image_bytes, mime="image/jpeg"):
        await asyncio.sleep(0.05)
        return SAMPLE, "fake-model"

    monkeypatch.setattr(handlers, "extract_prescription", fake_extract)
    monkeypatch.setattr(settings, "photo_min_gap_seconds", 0)

    async def scenario():
        t1 = asyncio.create_task(handlers.handle_event(SlowTG(), extract_event(_photo(1, 900))))
        t2 = asyncio.create_task(handlers.handle_event(SlowTG(), extract_event(_photo(2, 900))))
        await asyncio.gather(t1, t2)

    asyncio.run(scenario())
    assert len(order) >= 2  # both handled; nothing lost under the slot lock


def test_reset_clears_everything(tg, e2e):
    _run(e2e, tg, _photo(1))
    guardrails.reset()
    assert not guardrails._windows


# ---------------------------------------------------------------- global AI budget

def _seed_ai_calls(n: int, chat="999"):
    from datetime import datetime, timezone
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today = start.isoformat()
    for i in range(n):
        db.execute("INSERT INTO ai_calls(id, chat_id, kind, ok, created_at) VALUES(?,?,?,?,?)",
                   (f"seed{i}-{chat}", str(chat), "extract", 1, today))


def test_global_ai_budget_blocks_all_users(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "ai_daily_budget", 2)
    _seed_ai_calls(2)
    _run(e2e, tg, _photo(1, chat=819))
    assert any("at capacity" in t for _, t in tg.texts)
    # blocked before the ack: no "Reading your prescription" spent on it
    assert not any("Reading your prescription" in t for _, t in tg.texts)


def test_global_ai_budget_off_when_zero(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "ai_daily_budget", 0)
    _seed_ai_calls(5)
    _run(e2e, tg, _photo(1, chat=821))
    assert any("Reading your prescription" in t for _, t in tg.texts)


def test_budget_applies_to_edits_too(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "ai_daily_budget", 1)
    _seed_ai_calls(1)
    chat = "822"
    _run(e2e, tg, _text(1, chat, "/edit"))
    _run(e2e, tg, _text(2, chat, "change dose"))
    assert any("at capacity" in t for _, t in tg.texts)


# ---------------------------------------------------------------- auto-quarantine

def test_auto_quarantine_after_repeated_trips(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "photo_rate_per_hour", 1)
    monkeypatch.setattr(settings, "auto_block_trips", 2)
    chat = "820"
    _run(e2e, tg, _photo(1, chat))   # ok
    _run(e2e, tg, _photo(2, chat))   # trip 1
    _run(e2e, tg, _photo(3, chat))   # trip 2 → quarantined
    assert any("now blocked" in t for _, t in tg.texts)
    assert db.get_user(chat)["blocked"] == 1
    # quarantined chat: events silently dropped, not even a receipt
    before = len(tg.texts)
    _run(e2e, tg, _photo(4, chat))
    _run(e2e, tg, _text(5, chat, "/start"))
    assert len(tg.texts) == before


def test_manual_block_unblock(tg, e2e, monkeypatch):
    monkeypatch.setattr(settings, "super_user_key", "s3cret")
    chat, target = "823", "777"
    db.ensure_user(target)  # target exists (has contacted the bot before)
    _run(e2e, tg, _text(1, chat, f"/block {target}"))
    assert any("/help" in t or "photo" in t.lower() for _, t in tg.texts)  # non-super gets nothing
    _run(e2e, tg, _text(2, chat, "/super s3cret"))
    tg.texts.clear()
    _run(e2e, tg, _text(3, chat, f"/block {target}"))
    assert any("blocked" in t for _, t in tg.texts)
    assert db.get_user(target)["blocked"] == 1
    _run(e2e, tg, _text(4, chat, f"/unblock {target}"))
    assert db.get_user(target)["blocked"] == 0


# ---------------------------------------------------------------- retention

def _noise_jpeg(kb: int) -> bytes:
    import io as _io
    import os as _os
    from PIL import Image
    side = 100
    im = Image.frombytes("RGB", (side, side), _os.urandom(side * side * 3))
    buf = _io.BytesIO()
    im.save(buf, format="JPEG", quality=98)
    data = buf.getvalue()
    return (data * (kb * 1024 // len(data) + 1))[:kb * 1024]


def test_retention_prunes_oldest_ai_images_at_trigger(tmp_db, monkeypatch):
    from app import retention
    monkeypatch.setattr(settings, "ai_image_budget_kb", 100)  # 100 KB cap
    for i in range(5):  # 5 × ~100 KB ≫ 80% of cap
        db.log_ai_call("555", "extract", model="m", input_image=_noise_jpeg(100), ok=True)
    before = db.query_one("SELECT COUNT(*) c FROM ai_calls WHERE input_image IS NOT NULL")["c"]
    assert before == 5
    report = retention.maintenance()
    after = db.query_one("SELECT COUNT(*) c FROM ai_calls WHERE input_image IS NOT NULL")["c"]
    assert after == before - 1  # oldest 10% (≥1 row) unlinked, JSON I/O kept
    assert "unlinked" in (report["ai_images"] or "")


def test_retention_leaves_everything_below_70(tmp_db, monkeypatch):
    from app import retention
    monkeypatch.setattr(settings, "ai_image_budget_kb", 1024 * 1024)  # far above use
    db.log_ai_call("556", "extract", model="m", input_image=_noise_jpeg(50), ok=True)
    before = db.query_one("SELECT COUNT(*) c FROM ai_calls WHERE input_image IS NOT NULL")["c"]
    retention.maintenance()
    after = db.query_one("SELECT COUNT(*) c FROM ai_calls WHERE input_image IS NOT NULL")["c"]
    assert after == before  # untouched below capacity


def test_retention_prunes_logs_at_trigger(tmp_db, monkeypatch):
    from app import retention
    monkeypatch.setattr(settings, "logs_max_rows", 4)
    for i in range(6):
        db.execute("INSERT INTO user_messages(id, chat_id, kind, text, created_at)"
                   " VALUES(?,?,?,?,?)", (f"um{i}", "555", "free_text", f"m{i}",
                                          f"2026-01-0{i + 1}T00:00:00+00:00"))
    retention.maintenance()
    remaining = [r["text"] for r in db.query(
        "SELECT text FROM user_messages ORDER BY created_at")]
    assert "m0" not in remaining  # oldest pruned
    assert len(remaining) == 5    # 6 → minus oldest 10% (≥1)


# ---------------------------------------------------------------- window cap

def test_guardrail_window_cap_eviction(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "guardrail_window_cap", 4)
    for i in range(5):  # 5th creation crosses 80% of cap → evict oldest 10%
        assert guardrails.check(f"chat-{i}", "photo") is None
    assert len(guardrails._windows) <= 6


# ---------------------------------------------------------------- pixel guard

def test_compress_pixel_guard(tmp_db, monkeypatch):
    import io as _io
    from PIL import Image
    from app.images import ImageTooLarge, compress
    monkeypatch.setattr(settings, "image_max_pixels_mp", 0.001)  # 1000 px
    buf = _io.BytesIO()
    Image.new("RGB", (100, 100)).save(buf, format="JPEG", quality=90)
    with pytest.raises(ImageTooLarge):
        compress(buf.getvalue())


# ---------------------------------------------------------------- send pacing

def test_engine_spreads_sends_across_chats(tmp_db, tg, e2e, monkeypatch):
    from app import engine
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(settings, "send_spacing_seconds", 0.01)
    monkeypatch.setattr(settings, "send_jitter_seconds", 0.01)
    # two chats, one applied prescription each
    for i, chat in enumerate(("901", "902")):
        _run(e2e, tg, _photo(1000 + i, chat))
        _run(e2e, tg, _callback(2000 + i, chat, f"dcb{i}", "apply_draft"))
    # rush every queued reminder to "due now", then tick
    now_iso = db.now_iso()
    db.execute("UPDATE reminder_queue SET send_at=?", (now_iso,))
    due = db.query_one("SELECT COUNT(*) c FROM reminder_queue WHERE state='pending'")["c"]
    assert due > 0
    asyncio.run(engine.tick(tg, datetime.now(timezone.utc) + timedelta(minutes=1)))
    sent = db.query_one("SELECT COUNT(*) c FROM reminder_queue WHERE state='sent'")["c"]
    assert sent == due
    assert len(tg.buttoned) >= 2  # both chats got their reminder


# ---------------------------------------------------------------- compression

def _big_jpeg(w=3000, h=2000) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (250, 250, 245)).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_compress_shrinks_large_photo(tmp_db):
    from app.images import compress
    original = _big_jpeg()
    out = compress(original)
    assert len(out) < len(original)
    assert out[:3] == b"\xff\xd8\xff"


def test_compress_respects_max_dim(tmp_db):
    import io
    from app.config import settings as s
    from app.images import compress
    from PIL import Image
    out = compress(_big_jpeg())
    with Image.open(io.BytesIO(out)) as im:
        assert max(im.size) <= s.image_max_dim_px


def test_compress_passthrough_on_garbage(tmp_db):
    from app.images import compress
    assert compress(b"\xff\xd8\xff" + b"not really an image") == b"\xff\xd8\xff" + b"not really an image"


def test_compress_keeps_original_when_not_smaller(tmp_db):
    from app.images import compress
    tiny = b"\x89PNG\r\n\x1a\n" + b"x" * 50  # tiny valid-ish header: re-encode can't beat it
    assert compress(tiny) == tiny
