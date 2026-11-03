"""
SNS Automation Dashboard - Demo Screenshot Script
Captures key screens for TikTok app review submission.
"""

import os
import subprocess
import sys
import time

API_KEY = os.environ.get("SNS_API_KEY", "")
if not API_KEY:
    sys.exit("SNS_API_KEY env var is required")
BASE_URL = "http://127.0.0.1:9000"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo")


def inject_api_key(page):
    """Inject API key into sessionStorage so the dashboard JS doesn't prompt."""
    page.evaluate(f"sessionStorage.setItem('sns_api_key', '{API_KEY}')")


def wait_for_output_change(page, element_id, timeout_ms=15000):
    """Wait until an element's text changes away from its initial/loading state."""
    try:
        page.wait_for_function(
            f"""
            () => {{
                const el = document.getElementById('{element_id}');
                if (!el) return false;
                const text = el.textContent.trim();
                return text !== '' && text !== '...' && text !== 'Loading...' && !text.includes('Click Load');
            }}
            """,
            timeout=timeout_ms,
        )
    except Exception:
        pass  # Timeout is acceptable; we still screenshot what we have


def run_demo():
    from playwright.sync_api import sync_playwright

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"[INFO] Output directory: {OUTPUT_DIR}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            extra_http_headers={"Authorization": f"Bearer {API_KEY}"},
        )

        # Suppress browser dialogs (in case prompt() fires despite sessionStorage)
        def handle_dialog(dialog):
            print(f"[INFO] Suppressing dialog: {dialog.message}")
            dialog.accept(API_KEY)

        page = context.new_page()
        page.on("dialog", handle_dialog)

        # ── Screen 1: Dashboard Overview ──────────────────────────────────────
        print("[1/7] Navigating to Dashboard Overview...")
        page.goto(BASE_URL, wait_until="domcontentloaded")
        inject_api_key(page)
        page.wait_for_timeout(1500)

        # Trigger Load Summary so the overview has data
        try:
            page.click("button:has-text('Load Summary')")
            wait_for_output_change(page, "output")
            page.wait_for_timeout(1000)
        except Exception as e:
            print(f"  [WARN] Load Summary: {e}")

        page.screenshot(path=f"{OUTPUT_DIR}/01_dashboard.png", full_page=False)
        print("  Saved 01_dashboard.png")

        # ── Screen 2: Content Generation (form filled) ────────────────────────
        print("[2/7] Navigating to Content Generation tab...")
        page.click("div.tab:has-text('Content Generation')")
        inject_api_key(page)
        page.wait_for_timeout(800)

        # Fill in the form fields using the correct element IDs from the HTML
        try:
            page.select_option("select#gen-platform", "tiktok")
        except Exception as e:
            print(f"  [WARN] Platform select: {e}")

        try:
            page.fill("input#gen-topic", "美容コスメレビュー")
        except Exception as e:
            print(f"  [WARN] Topic fill: {e}")

        page.wait_for_timeout(500)
        page.screenshot(path=f"{OUTPUT_DIR}/02_content_gen.png", full_page=False)
        print("  Saved 02_content_gen.png")

        # ── Screen 3: Content Generation Result ───────────────────────────────
        print("[3/7] Clicking Generate and waiting for result...")
        try:
            # Click the Generate button (first .btn in the generate card)
            page.click("#generate button:has-text('Generate')")
            wait_for_output_change(page, "gen-output", timeout_ms=25000)
            page.wait_for_timeout(1000)
        except Exception as e:
            print(f"  [WARN] Generate click: {e}")

        page.screenshot(path=f"{OUTPUT_DIR}/03_content_result.png", full_page=False)
        print("  Saved 03_content_result.png")

        # ── Screen 4: Schedule Tab ─────────────────────────────────────────────
        print("[4/7] Navigating to Schedule tab...")
        page.click("div.tab:has-text('Schedule')")
        inject_api_key(page)
        page.wait_for_timeout(1000)

        # Pre-fill schedule form to look realistic
        try:
            page.select_option("select#sched-platform", "tiktok")
            page.fill(
                "textarea#sched-text",
                "✨ 新作コスメを徹底レビュー！プチプラなのに本格的な仕上がり #美容 #コスメ #TikTok",
            )
        except Exception as e:
            print(f"  [WARN] Schedule form fill: {e}")

        page.wait_for_timeout(500)
        page.screenshot(path=f"{OUTPUT_DIR}/04_schedule.png", full_page=False)
        print("  Saved 04_schedule.png")

        # ── Screen 5: Trends Tab → Google Trends ──────────────────────────────
        print("[5/7] Navigating to Trends tab...")
        page.click("div.tab:has-text('Trends')")
        inject_api_key(page)
        page.wait_for_timeout(800)

        try:
            page.click("button:has-text('Google Trends')")
            wait_for_output_change(page, "trend-output", timeout_ms=20000)
            page.wait_for_timeout(1000)
        except Exception as e:
            print(f"  [WARN] Google Trends click: {e}")

        page.screenshot(path=f"{OUTPUT_DIR}/05_trends.png", full_page=False)
        print("  Saved 05_trends.png")

        # ── Screen 6: Analytics Tab ────────────────────────────────────────────
        print("[6/7] Navigating to Analytics tab...")
        page.click("div.tab:has-text('Analytics')")
        inject_api_key(page)
        page.wait_for_timeout(800)

        try:
            page.click("button:has-text('30 Days')")
            wait_for_output_change(page, "analytics-output", timeout_ms=15000)
            page.wait_for_timeout(1000)
        except Exception as e:
            print(f"  [WARN] Analytics 30 Days: {e}")

        page.screenshot(path=f"{OUTPUT_DIR}/06_analytics.png", full_page=False)
        print("  Saved 06_analytics.png")

        # ── Screen 7: Health Endpoint ──────────────────────────────────────────
        print("[7/7] Navigating to /health endpoint...")
        page.goto(f"{BASE_URL}/health", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        page.screenshot(path=f"{OUTPUT_DIR}/07_health.png", full_page=False)
        print("  Saved 07_health.png")

        browser.close()

    print("\n[INFO] All screenshots captured successfully.")
    return True


def build_video():
    """Attempt to combine screenshots into an mp4 slideshow using ffmpeg."""
    ffmpeg_path = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True).stdout.strip()
    if not ffmpeg_path:
        print("[INFO] ffmpeg not found — skipping video generation. Screenshots are sufficient.")
        return

    output_video = f"{OUTPUT_DIR}/tiktok_demo.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate", "0.5",
        "-pattern_type", "glob",
        "-i", f"{OUTPUT_DIR}/*.png",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=1280:720",
        output_video,
    ]
    print(f"\n[INFO] Running ffmpeg to build slideshow video...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"[INFO] Video saved: {output_video}")
    else:
        print(f"[WARN] ffmpeg failed:\n{result.stderr[:500]}")


def list_outputs():
    print(f"\n[INFO] Files in {OUTPUT_DIR}:")
    for fname in sorted(os.listdir(OUTPUT_DIR)):
        fpath = os.path.join(OUTPUT_DIR, fname)
        size = os.path.getsize(fpath)
        print(f"  {fname}  ({size:,} bytes)")


if __name__ == "__main__":
    success = run_demo()
    if success:
        build_video()
        list_outputs()
    else:
        sys.exit(1)
