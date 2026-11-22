"""Image compression — one place, applied everywhere an image is used.

Every prescription photo is (a) sent to a vision model and (b) persisted as a
BLOB in ai_calls. Camera originals are 3–10 MB multi-megapixel JPEGs; medicine
labels need only a fraction of that. We downscale to IMAGE_MAX_DIM_PX on the
long edge and re-encode as JPEG at IMAGE_JPEG_QUALITY *before* both uses:

- for the AI: vision APIs charge image input by resolution (Gemini tiles an
  image on a fixed grid — smaller images = fewer tiles = fewer tokens), and
  upload latency drops proportionally;
- for storage: the golden-set BLOBs shrink ~5-10x, and what we keep is exactly
  what the model saw — the golden dataset stays honest.

If Pillow is missing or the bytes won't decode, we pass the original through —
better an unoptimized call than a lost prescription.
"""
import io
import logging

from .config import settings

log = logging.getLogger("remed.images")


class ImageTooLarge(Exception):
    """Decoded pixel count over IMAGE_MAX_PIXELS_MP — treated like any other
    unreadable photo by the intake flow (counts toward failure backoff)."""


def compress(image: bytes) -> bytes:
    """Downscale + re-encode a photo. Returns the better of (original,
    compressed) so re-encoding never *increases* size. Raises ImageTooLarge
    when the pixels alone would blow RAM (header check — no decode happens
    before this)."""
    if not image:
        return image
    try:
        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        with Image.open(io.BytesIO(image)) as im:
            w, h = im.size  # header only — image not decoded yet
            max_pixels = settings.image_max_pixels_mp * 1_000_000
            if w * h > max_pixels:
                raise ImageTooLarge(f"{w}x{h} = {w*h} px over {settings.image_max_pixels_mp}MP cap")
            max_dim = settings.image_max_dim_px
            if max(w, h) > max_dim:
                scale = max_dim / max(w, h)
                im = im.resize((round(w * scale), round(h * scale)),
                               Image.LANCZOS)  # crisp small text matters here
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=settings.image_jpeg_quality, optimize=True)
        compressed = out.getvalue()
        return compressed if len(compressed) < len(image) else image
    except ImageTooLarge:
        raise
    except Exception as e:
        log.warning("image compression skipped (%s) — using original", type(e).__name__)
        return image
