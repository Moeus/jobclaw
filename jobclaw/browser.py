"""Boss Chromium session and selector compatibility for DrissionPage."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Self

from DrissionPage import Chromium, ChromiumOptions

from jobclaw.config import Settings

logger = logging.getLogger(__name__)

_HAS_TEXT = re.compile(
    r"^(?P<css>.+):has-text\((?P<quote>['\"])(?P<text>.*?)(?P=quote)\)$"
)


class BossBrowser:
    """Use one persistent Chrome profile for Boss login, search, and chat."""

    def __init__(self, settings: Settings, *, headed: bool = False) -> None:
        self._settings = settings
        self._headed = headed
        self._browser: Chromium | None = None
        self._created_here = False
        self._tabs: list[Any] = []

    def __enter__(self) -> Self:
        profile = Path(self._settings.boss_browser_user_data_dir).expanduser().resolve()
        profile.mkdir(parents=True, exist_ok=True)

        options = ChromiumOptions(read_file=False)
        options.set_local_port(self._settings.boss_browser_port)
        options.set_user_data_path(str(profile))
        options.headless(False if self._headed else self._settings.jobclaw_headless)
        # Keep JavaScript loading after navigation returns; wait for page content separately.
        options.set_load_mode("none")
        options.set_timeouts(base=10, page_load=30)
        options.set_retry(times=0)
        if self._settings.boss_browser_path:
            options.set_browser_path(self._settings.boss_browser_path)

        self._browser = Chromium(options)
        self._created_here = not self._browser.states.is_existed
        if self._headed and self._browser.states.is_headless:
            raise RuntimeError(
                "Boss browser is already running headless on the configured port. "
                "Close it or choose another BOSS_BROWSER_PORT before manual login."
            )
        logger.info("Boss browser profile: %s", profile)
        return self

    def __exit__(self, *args: object) -> None:
        for tab in self._tabs[:]:
            self.close_tab(tab)
        if self._browser and self._created_here:
            try:
                self._browser.quit()
            except Exception as exc:
                logger.warning("Could not close Boss browser cleanly: %s", exc)
        self._browser = None

    def new_tab(self) -> Any:
        if self._browser is None:
            raise RuntimeError("Boss browser is not open")
        tab = self._browser.new_tab()
        self._tabs.append(tab)
        return tab

    def tab_ids(self) -> set[str]:
        """Snapshot open tab IDs so a click-created chat tab can be identified."""
        if self._browser is None:
            raise RuntimeError("Boss browser is not open")
        return set(self._browser.tab_ids)

    def get_tab(self, tab_id: str) -> Any:
        """Inspect an existing tab without taking ownership of unrelated tabs."""
        if self._browser is None:
            raise RuntimeError("Boss browser is not open")
        return self._browser.get_tab(tab_id)

    def activate_tab(self, tab: Any) -> None:
        """Bring a retained search tab forward before scrolling its results."""
        if self._browser is None:
            raise RuntimeError("Boss browser is not open")
        self._browser.activate_tab(tab.tab_id)

    def track_tab(self, tab: Any) -> None:
        """Take ownership only after a new tab is confirmed as our chat page."""
        if tab not in self._tabs:
            self._tabs.append(tab)

    def close_tab(self, tab: Any) -> None:
        if tab in self._tabs:
            self._tabs.remove(tab)
        try:
            if tab.states.is_alive:
                tab.close()
        except Exception as exc:
            logger.debug("Could not close Boss tab: %s", exc)


def find_all(root: Any, selector: str) -> list[Any]:
    """Find descendants using the original CSS selector's purpose.

    DrissionPage accepts browser CSS through ``css:``. Playwright's
    ``:has-text()`` extension needs a CSS lookup followed by text filtering.
    """
    match = _HAS_TEXT.fullmatch(selector)
    css = match.group("css") if match else selector
    elements = list(root.eles(f"css:{css}", timeout=0))
    if match:
        wanted = _normalise(match.group("text"))
        elements = [element for element in elements if wanted in _normalise(element.text)]
    return elements


def find_first(root: Any, selector: str) -> Any | None:
    elements = find_all(root, selector)
    return elements[0] if elements else None


def find_first_visible(root: Any, selectors: list[str]) -> Any | None:
    """Keep the original fallback order and require a displayed element."""
    for selector in selectors:
        try:
            for element in find_all(root, selector):
                if element.states.is_displayed:
                    return element
        except Exception as exc:
            logger.debug("Boss selector %s failed: %s", selector, exc)
    return None


def _normalise(value: str) -> str:
    return " ".join(value.split()).casefold()
