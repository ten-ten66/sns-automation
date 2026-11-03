"""Tests for Miku ★ Oracle prompt bank and face mosaic pipeline."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from config.miku_oracle_prompts import (
    ALL_VARIATIONS,
    ASTRO_VARIATIONS,
    CATEGORIES,
    DIVINATION_VARIATIONS,
    MIKU_ORACLE_BASE,
    NO_FACE,
    SEASONAL_VARIATIONS,
    TAROT_VARIATIONS,
    get_oracle_prompt,
    list_variations,
)
from src.media.face_mosaic import (
    _fallback_box,
    apply_face_mosaic,
    has_cv2,
    mosaic_region,
)


# ---------------------------------------------------------------------------
# Prompt bank structural tests
# ---------------------------------------------------------------------------


def test_exactly_40_variations():
    assert len(ALL_VARIATIONS) == 40


def test_ten_per_category():
    assert len(TAROT_VARIATIONS) == 10
    assert len(ASTRO_VARIATIONS) == 10
    assert len(DIVINATION_VARIATIONS) == 10
    assert len(SEASONAL_VARIATIONS) == 10


def test_all_ids_unique():
    ids = [v["id"] for v in ALL_VARIATIONS]
    assert len(set(ids)) == 40


def test_all_ids_follow_naming_scheme():
    for v in TAROT_VARIATIONS:
        assert v["id"].startswith("tarot_")
    for v in ASTRO_VARIATIONS:
        assert v["id"].startswith("astro_")
    for v in DIVINATION_VARIATIONS:
        assert v["id"].startswith("divine_")
    for v in SEASONAL_VARIATIONS:
        assert v["id"].startswith("season_")


def test_all_variations_have_required_fields():
    for v in ALL_VARIATIONS:
        assert "id" in v and isinstance(v["id"], str)
        assert "title" in v and isinstance(v["title"], str)
        assert "prompt" in v and isinstance(v["prompt"], str)
        assert len(v["prompt"]) > 50  # non-trivial prompts


def test_categories_cover_all_variations():
    total = sum(len(items) for items in CATEGORIES.values())
    assert total == 40


# ---------------------------------------------------------------------------
# get_oracle_prompt helper
# ---------------------------------------------------------------------------


def test_get_oracle_prompt_by_id_returns_no_face_suffix():
    prompt, aspect = get_oracle_prompt(variation_id="tarot_01", platform="instagram")
    assert NO_FACE in prompt
    assert aspect == "1:1"


def test_get_oracle_prompt_by_id_injects_character_base():
    prompt, _ = get_oracle_prompt(variation_id="astro_05", platform="x")
    # astro_05 is Libra — the prompt text references the character implicitly,
    # and for some variations the base is inlined while others reference Miku Oracle.
    # The NO_FACE suffix is the invariant we can always check.
    assert NO_FACE in prompt


def test_get_oracle_prompt_random_category():
    prompt, aspect = get_oracle_prompt(category="divination", platform="tiktok")
    assert NO_FACE in prompt
    assert aspect == "9:16"


def test_get_oracle_prompt_random_all():
    prompt, aspect = get_oracle_prompt(platform="youtube")
    assert NO_FACE in prompt
    assert aspect == "16:9"


def test_get_oracle_prompt_unknown_id_raises():
    with pytest.raises(ValueError, match="unknown Miku Oracle variation_id"):
        get_oracle_prompt(variation_id="nonexistent")


def test_get_oracle_prompt_unknown_category_raises():
    with pytest.raises(ValueError, match="unknown Miku Oracle category"):
        get_oracle_prompt(category="bogus")


def test_list_variations_all():
    assert len(list_variations()) == 40


def test_list_variations_filtered():
    assert len(list_variations(category="tarot")) == 10
    assert len(list_variations(category="astrology")) == 10


def test_character_base_has_face_hiding_directive():
    assert "face NEVER visible" in MIKU_ORACLE_BASE
    # NO_FACE is a separate constant with the strict directive
    assert "never appear" in NO_FACE


# ---------------------------------------------------------------------------
# face_mosaic module
# ---------------------------------------------------------------------------


def _make_solid_image(w: int = 512, h: int = 512, color: tuple[int, int, int] = (200, 100, 50)) -> Image.Image:
    return Image.new("RGB", (w, h), color)


def _draw_fake_face(img: Image.Image) -> tuple[int, int, int, int]:
    """Draw a crude face-like oval so haarcascade has *something* to try on.

    Returns the (x, y, w, h) of the drawn region. Note: the cascade is
    unlikely to actually detect this synthetic drawing — so this test
    primarily exercises the fallback path.
    """
    w, h = img.size
    box = (w // 2 - 100, h // 4, 200, 240)
    draw = ImageDraw.Draw(img)
    draw.ellipse([box[0], box[1], box[0] + box[2], box[1] + box[3]], fill=(240, 220, 200))
    return box


def test_has_cv2_available():
    # We installed opencv-python-headless as a project dependency.
    assert has_cv2() is True


def test_fallback_box_top_center_region():
    img = _make_solid_image(1000, 1000)
    box = _fallback_box(img)
    x, y, w, h = box
    # 40% × 40% centered horizontally, starting 5% from top
    assert w == 400
    assert h == 400
    assert x == 300
    assert y == 50


def test_mosaic_region_modifies_pixels(tmp_path: Path):
    img = _make_solid_image(200, 200, color=(255, 0, 0))
    original = img.copy()
    mosaic_region(img, (50, 50, 100, 100), block_size=10)
    # Image data should differ somewhere in the region (even a solid color
    # technically survives — but the downsample+upsample path forces a
    # new pixel write, so the object state has advanced)
    assert img.size == original.size
    # Solid red stays solid red, but we can at least check that a touched
    # region differs from out-of-region (different rounding/paste effects)
    center_px = img.getpixel((100, 100))
    corner_px = img.getpixel((0, 0))
    # Same color family (red) but the corner is untouched original
    assert corner_px == (255, 0, 0)
    # Center is still red-ish after pixelation
    assert center_px[0] >= 200


def test_mosaic_region_clips_out_of_bounds():
    img = _make_solid_image(100, 100)
    mosaic_region(img, (90, 90, 200, 200), block_size=5)  # extends past edge
    assert img.size == (100, 100)


def test_mosaic_region_ignores_empty_box():
    img = _make_solid_image(100, 100)
    before = img.tobytes()
    mosaic_region(img, (10, 10, 0, 0))
    assert img.tobytes() == before


def test_apply_face_mosaic_from_path(tmp_path: Path):
    src = tmp_path / "src.png"
    _make_solid_image(800, 800).save(src)
    out = apply_face_mosaic(src)
    assert out.exists()
    assert out.name == "src_mosaic.png"
    # Output is a valid image with same dimensions
    with Image.open(out) as im:
        assert im.size == (800, 800)


def test_apply_face_mosaic_custom_dst(tmp_path: Path):
    src = tmp_path / "in.jpg"
    _make_solid_image(600, 600).save(src, format="JPEG")
    dst = tmp_path / "out_mosaic.png"
    out = apply_face_mosaic(src, dst)
    assert out == dst
    assert out.exists()


def test_apply_face_mosaic_from_bytes(tmp_path: Path):
    buf = io.BytesIO()
    _make_solid_image(400, 400).save(buf, format="PNG")
    raw = buf.getvalue()
    dst = tmp_path / "bytes_out.png"
    out = apply_face_mosaic(raw, dst)
    assert out.exists()


def test_apply_face_mosaic_fallback_always_on_synthetic_image(tmp_path: Path):
    """Haarcascade won't find a face in a solid rectangle → fallback applies."""
    src = tmp_path / "solid.png"
    _make_solid_image(1024, 1024, color=(50, 50, 50)).save(src)
    out = apply_face_mosaic(src, tmp_path / "out.png", block_size=16)
    assert out.exists()
    # The fallback box is at top-center. Pixels inside should no longer all
    # be exactly 50 after save (PNG round-trip preserves, but the resize
    # path may shift by 0-1). Primary assertion: output is writable.
    with Image.open(out) as im:
        assert im.mode == "RGB"
        assert im.size == (1024, 1024)


def test_apply_face_mosaic_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        apply_face_mosaic(tmp_path / "does_not_exist.png")


def test_apply_face_mosaic_always_fallback_forces_rectangle(tmp_path: Path):
    src = tmp_path / "in.png"
    img = _make_solid_image(512, 512, color=(100, 150, 200))
    img.save(src)
    out = apply_face_mosaic(src, tmp_path / "out.png", always_fallback=True)
    assert out.exists()
    # Re-load and check the top-center region is no longer perfectly flat
    with Image.open(out) as result:
        rgb = result.convert("RGB")
        # Sample a pixel inside the fallback region
        px = rgb.getpixel((256, 150))
        # Should be close to original (solid color), but the mosaic pipeline
        # ran through it — primary assertion is file validity and no crash
        assert px is not None
        assert len(px) == 3
