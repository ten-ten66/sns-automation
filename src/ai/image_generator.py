"""Image generation using Kie.ai Nano Banana Pro API."""

import asyncio
import logging
import random
import time

import httpx
from pathlib import Path

from config.settings import settings
from src.core.cost_tracker import cost_tracker

logger = logging.getLogger(__name__)

GENERATE_URL = f"{settings.kie_base_url}/jobs/createTask"
TASK_URL = f"{settings.kie_base_url}/jobs/recordInfo"

# Map pixel sizes to aspect ratios for Kie.ai
SIZE_TO_ASPECT_RATIO = {
    "1024x1024": "1:1",
    "1792x1024": "16:9",
    "1024x1792": "9:16",
}

# Map API model names to cost tracker keys
MODEL_COST_KEYS = {
    "gemini-3-pro-image-preview": "nano_banana_pro",
    "gemini-3.1-flash-image-preview": "nano_banana_2",
}

POLL_INTERVAL = 3  # seconds
POLL_TIMEOUT = 300  # seconds
MAX_TRANSIENT_RETRIES = 3


class KieApiError(Exception):
    """Raised when the Kie.ai API returns an error."""


class KieContentPolicyError(KieApiError):
    """Raised when content is blocked by Kie.ai content policy."""


class KieTimeoutError(KieApiError):
    """Raised when polling for a Kie.ai task result times out."""


class ImageGenerator:
    """Generate images for SNS posts using Kie.ai Nano Banana Pro."""

    def __init__(self):
        self.api_key = settings.kie_api_key
        self.default_model = getattr(
            settings, "kie_nano_banana_model", "gemini-3-pro-image-preview"
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _poll_task(self, task_id: str, client: httpx.AsyncClient) -> dict:
        """Poll a Kie.ai task until completion or timeout.

        Returns the task output dict on success.
        Raises KieTimeoutError, KieApiError, or KieContentPolicyError on failure.
        Retries on transient HTTP errors (5xx, connection errors) up to
        MAX_TRANSIENT_RETRIES consecutive times before giving up.
        """
        start = time.monotonic()
        consecutive_errors = 0

        while (time.monotonic() - start) < POLL_TIMEOUT:
            try:
                resp = await client.get(
                    TASK_URL,
                    params={"taskId": task_id},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                consecutive_errors = 0  # reset on success
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    # 4xx — not transient, raise immediately
                    raise
                consecutive_errors += 1
                logger.warning(
                    "Transient HTTP %s polling task %s (%d/%d)",
                    exc.response.status_code,
                    task_id,
                    consecutive_errors,
                    MAX_TRANSIENT_RETRIES,
                )
                if consecutive_errors >= MAX_TRANSIENT_RETRIES:
                    raise KieApiError(
                        f"Kie.ai polling failed after {MAX_TRANSIENT_RETRIES} "
                        f"consecutive transient errors: {exc}"
                    ) from exc
                await asyncio.sleep(POLL_INTERVAL)
                continue
            except httpx.RequestError as exc:
                consecutive_errors += 1
                logger.warning(
                    "Transient request error polling task %s (%d/%d): %s",
                    task_id,
                    consecutive_errors,
                    MAX_TRANSIENT_RETRIES,
                    exc,
                )
                if consecutive_errors >= MAX_TRANSIENT_RETRIES:
                    raise KieApiError(
                        f"Kie.ai polling failed after {MAX_TRANSIENT_RETRIES} "
                        f"consecutive transient errors: {exc}"
                    ) from exc
                await asyncio.sleep(POLL_INTERVAL)
                continue

            body = resp.json()

            if body.get("code") != 200:
                raise KieApiError(
                    f"Kie.ai task poll error: {body}"
                )

            data = body["data"]
            status = data.get("state", data.get("status", ""))

            if status == "success" or status == "completed":
                # Parse resultJson if present (string-encoded JSON)
                result_json_str = data.get("resultJson", "")
                if result_json_str and isinstance(result_json_str, str):
                    import json
                    result_data = json.loads(result_json_str)
                    return result_data
                return data.get("output", data)
            elif status == "fail" or status == "failed":
                error_msg = data.get("failMsg", data.get("error", "Unknown error"))
                if "content policy" in str(error_msg).lower():
                    raise KieContentPolicyError(
                        f"Content blocked by policy: {error_msg}"
                    )
                raise KieApiError(f"Kie.ai task failed: {error_msg}")
            # waiting / queuing / generating — keep polling

            await asyncio.sleep(POLL_INTERVAL)

        elapsed = time.monotonic() - start
        raise KieTimeoutError(
            f"Kie.ai task {task_id} timed out after {elapsed:.0f}s"
        )

    async def _generate_pollinations_fallback(self, prompt: str, aspect_ratio: str = "1:1") -> list[str]:
        """Free fallback image generation via Pollinations.ai when Kie.ai credits are depleted.

        Downloads the image to disk and returns the local path.
        Pollinations URLs are ephemeral — the remote image changes on every request,
        so we must persist the bytes locally before returning.
        """
        import urllib.parse

        # Map aspect ratios to dimensions
        size_map = {
            "1:1": (1024, 1024),
            "9:16": (768, 1365),
            "16:9": (1365, 768),
            "4:5": (819, 1024),
            "3:4": (768, 1024),
        }
        width, height = size_map.get(aspect_ratio, (1024, 1024))

        seed = random.randint(1, 9999)
        encoded_prompt = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&model=flux&seed={seed}"

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(url)
            if response.status_code == 200 and len(response.content) > 10000:
                # Save to disk so the bytes are not lost when the ephemeral URL regenerates
                save_dir = Path("data/media/pollinations")
                await asyncio.to_thread(save_dir.mkdir, parents=True, exist_ok=True)
                filename = f"pollinations_{seed}_{int(time.time())}.png"
                save_path = save_dir / filename
                await asyncio.to_thread(save_path.write_bytes, response.content)
                logger.info("Pollinations fallback image saved: %s", save_path)
                return [str(save_path)]

        return []

    async def generate(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        model: str | None = None,
        n: int = 1,
    ) -> list[str]:
        """Generate images using Kie.ai Nano Banana Pro.

        This is the primary generation method. Accepts aspect_ratio directly
        (e.g. "1:1", "16:9", "9:16").

        Falls back to Pollinations.ai if Kie.ai credits are depleted.

        Returns list of image URLs.
        """
        try:
            # Map old model names to Kie.ai model IDs
            model_map = {
                "gemini-3-pro-image-preview": "nano-banana-pro",
                "gemini-3.1-flash-image-preview": "nano-banana-2",
            }
            api_model = model_map.get(model or self.default_model, model or "nano-banana-pro")

            payload = {
                "model": api_model,
                "input": {
                    "prompt": prompt,
                    "image_input": [],
                    "aspect_ratio": aspect_ratio,
                    "resolution": "2K",
                    "output_format": "png",
                },
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                # Submit the generation task
                resp = await client.post(
                    GENERATE_URL,
                    headers=self._headers(),
                    json=payload,
                )
                resp.raise_for_status()
                body = resp.json()

                if body.get("code") != 200:
                    raise KieApiError(f"Kie.ai generation request failed: {body}")

                task_id = body["data"]["taskId"]
                logger.info("Kie.ai task submitted: %s (model=%s)", task_id, api_model)

                # Poll until complete
                output = await self._poll_task(task_id, client)

            # Extract image URLs from result (may be in resultUrls or image_urls)
            image_urls = output.get("resultUrls", output.get("image_urls", []))
            if image_urls:
                cost_key = MODEL_COST_KEYS.get(model or self.default_model, model or "nano_banana_pro")
                cost_tracker.record_kie_image_usage("generate", cost_key)
                logger.info(
                    "Kie.ai generated %d image(s) with model %s",
                    len(image_urls),
                    api_model,
                )
            return image_urls
        except Exception as e:
            if "402" in str(e) or "Credits insufficient" in str(e) or "credit" in str(e).lower():
                logger.warning("Kie.ai credits depleted, falling back to Pollinations.ai")
                return await self._generate_pollinations_fallback(prompt, aspect_ratio)
            raise

    async def generate_with_kie(
        self,
        prompt: str,
        size: str = "1024x1024",
        model: str | None = None,
        n: int = 1,
    ) -> list[str]:
        """Generate images using Kie.ai Nano Banana Pro.

        Backward-compatible method that accepts pixel size strings.
        Converts size to aspect_ratio and delegates to generate().

        Returns list of image URLs.
        """
        aspect_ratio = SIZE_TO_ASPECT_RATIO.get(size, "1:1")
        return await self.generate(
            prompt=prompt, aspect_ratio=aspect_ratio, model=model, n=n
        )

    # Alias for backward compatibility (old name was generate_with_dalle)
    generate_with_dalle = generate_with_kie

    async def generate_thumbnail(
        self,
        title: str,
        platform: str = "youtube",
        style: str = "bold and eye-catching",
    ) -> str:
        """Generate a thumbnail image optimized for a platform."""
        size_map = {
            "youtube": "1792x1024",
            "instagram": "1024x1024",
            "x": "1792x1024",
            "tiktok": "1024x1792",
        }
        size = size_map.get(platform, "1024x1024")

        prompt = (
            f"Create a {style} social media thumbnail for: {title}. "
            f"Optimized for {platform}. Bold text overlay, vibrant colors, "
            f"high contrast, professional quality. No watermarks."
        )

        urls = await self.generate_with_kie(prompt, size=size)
        return urls[0] if urls else ""

    async def download_image(self, url: str, save_path: str) -> str:
        """Download an image from URL and save locally."""
        # SSRF prevention: only allow HTTPS from trusted domains
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http"):
            raise ValueError(f"Untrusted download URL scheme: {url}")
        blocked_hosts = ("169.254.169.254", "metadata.google.internal", "localhost", "127.0.0.1", "0.0.0.0")
        if parsed.hostname and parsed.hostname in blocked_hosts:
            raise ValueError(f"Blocked download URL host: {parsed.hostname}")

        # Validate save_path is under allowed directory
        allowed_dir = Path("data/media").resolve()
        resolved_save = Path(save_path).resolve()
        if not str(resolved_save).startswith(str(allowed_dir)):
            raise ValueError(f"Save path must be under data/media: {save_path}")

        # Pollinations.ai returns images directly via redirect; allow redirects for that domain
        is_pollinations = parsed.hostname and "pollinations.ai" in parsed.hostname
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=is_pollinations) as client:
            response = await client.get(url)
            response.raise_for_status()
            path = Path(save_path)
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, response.content)
            return str(path)

    async def generate_carousel_images(
        self,
        slides: list[str],
        theme: str = "modern minimalist",
    ) -> list[str]:
        """Generate a set of carousel images (e.g., for Instagram)."""
        urls = []
        for i, slide_text in enumerate(slides):
            prompt = (
                f"Slide {i+1} of a carousel post. {theme} design. "
                f"Content: {slide_text}. Clean layout, readable text, "
                f"consistent color scheme. Square format."
            )
            result = await self.generate_with_kie(prompt, size="1024x1024")
            if result:
                urls.append(result[0])
        return urls
