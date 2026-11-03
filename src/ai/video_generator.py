"""Video generation using Kie.ai API.

Supports two video endpoints:
- VEO 3.1: POST /api/v1/veo/generate (flat body, models: veo3, veo3_fast, veo3_lite)
- Generic:  POST /api/v1/jobs/createTask (input wrapper, models: kling-3.0/video, sora-2-text-to-video, etc.)

Polling is always via GET /api/v1/jobs/recordInfo?taskId=...
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import httpx

from config.settings import settings
from src.core.cost_tracker import cost_tracker, BudgetExceededError

logger = logging.getLogger(__name__)


@dataclass
class GeneratedVideo:
    video_url: str
    duration_seconds: float
    resolution: str
    generation_id: str


class KieAPIError(Exception):
    """Raised when the Kie.ai API returns an error."""

    def __init__(self, message: str, code: int | None = None):
        self.code = code
        super().__init__(message)


# VEO models use the dedicated /veo/generate endpoint
VEO_MODELS = {"veo3", "veo3_fast", "veo3_lite"}


class KieVideoGenerator:
    """Generate videos using Kie.ai API (VEO 3.1 or generic models)."""

    POLL_INTERVAL = 10  # seconds
    MAX_POLL_TIMEOUT = 600  # seconds
    MAX_TRANSIENT_RETRIES = 3

    def __init__(self):
        self.api_key: str = settings.kie_api_key
        self.model: str = getattr(settings, "kie_veo_model", "veo3_fast")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _is_veo_model(self, model: str | None = None) -> bool:
        return (model or self.model) in VEO_MODELS

    async def _submit_veo(
        self,
        prompt: str,
        aspect_ratio: str,
        model: str | None = None,
    ) -> str:
        """Submit to the VEO-specific endpoint: POST /api/v1/veo/generate"""
        use_model = model or self.model
        payload = {
            "prompt": prompt,
            "model": use_model,
            "aspect_ratio": aspect_ratio,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(
                    f"{settings.kie_base_url}/veo/generate",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise KieAPIError(
                    f"Kie.ai VEO HTTP {exc.response.status_code}: {exc.response.text}",
                    code=exc.response.status_code,
                ) from exc
            except httpx.RequestError as exc:
                raise KieAPIError(f"Kie.ai VEO request failed: {exc}") from exc

            data = response.json()

        if data.get("code") != 200:
            raise KieAPIError(f"Kie.ai VEO generate failed: {data}", code=data.get("code"))

        task_id = data.get("data", {}).get("taskId", data.get("data", {}).get("task_id"))
        if not task_id:
            raise KieAPIError("Kie.ai VEO returned no task_id")

        logger.info("Kie.ai VEO generation submitted: task_id=%s model=%s", task_id, use_model)
        return task_id

    async def _submit_generic(
        self,
        prompt: str,
        duration: int,
        aspect_ratio: str,
        resolution: str,
        model: str | None = None,
    ) -> str:
        """Submit to generic endpoint: POST /api/v1/jobs/createTask"""
        use_model = model or self.model
        payload = {
            "model": use_model,
            "input": {
                "prompt": prompt,
                "duration": str(duration),
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
            },
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(
                    f"{settings.kie_base_url}/jobs/createTask",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise KieAPIError(
                    f"Kie.ai generate HTTP {exc.response.status_code}: {exc.response.text}",
                    code=exc.response.status_code,
                ) from exc
            except httpx.RequestError as exc:
                raise KieAPIError(f"Kie.ai generate request failed: {exc}") from exc

            data = response.json()

        if data.get("code") != 200:
            raise KieAPIError(f"Kie.ai generate failed: {data}", code=data.get("code"))

        task_id = data.get("data", {}).get("taskId", data.get("data", {}).get("task_id"))
        if not task_id:
            raise KieAPIError("Kie.ai returned no task_id")

        logger.info("Kie.ai video generation submitted: task_id=%s model=%s", task_id, use_model)
        return task_id

    async def _poll_task(self, task_id: str, is_veo: bool = False) -> dict:
        """Poll a task until completion or timeout. Returns the output dict.

        VEO tasks use GET /veo/record-info with successFlag (0=pending, 1=done, 2/3=fail).
        Generic tasks use GET /jobs/recordInfo with state field.
        """
        if is_veo:
            url = f"{settings.kie_base_url}/veo/record-info"
        else:
            url = f"{settings.kie_base_url}/jobs/recordInfo"

        start = time.monotonic()
        consecutive_errors = 0
        poll_interval = 30 if is_veo else self.POLL_INTERVAL

        async with httpx.AsyncClient(timeout=30) as client:
            while (time.monotonic() - start) < self.MAX_POLL_TIMEOUT:
                try:
                    resp = await client.get(url, params={"taskId": task_id}, headers=self._headers())
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code < 500:
                        raise KieAPIError(
                            f"Kie.ai poll HTTP {exc.response.status_code}: {exc.response.text}",
                            code=exc.response.status_code,
                        ) from exc
                    consecutive_errors += 1
                    logger.warning(
                        "Kie.ai poll transient error (%d/%d): %s",
                        consecutive_errors, self.MAX_TRANSIENT_RETRIES, exc,
                    )
                    if consecutive_errors >= self.MAX_TRANSIENT_RETRIES:
                        raise KieAPIError(
                            f"Kie.ai poll failed after {self.MAX_TRANSIENT_RETRIES} transient errors: {exc}",
                        ) from exc
                    await asyncio.sleep(poll_interval)
                    continue
                except httpx.RequestError as exc:
                    consecutive_errors += 1
                    logger.warning(
                        "Kie.ai poll request error (%d/%d): %s",
                        consecutive_errors, self.MAX_TRANSIENT_RETRIES, exc,
                    )
                    if consecutive_errors >= self.MAX_TRANSIENT_RETRIES:
                        raise KieAPIError(
                            f"Kie.ai poll failed after {self.MAX_TRANSIENT_RETRIES} transient errors: {exc}",
                        ) from exc
                    await asyncio.sleep(poll_interval)
                    continue

                consecutive_errors = 0
                body = resp.json()

                if body.get("code") != 200:
                    raise KieAPIError(f"Kie.ai poll error: {body}", code=body.get("code"))

                task_data = body.get("data", {})
                elapsed = time.monotonic() - start

                if is_veo:
                    # VEO uses successFlag: 0=generating, 1=success, 2=failed, 3=generation failed
                    flag = task_data.get("successFlag")
                    if flag == 1:
                        logger.info("Kie.ai VEO task %s completed after %.1fs", task_id, elapsed)
                        return task_data.get("response", task_data)
                    elif flag in (2, 3):
                        error_msg = task_data.get("errorMessage", "Unknown")
                        raise KieAPIError(f"Kie.ai VEO task {task_id} failed (flag={flag}): {error_msg}")
                    # flag == 0: still generating, continue polling
                else:
                    # Generic uses state field
                    status = task_data.get("state", task_data.get("status", ""))
                    if status in ("success", "completed"):
                        logger.info("Kie.ai task %s completed after %.1fs", task_id, elapsed)
                        result_json_str = task_data.get("resultJson", "")
                        if result_json_str and isinstance(result_json_str, str):
                            return json.loads(result_json_str)
                        return task_data.get("output", task_data)
                    elif status in ("fail", "failed"):
                        error_msg = task_data.get("failMsg", task_data.get("error", "Unknown"))
                        raise KieAPIError(f"Kie.ai task {task_id} failed: {error_msg}")

                await asyncio.sleep(poll_interval)

        raise TimeoutError(
            f"Kie.ai video generation timed out after {self.MAX_POLL_TIMEOUT}s (task_id={task_id})"
        )

    async def generate_video(
        self,
        prompt: str,
        duration_seconds: int = 8,
        aspect_ratio: str = "9:16",
        resolution: str = "720p",
        model: str | None = None,
    ) -> GeneratedVideo:
        """Generate a video using Kie.ai.

        Automatically routes to the correct endpoint based on model:
        - VEO models (veo3, veo3_fast, veo3_lite) → /veo/generate
        - Other models (kling-3.0/video, etc.) → /jobs/createTask
        """
        if cost_tracker.is_budget_exceeded():
            raise BudgetExceededError("Daily API budget exceeded — skipping video generation")

        use_model = model or self.model
        is_veo = self._is_veo_model(use_model)

        if is_veo:
            task_id = await self._submit_veo(prompt, aspect_ratio, use_model)
        else:
            task_id = await self._submit_generic(prompt, duration_seconds, aspect_ratio, resolution, use_model)

        output = await self._poll_task(task_id, is_veo=is_veo)

        cost_tracker.record_kie_video_usage("generate_video", use_model)

        # Extract video URL from result
        result_urls = output.get("resultUrls", [])
        video_url = result_urls[0] if result_urls else output.get("video_url", "")

        return GeneratedVideo(
            video_url=video_url,
            duration_seconds=output.get("duration", duration_seconds),
            resolution=resolution,
            generation_id=task_id,
        )

    async def generate_short_video(
        self,
        topic: str,
        style: str = "dynamic and engaging",
        platform: str = "tiktok",
    ) -> GeneratedVideo:
        """Generate a short-form video optimized for a specific platform."""
        aspect_ratios = {
            "tiktok": "9:16",
            "instagram": "9:16",
            "youtube": "9:16",
            "x": "16:9",
        }

        prompt = (
            f"Create a {style} short video about: {topic}. "
            f"Fast-paced, visually appealing, designed for {platform}. "
            f"High production quality, trending aesthetics, attention-grabbing from the first frame."
        )

        return await self.generate_video(
            prompt=prompt,
            duration_seconds=8,
            aspect_ratio=aspect_ratios.get(platform, "9:16"),
        )

    async def extend_video(
        self,
        task_id: str,
        prompt: str,
        duration: int = 8,
        resolution: str = "720p",
    ) -> GeneratedVideo:
        """Extend an existing video using Kie.ai VEO 3.1 extend endpoint.

        Uses POST /api/v1/veo/extend for VEO models.
        """
        if cost_tracker.is_budget_exceeded():
            raise BudgetExceededError("Daily API budget exceeded — skipping video extension")

        payload = {
            "taskId": task_id,
            "prompt": prompt,
            "model": self.model,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(
                    f"{settings.kie_base_url}/veo/extend",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise KieAPIError(
                    f"Kie.ai extend HTTP {exc.response.status_code}: {exc.response.text}",
                    code=exc.response.status_code,
                ) from exc
            except httpx.RequestError as exc:
                raise KieAPIError(f"Kie.ai extend request failed: {exc}") from exc

            data = response.json()

        if data.get("code") != 200:
            raise KieAPIError(f"Kie.ai extend failed: {data}", code=data.get("code"))

        extend_task_id = data.get("data", {}).get("taskId", data.get("data", {}).get("task_id"))
        if not extend_task_id:
            raise KieAPIError("Kie.ai extend returned no task_id")

        logger.info("Kie.ai video extend submitted: task_id=%s", extend_task_id)

        output = await self._poll_task(extend_task_id, is_veo=True)

        cost_tracker.record_kie_video_usage("extend_video", self.model)

        result_urls = output.get("resultUrls", [])
        video_url = result_urls[0] if result_urls else output.get("video_url", "")

        return GeneratedVideo(
            video_url=video_url,
            duration_seconds=output.get("duration", duration),
            resolution=resolution,
            generation_id=extend_task_id,
        )
