"""Manual Boss login and session checks using one persistent browser profile."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

from jobclaw.browser import BossBrowser, find_first_visible
from jobclaw.config import Settings

logger = logging.getLogger(__name__)

BOSS_LOGIN_URL = "https://www.zhipin.com/web/user/?ka=header-login"
BOSS_SUCCESS_SELECTORS = [".user-nav", ".nav-figure"]


def interactive_login(settings: Settings, timeout_minutes: int = 5) -> None:
    """Let the user log in; Chrome retains the session in its dedicated profile."""
    deadline = time.monotonic() + timeout_minutes * 60
    with BossBrowser(settings, headed=True) as browser:
        tab = browser.new_tab()
        if not tab.get(BOSS_LOGIN_URL, retry=0, timeout=30):
            raise ConnectionError(f"Could not open Boss login page: {BOSS_LOGIN_URL}")

        logger.info("Waiting for manual Boss login (timeout=%dm)", timeout_minutes)
        while time.monotonic() < deadline:
            if not tab.states.is_alive:
                break
            if _has_login_cookie(tab) and (
                "/web/geek/job" in tab.url
                or find_first_visible(tab, BOSS_SUCCESS_SELECTORS)
            ):
                logger.info("Boss login detected; browser profile will retain the session")
                return
            time.sleep(1.5)

    raise TimeoutError(
        f"Boss login was not detected within {timeout_minutes} minutes. "
        "Try `jobclaw login` again."
    )


def cookies_valid(settings: Settings) -> bool:
    """Check whether the saved browser profile reaches a signed-in Boss page."""
    params = urlencode({"city": settings.boss_city})
    check_url = f"https://www.zhipin.com/web/geek/jobs?{params}"
    try:
        with BossBrowser(settings) as browser:
            tab = browser.new_tab()
            if not tab.get(check_url, retry=0, timeout=15):
                return False
            if not tab.wait.eles_loaded(
                [f"css:{selector}" for selector in BOSS_SUCCESS_SELECTORS],
                timeout=10, any_one=True, raise_err=False,
            ):
                return False
            return (
                "/web/user" not in tab.url
                and _has_login_cookie(tab)
                and bool(find_first_visible(tab, BOSS_SUCCESS_SELECTORS))
            )
    except Exception as exc:
        logger.warning("Boss session check failed: %s", exc)
        return False


def _has_login_cookie(tab: object) -> bool:
    try:
        return any(
            cookie.get("name") == "wt2" and bool(cookie.get("value"))
            for cookie in tab.cookies(all_domains=True)  # type: ignore[attr-defined]
        )
    except Exception:
        return False
