#!/usr/bin/env python3
"""Generate 3 Miku ★ Oracle cosplay images and post them to Discord with face mosaic.

Picks one variation from three different theme banks (tarot / astrology /
divination) for variety, runs each through ``marketing.generate_oracle_image``
(which calls Kie.ai Nano Banana Pro → downloads the file → applies
``src.media.face_mosaic.apply_face_mosaic``), and uploads the resulting
mosaiced PNGs as multipart attachments to the Discord webhook defined by
``ALERT_WEBHOOK_URL`` in ``.env``.

The remote Kie.ai URL is NEVER posted — only the local, face-mosaiced file
is uploaded, so Discord only ever sees the protected version.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config.settings import settings  # noqa: E402
from src.agents import Orchestrator  # noqa: E402


# Three hand-picked variations across three different theme banks for variety.
TARGETS: list[tuple[str, str]] = [
    ("tarot", "tarot_01"),        # Moonlit Witch Spread
    ("astrology", "astro_02"),    # Zodiac Wheel Pose
    ("divination", "divine_01"),  # Crystal Ball Gaze
]


async def _dispatch_one(orch: Orchestrator, category: str, variation_id: str) -> dict:
    result = await orch.dispatch(
        "marketing", "generate_oracle_image", triggered_by="discord_demo",
        category=category,
        variation_id=variation_id,
        platform="instagram",
        apply_mosaic=True,
        save_dir="data/media/oracle",
    )
    return {
        "ok": result.ok,
        "error": result.error,
        "data": result.data,
    }


def _upload_to_discord(webhook_url: str, files_info: list[dict]) -> None:
    """Upload each mosaiced file to the Discord webhook as an attachment.

    One POST per image so each one gets its own card in the channel and
    caption text. Non-fatal on per-image error — logs and continues.
    """
    for i, info in enumerate(files_info, start=1):
        local_path = Path(info["mosaic_path"])
        if not local_path.exists():
            print(f"  [{i}] FAIL: mosaic file missing at {local_path}")
            continue

        content_lines = [
            f"**Miku ★ Oracle — {info['variation_id']}** ({info['category']})",
            f"Aspect: `{info['aspect_ratio']}` • face mosaic applied ({'face detected' if info.get('face_detected') else 'fallback rectangle'})",
        ]
        payload = {"content": "\n".join(content_lines)}

        with open(local_path, "rb") as fh:
            files = {
                "file": (local_path.name, fh, "image/png"),
                "payload_json": (None, json.dumps(payload), "application/json"),
            }
            try:
                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(webhook_url, files=files)
                if 200 <= resp.status_code < 300:
                    print(f"  [{i}] OK: posted {local_path.name} ({local_path.stat().st_size // 1024} KB)")
                else:
                    print(f"  [{i}] FAIL: HTTP {resp.status_code} {resp.text[:200]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [{i}] FAIL: upload error: {exc}")


async def main() -> int:
    webhook_url = settings.alert_webhook_url or ""
    if not webhook_url or "discord" not in webhook_url.lower():
        print("ERROR: ALERT_WEBHOOK_URL is not a Discord webhook (set it in .env)")
        return 2

    orch = Orchestrator()

    print("\n==== Generating 3 Miku ★ Oracle cosplay images ====")
    results: list[dict] = []
    for i, (category, variation_id) in enumerate(TARGETS, start=1):
        print(f"  [{i}] dispatching marketing.generate_oracle_image "
              f"(category={category}, variation_id={variation_id})")
        outcome = await _dispatch_one(orch, category, variation_id)
        if not outcome["ok"]:
            print(f"      FAIL: {outcome.get('error')}")
            continue
        data = outcome["data"]
        mosaic_path = data.get("mosaic_path")
        if not mosaic_path:
            print(f"      FAIL: no mosaic_path in result "
                  f"(mosaic_error={data.get('mosaic_error')}, download_error={outcome.get('error')})")
            continue
        print(f"      OK: remote={data['remote_url'][:80]}...")
        print(f"          local={data['local_path']}")
        print(f"          mosaic={mosaic_path}")
        results.append({
            "variation_id": data["variation_id"],
            "category": category,
            "aspect_ratio": data["aspect_ratio"],
            "mosaic_path": mosaic_path,
        })

    if not results:
        print("\nNo images were generated — aborting Discord upload.")
        return 1

    print(f"\n==== Uploading {len(results)} image(s) to Discord ====")
    _upload_to_discord(webhook_url, results)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
