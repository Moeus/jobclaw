"""Captcha detection and notification for Boss直聘."""

from __future__ import annotations

import logging
from typing import Any

from jobclaw.browser import find_first, find_first_visible

logger = logging.getLogger(__name__)

# Known captcha selectors on Boss直聘
_CAPTCHA_SELECTORS = [
    "#captcha",
    ".slide-verify",
    ".geetest_panel",
    ".captcha-wrapper",
    "[class*='verify']",
    "[class*='captcha']",
    "div.dialog-container:has(canvas)",  # slider captcha dialog
]


def detect_captcha(page: Any) -> bool:
    """Detect if the page is showing a captcha challenge.

    Checks multiple known selectors and also looks for common
    captcha-related text in visible dialogs.
    """
    for selector in _CAPTCHA_SELECTORS:
        try:
            if find_first_visible(page, [selector]):
                logger.warning("Captcha detected via selector: %s", selector)
                return True
        except Exception:
            continue

    # Text-based fallback: check for common captcha prompt text
    try:
        body = find_first(page, "body")
        body_text = body.text if body else ""
        captcha_keywords = ["请完成安全验证", "滑动验证", "图形验证", "安全检测"]
        for kw in captcha_keywords:
            if kw in body_text:
                logger.warning("Captcha detected via keyword: %s", kw)
                return True
    except Exception:
        pass

    return False


async def notify_captcha(notifier: object, job_url: str) -> None:
    """Send a captcha alert via the notifier (e.g. Telegram).

    Args:
        notifier: Object with an async ``send_text(text)`` method
                  (e.g. TelegramNotifier).
        job_url: The URL where captcha was encountered.
    """
    message = (
        "Boss直聘遇到验证码，本次投递已停止。请手动处理并重新运行。\n"
        f"岗位：{job_url}"
    )
    try:
        if hasattr(notifier, "send_text"):
            await notifier.send_text(message)
            logger.info("Captcha notification sent")
        else:
            logger.warning("Notifier does not support send_text")
    except Exception as e:
        logger.error("Failed to send captcha notification: %s", e)
