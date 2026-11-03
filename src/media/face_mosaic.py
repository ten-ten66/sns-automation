"""Face-mosaic post-processing for generated images.

The Miku ★ Oracle content pipeline generates faceless images by prompt
design (see ``config/miku_oracle_prompts.py`` — every prompt uses back
shots, hair covering, hoods, silhouettes, etc.). This module is the
**third line of defense**: even if Kie.ai occasionally renders a face
anyway, we pixelate it before the image is published.

Detection: OpenCV's Haar cascade ``haarcascade_frontalface_default.xml``
(ships with ``opencv-python-headless``). If no face is detected — or if
``cv2`` is not importable for any reason — we still apply a defensive
"top-center rectangle" mosaic covering the region where a face would
usually appear. That way the output is *never* face-visible regardless
of model compliance.

Public API
----------

``apply_face_mosaic(src, dst=None, *, block_size=24, always_fallback=False)``
    Load an image from ``src`` (path or bytes), pixelate any detected
    face, save to ``dst`` (defaults to ``<src stem>_mosaic.<ext>``), and
    return the output ``Path``.

``mosaic_region(image, box, block_size=24)``
    Low-level helper: pixelate a single ``(x, y, w, h)`` rectangle of a
    ``PIL.Image.Image`` in place.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

# Minimum face size in pixels — prevents tiny false positives from ruining
# the entire image. Tuned to match Nano Banana Pro 1024x1024 output.
_MIN_FACE_PX = 48

# Minimum coverage ratio of the naive fallback rectangle if no face found.
# (0.28 means: 28% of width centered horizontally, 40% of height starting
# near the top — a region where a portrait's face almost always lies.)
_FALLBACK_WIDTH_RATIO = 0.40
_FALLBACK_HEIGHT_RATIO = 0.40
_FALLBACK_TOP_OFFSET_RATIO = 0.05


# ---------------------------------------------------------------------------
# cv2 is an optional runtime dep: we gracefully degrade to the PIL fallback
# if import fails (e.g. slim docker image without opencv).
# ---------------------------------------------------------------------------

_cv2: Any | None = None
_cascade: Any | None = None


def _load_cascade() -> Any | None:
    """Lazy-load the Haar face cascade. Cached for reuse."""
    global _cv2, _cascade
    if _cascade is not None:
        return _cascade
    try:
        import cv2  # type: ignore
    except Exception as exc:  # noqa: BLE001 — runtime import failures fall back
        logger.info("cv2 unavailable, face_mosaic will use naive fallback: %s", exc)
        return None
    _cv2 = cv2
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if not cascade_path.exists():
        logger.warning("haarcascade not found at %s", cascade_path)
        return None
    cascade = cv2.CascadeClassifier(str(cascade_path))
    if cascade.empty():
        logger.warning("haarcascade failed to load from %s", cascade_path)
        return None
    _cascade = cascade
    return _cascade


# ---------------------------------------------------------------------------
# Core mosaic operations
# ---------------------------------------------------------------------------


def mosaic_region(image: Image.Image, box: tuple[int, int, int, int], block_size: int = 24) -> None:
    """Pixelate a rectangular region of ``image`` in place.

    Args:
        image: PIL Image to modify (must be RGB or RGBA).
        box: ``(x, y, w, h)`` pixel rectangle.
        block_size: mosaic tile size in pixels (larger = coarser).
    """
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return
    x = max(0, x)
    y = max(0, y)
    w = min(image.width - x, w)
    h = min(image.height - y, h)
    if w <= 0 or h <= 0:
        return
    region = image.crop((x, y, x + w, y + h))
    # Shrink then enlarge with NEAREST to produce blocky pixelation
    small_w = max(1, w // block_size)
    small_h = max(1, h // block_size)
    small = region.resize((small_w, small_h), Image.BILINEAR)
    pixelated = small.resize((w, h), Image.NEAREST)
    image.paste(pixelated, (x, y))


def _detect_face_boxes(pil_image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Return a list of ``(x, y, w, h)`` face rectangles detected by OpenCV.

    Returns an empty list when cv2 is unavailable or no faces are found.
    """
    cascade = _load_cascade()
    if cascade is None or _cv2 is None:
        return []

    import numpy as np  # lazy import — only when we actually run detection

    rgb = pil_image.convert("RGB")
    arr = np.asarray(rgb, dtype=np.uint8)
    gray = _cv2.cvtColor(arr, _cv2.COLOR_RGB2GRAY)
    gray = _cv2.equalizeHist(gray)

    faces = cascade.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(_MIN_FACE_PX, _MIN_FACE_PX),
    )
    return [tuple(int(v) for v in f) for f in faces]  # type: ignore[return-value]


def _fallback_box(image: Image.Image) -> tuple[int, int, int, int]:
    """Produce the defensive top-center rectangle used when no face is detected."""
    w = int(image.width * _FALLBACK_WIDTH_RATIO)
    h = int(image.height * _FALLBACK_HEIGHT_RATIO)
    x = (image.width - w) // 2
    y = int(image.height * _FALLBACK_TOP_OFFSET_RATIO)
    return (x, y, w, h)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def apply_face_mosaic(
    src: str | Path | bytes,
    dst: str | Path | None = None,
    *,
    block_size: int = 24,
    always_fallback: bool = False,
) -> Path:
    """Apply a face mosaic to an image and write it to disk.

    Behavior:
      1. Try Haar-cascade face detection and pixelate every detected face.
      2. If no face was detected (or cv2 is missing), apply a defensive
         top-center rectangle mosaic so the output is never face-visible.
      3. If ``always_fallback`` is True, always apply the fallback region
         *in addition to* any detected faces — useful for a paranoid mode.

    Args:
        src: path to an input image or raw image bytes.
        dst: output path. Defaults to ``<src stem>_mosaic.<src suffix>``
             next to the input (or ``data/media/mosaic_<timestamp>.png``
             when src is bytes).
        block_size: mosaic tile size (pixels).
        always_fallback: overlay the top-center rectangle regardless of
                         detection results.

    Returns:
        The ``Path`` of the saved output image.
    """
    if isinstance(src, (str, Path)):
        src_path = Path(src)
        if not src_path.exists():
            raise FileNotFoundError(f"face_mosaic: input not found: {src_path}")
        image = Image.open(src_path).convert("RGB")
        if dst is None:
            dst = src_path.with_name(f"{src_path.stem}_mosaic{src_path.suffix or '.png'}")
    else:
        image = Image.open(io.BytesIO(src)).convert("RGB")
        if dst is None:
            import time
            dst = Path("data/media") / f"mosaic_{int(time.time())}.png"

    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    boxes = _detect_face_boxes(image)
    applied = 0
    for box in boxes:
        mosaic_region(image, box, block_size=block_size)
        applied += 1

    if applied == 0 or always_fallback:
        mosaic_region(image, _fallback_box(image), block_size=block_size)
        logger.info(
            "face_mosaic: %d face(s) detected, fallback applied=%s -> %s",
            applied, applied == 0 or always_fallback, dst_path,
        )
    else:
        logger.info("face_mosaic: %d face(s) detected and pixelated -> %s",
                    applied, dst_path)

    image.save(dst_path)
    return dst_path


def apply_background_blur(
    src: str | Path | bytes,
    dst: str | Path | None = None,
    *,
    blur_radius: int = 50,
    person_width_ratio: float = 0.75,
    person_height_ratio: float = 0.95,
    feather: float = 0.35,
) -> Path:
    """Apply portrait-mode style depth-of-field blur to background.

    Simulates a camera focusing on the person with shallow DOF — the
    background gets a clean Gaussian bokeh while the subject stays sharp.

    Strategy:
      1. Detect face via Haar cascade to locate the person.
      2. Build an elliptical mask around the person (face → expand to body).
      3. If no face found, assume center-weighted ellipse (selfie convention).
      4. Apply Gaussian blur to background only (no pixelation).
      5. Wide feathered edge for natural focus fall-off.

    Args:
        src: path to input image or raw bytes.
        dst: output path (defaults to ``<stem>_bgblur.<ext>``).
        blur_radius: Gaussian blur radius (higher = more bokeh).
        person_width_ratio: width of the sharp ellipse relative to image.
        person_height_ratio: height of the sharp ellipse relative to image.
        feather: width of the soft transition zone (0.0–1.0 relative to ellipse).

    Returns:
        Path of the saved output image.
    """
    from PIL import ImageFilter

    # --- Load image ---
    if isinstance(src, (str, Path)):
        src_path = Path(src)
        if not src_path.exists():
            raise FileNotFoundError(f"background_blur: input not found: {src_path}")
        image = Image.open(src_path).convert("RGB")
        if dst is None:
            dst = src_path.with_name(f"{src_path.stem}_bgblur{src_path.suffix or '.png'}")
    else:
        image = Image.open(io.BytesIO(src)).convert("RGB")
        if dst is None:
            import time
            dst = Path("data/media") / f"bgblur_{int(time.time())}.png"

    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    W, H = image.size

    # --- Detect face to locate person ---
    faces = _detect_face_boxes(image)
    if faces:
        fx, fy, fw, fh = max(faces, key=lambda b: b[2] * b[3])
        cx = fx + fw // 2
        # Body center is below face
        cy = min(H, fy + fh * 2)
        logger.info("background_blur: face at (%d,%d %dx%d), body center (%d,%d)", fx, fy, fw, fh, cx, cy)
    else:
        cx, cy = W // 2, H // 2
        logger.info("background_blur: no face detected, using image center")

    # --- Feathered elliptical mask ---
    import numpy as np

    ew = int(W * person_width_ratio) // 2
    eh = int(H * person_height_ratio) // 2

    y_grid, x_grid = np.ogrid[:H, :W]
    dist = np.sqrt(((x_grid - cx) / max(ew, 1)) ** 2 + ((y_grid - cy) / max(eh, 1)) ** 2)

    # Smooth ramp: 0 inside person → 1 far background
    inner = 1.0 - feather / 2
    outer = 1.0 + feather / 2
    alpha = np.clip((dist - inner) / max(outer - inner, 0.01), 0.0, 1.0).astype(np.float32)

    # --- Portrait-mode Gaussian bokeh ---
    bg_blur = image.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    # --- Composite ---
    orig_arr = np.asarray(image, dtype=np.float32)
    blur_arr = np.asarray(bg_blur, dtype=np.float32)
    alpha3 = alpha[:, :, np.newaxis]
    result_arr = orig_arr * (1.0 - alpha3) + blur_arr * alpha3
    result = Image.fromarray(result_arr.astype(np.uint8))

    result.save(dst_path, quality=95)
    logger.info("background_blur: portrait-mode blur saved to %s", dst_path)
    return dst_path


def has_cv2() -> bool:
    """Report whether OpenCV + Haar cascade are available at runtime."""
    return _load_cascade() is not None
