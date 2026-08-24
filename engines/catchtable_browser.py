from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from engines.catchtable_client import CatchTableClient
from engines.catchtable_models import CatchTableSessionValidation
from pengucro.storage import get_data_dir

logger = logging.getLogger(__name__)

SESSION_FILE_NAME = "catchtable_session.json"


def get_catchtable_session_path() -> Path:
    data_dir = get_data_dir()
    return data_dir / SESSION_FILE_NAME


def load_saved_catchtable_session() -> dict[str, Any]:
    """Load saved session tokens, cookies, and device id from disk."""
    path = get_catchtable_session_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load CatchTable session file: %s", e)
        return {}


def save_catchtable_session(data: dict[str, Any]) -> None:
    """Save session tokens and cookies to disk."""
    path = get_catchtable_session_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Failed to save CatchTable session file: %s", e)


class CatchTableBrowserSession:
    """Helper to perform interactive login and capture credentials using Playwright."""

    @classmethod
    async def login_and_capture(
        cls,
        *,
        headless: bool = False,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        """Open browser window for user to log in, then extract session cookies and tokens."""
        from playwright.async_api import async_playwright

        captured: dict[str, Any] = {}
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 850},
            )
            page = await context.new_page()

            # Intercept authorization tokens from request headers
            async def on_request(req):
                auth = req.headers.get("authorization", "")
                if auth and "Bearer " in auth:
                    captured["auth_token"] = auth.replace("Bearer ", "").strip()
                dev_id = req.headers.get("x-device-id", "")
                if dev_id:
                    captured["device_id"] = dev_id

            page.on("request", on_request)

            logger.info("Opening CatchTable login page...")
            await page.goto("https://app.catchtable.co.kr/ct/login", wait_until="domcontentloaded")

            # Wait for user to complete login (e.g. navigation away from /login or presence of cookies)
            start_time = 0
            while start_time < timeout_seconds:
                cookies = await context.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}

                # Check if logged in
                if any(k in cookie_dict for k in ["ct_access_token", "accessToken", "refreshToken", "user_id"]):
                    captured["cookies"] = cookie_dict
                    break
                if "/ct/login" not in page.url and "catchtable.co.kr" in page.url:
                    captured["cookies"] = cookie_dict
                    break

                await page.wait_for_timeout(1000)
                start_time += 1

            # Extract localStorage
            try:
                device_id = await page.evaluate("() => localStorage.getItem('deviceId') || localStorage.getItem('x-device-id') || ''")
                if device_id:
                    captured["device_id"] = device_id
            except Exception:
                pass

            if "cookies" not in captured:
                cookies = await context.cookies()
                captured["cookies"] = {c["name"]: c["value"] for c in cookies}

            await browser.close()

        if captured.get("cookies") or captured.get("auth_token"):
            save_catchtable_session(captured)

        return captured

    @classmethod
    async def verify_saved_session(
        cls,
        *,
        api_base: str = "https://ct-api.catchtable.co.kr",
    ) -> CatchTableSessionValidation:
        """Check whether current saved session on disk is valid."""
        saved = load_saved_catchtable_session()
        auth_token = saved.get("auth_token", "")
        cookies = saved.get("cookies", {})
        device_id = saved.get("device_id", "")

        client = CatchTableClient(
            api_base=api_base,
            auth_token=auth_token,
            device_id=device_id,
            cookies=cookies,
        )
        try:
            return await client.validate_session()
        finally:
            await client.close()
