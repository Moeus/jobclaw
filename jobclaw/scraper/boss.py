"""Boss直聘 (zhipin.com) scraper using DrissionPage."""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from hashlib import sha256
from typing import Any, Callable, Self
from urllib.parse import urlencode, urljoin

from jobclaw.browser import BossBrowser, find_all, find_first
from jobclaw.config import Settings
from jobclaw.models import Job, JobSource, SalaryRange
from jobclaw.scraper.base import BaseScraper

logger = logging.getLogger(__name__)

JOB_TYPE_CODES = {"all": None, "full-time": "1901", "internship": "1902", "part-time": "1903"}
_JOB_LINK_SELECTOR = "a[href*='/job_detail/']"
_COMPANY_LINK_SELECTOR = "a[href*='/gongsi/']"
_CARD_SELECTORS = ("li.job-card-box", ".job-card-wrapper")
_CARD_WAIT_SECONDS = 30
_SCROLL_WAIT_SECONDS = 3
_MAX_STALLED_SCROLLS = 4


class BossSearchUnavailable(RuntimeError):
    """The Boss search page did not become usable for scraping."""


class BossScraper(BaseScraper):
    """Scrape job listings with a persistent Boss browser profile."""

    source = JobSource.BOSS

    def __init__(self, settings: Settings, *, browser: BossBrowser | None = None) -> None:
        self._settings = settings
        self._browser = browser
        self._owns_browser = False

    def __enter__(self) -> Self:
        if self._browser is None:
            self._browser = BossBrowser(self._settings)
            self._browser.__enter__()
            self._owns_browser = True
        return self

    def __exit__(self, *args: object) -> None:
        if self._browser and self._owns_browser:
            self._browser.__exit__(*args)
            self._browser = None
            self._owns_browser = False

    def scrape_jobs(
        self,
        query: str,
        location: str | None = None,
        limit: int = 20,
        job_type: str = "all",
    ) -> list[Job]:
        """Scrape one search term; leave site navigation retries disabled."""
        if not self._browser:
            raise RuntimeError("Scraper not initialized. Use 'with' context.")
        if job_type not in JOB_TYPE_CODES:
            raise ValueError(f"Unsupported Boss job type: {job_type}")

        params = {"city": self._settings.boss_city, "query": query}
        if JOB_TYPE_CODES[job_type]:
            params["jobType"] = JOB_TYPE_CODES[job_type]
        search_url = f"https://www.zhipin.com/web/geek/jobs?{urlencode(params)}"

        jobs: list[Job] = []
        tab = self._browser.new_tab()
        try:
            # `none` load mode returns after navigation starts. Wait for real cards,
            # while retry=0 avoids DrissionPage's default repeat navigations.
            if not tab.get(search_url, retry=0, timeout=30):
                raise BossSearchUnavailable(f"Boss search navigation failed: {search_url}")
            cards = _wait_for_job_cards(tab, timeout=_CARD_WAIT_SECONDS)
            if not cards:
                raise BossSearchUnavailable(
                    f"Boss search did not show job cards (current URL: {tab.url}). "
                    f"Observed: {_card_diagnostics(tab)}. "
                    "The result list may still be loading, empty, or refreshing. "
                    "Check the visible browser and run `jobclaw login --check` if needed."
                )

            for card, job_link in cards[:limit]:
                job = self._parse_card(card, job_link, location, job_type)
                if job is not None:
                    jobs.append(job)

        except BossSearchUnavailable:
            raise
        except Exception as exc:
            detail = str(exc).splitlines()
            raise BossSearchUnavailable(
                f"Boss search failed (current URL: {tab.url}): "
                f"{detail[0] if detail else type(exc).__name__}"
            ) from exc
        finally:
            self._browser.close_tab(tab)

        logger.info("Boss: scraped %d jobs for query '%s'", len(jobs), query)
        return jobs

    def open_search(
        self, query: str, location: str | None = None,
        job_type: str = "all", *, progress: Callable[[str], None] | None = None,
    ) -> BossSearchSession:
        """Keep a result tab open and load more cards only when needed."""
        if not self._browser:
            raise RuntimeError("Scraper not initialized. Use 'with' context.")
        return BossSearchSession(self, query, location, job_type, progress)

    def _parse_card(
        self, card: Any, job_link: Any, location: str | None, job_type: str,
    ) -> Job | None:
        try:
            # Preserve the original selectors and the fields they identify.
            title_el = find_first(card, ".job-name")
            company_el = find_first(card, ".company-name a")
            if not company_el:
                company_el = find_first(card, _COMPANY_LINK_SELECTOR)
            salary_el = find_first(card, ".salary")
            location_el = find_first(card, ".job-area")
            link_el = find_first(card, ".job-card-left a")
            tags_els = find_all(card, ".tag-list span")
            desc_el = find_first(card, ".job-card-desc")

            title = title_el.text if title_el else job_link.text
            company = company_el.text if company_el else "Unknown"
            salary_text = salary_el.text if salary_el else ""
            location_text = location_el.text if location_el else ""
            href = link_el.attr("href") if link_el else ""
            if not _valid_job_href(href):
                href = job_link.attr("href")
            tags = [element.text for element in tags_els]
            desc = desc_el.text if desc_el else card.text

            if not href:
                logger.warning("Skipping Boss card without a job URL")
                return None
            url = urljoin("https://www.zhipin.com", href)
            salary = (
                _parse_boss_salary(salary_text)
                if self._settings.salary_filter_enabled else None
            )

            return Job(
                id=f"boss-{sha256(url.encode()).hexdigest()[:20]}",
                source=JobSource.BOSS,
                title=title.strip(),
                company=company.strip(),
                location=(location_text.strip() or location or (
                    "全国" if self._settings.boss_city == "100010000"
                    else self._settings.boss_city
                )),
                url=url,
                description=desc.strip(),
                salary=salary,
                tags=tags,
                job_type=job_type if job_type != "all" else None,
            )
        except Exception as exc:
            logger.warning("Failed to parse Boss card: %s", exc)
            return None


class BossSearchSession:
    """Read cards from one live search tab without reloading or closing it."""

    def __init__(
        self, scraper: BossScraper, query: str, location: str | None,
        job_type: str, progress: Callable[[str], None] | None,
    ) -> None:
        self._scraper = scraper
        self.query = query
        self._location = location
        self._job_type = job_type
        self._progress = progress
        self._tab: Any | None = None
        self._pending: deque[Job] = deque()
        self._seen_urls: set[str] = set()
        self._exhausted = False
        self.cards_read = 0
        self.cards_returned = 0
        self.scrolls = 0

    def __enter__(self) -> Self:
        if self._job_type not in JOB_TYPE_CODES:
            raise ValueError(f"Unsupported Boss job type: {self._job_type}")
        browser = self._scraper._browser
        assert browser is not None
        params = {"city": self._scraper._settings.boss_city, "query": self.query}
        if JOB_TYPE_CODES[self._job_type]:
            params["jobType"] = JOB_TYPE_CODES[self._job_type]
        url = f"https://www.zhipin.com/web/geek/jobs?{urlencode(params)}"
        self._tab = browser.new_tab()
        try:
            if not self._tab.get(url, retry=0, timeout=30):
                raise BossSearchUnavailable(f"Boss search navigation failed: {url}")
            cards = _wait_for_job_cards(self._tab, timeout=_CARD_WAIT_SECONDS)
            if not cards:
                raise BossSearchUnavailable(
                    f"Boss search did not show job cards (current URL: {self._tab.url}). "
                    f"Observed: {_card_diagnostics(self._tab)}. "
                    "The result list may still be loading, empty, or refreshing. "
                    "Check the visible browser and run `jobclaw login --check` if needed."
                )
            self._capture(cards)
            self._emit(f"搜索页已加载：当前读取 {self.cards_read} 张卡片，标签页保持打开")
            return self
        except BossSearchUnavailable:
            self.close()
            raise
        except Exception as exc:
            self.close()
            detail = str(exc).splitlines()
            raise BossSearchUnavailable(
                f"Boss search failed (search URL: {url}): "
                f"{detail[0] if detail else type(exc).__name__}"
            ) from exc

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._tab is not None:
            browser = self._scraper._browser
            if browser is not None:
                browser.close_tab(self._tab)
            self._tab = None

    def next_job(self, *, max_cards: int | None = None) -> Job | None:
        """Return the next unseen card, scrolling only after loaded cards run out."""
        if self._tab is None:
            raise RuntimeError("Boss search session is closed")
        if self._exhausted or (max_cards is not None and self.cards_returned >= max_cards):
            return None
        if self._pending:
            return self._pop_job()
        self._capture_current()
        if self._pending:
            return self._pop_job()

        for stalled in range(1, _MAX_STALLED_SCROLLS + 1):
            self.scrolls += 1
            self._emit(f"已处理当前卡片；搜索页下滑第 {self.scrolls} 次，等待新卡片")
            browser = self._scraper._browser
            assert browser is not None
            browser.activate_tab(self._tab)
            position = _scroll_results(self._tab)
            logger.info("Boss search '%s' scroll position: %s", self.query, position)
            deadline = time.monotonic() + _SCROLL_WAIT_SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.3)
                self._capture_current()
                if self._pending:
                    self._emit(f"下滑后找到新卡片；累计读取 {self.cards_read} 张")
                    return self._pop_job()
            self._emit(f"本次下滑未出现新卡片（{stalled}/{_MAX_STALLED_SCROLLS}）")

        self._exhausted = True
        self._emit(f"连续 {_MAX_STALLED_SCROLLS} 次下滑没有新卡片，结束此搜索词")
        return None

    def _pop_job(self) -> Job:
        self.cards_returned += 1
        return self._pending.popleft()

    def _capture_current(self) -> None:
        try:
            self._capture(_current_job_cards(self._tab))
        except Exception as exc:
            logger.debug("Boss search cards changed while reading: %s", exc)

    def _capture(self, cards: list[tuple[Any, Any]]) -> None:
        for card, link in cards:
            try:
                href = link.attr("href")
                if not _valid_job_href(href):
                    continue
                url = urljoin("https://www.zhipin.com", href)
                if url in self._seen_urls:
                    continue
                self._seen_urls.add(url)
                job = self._scraper._parse_card(card, link, self._location, self._job_type)
                if job is not None:
                    self._pending.append(job)
                    self.cards_read += 1
            except Exception as exc:
                logger.debug("Boss card changed while reading: %s", exc)

    def _emit(self, message: str) -> None:
        logger.info("Boss search '%s': %s", self.query, message)
        if self._progress:
            self._progress(message)


def _valid_job_href(href: str | None) -> bool:
    """Exclude the footer's generic /job_detail/ link and other non-jobs."""
    return bool(href and re.search(r"/job_detail/[^/?#]+\.html(?:[?#]|$)", href))


def _job_links(root: Any) -> list[Any]:
    return [
        link for link in find_all(root, _JOB_LINK_SELECTOR)
        if _valid_job_href(link.attr("href"))
    ]


def _card_for_link(link: Any) -> Any | None:
    """Find the smallest ancestor containing this job and its company."""
    parent = link
    for _ in range(6):
        parent = parent.parent()
        if not parent:
            break
        if len(_job_links(parent)) != 1:
            break
        if find_first(parent, ".company-name a") or find_first(parent, _COMPANY_LINK_SELECTOR):
            return parent
    return None


def _wait_for_job_cards(tab: Any, timeout: float) -> list[tuple[Any, Any]]:
    """Wait for genuine job links, not a still-rendering list or skeleton."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            cards = _current_job_cards(tab)
            if cards:
                return cards
        except Exception as exc:
            logger.debug("Boss result list is still changing: %s", exc)
        time.sleep(0.25)
    return []


def _current_job_cards(tab: Any) -> list[tuple[Any, Any]]:
    """Read the currently rendered cards using the original selector fallbacks."""
    cards: list[tuple[Any, Any]] = []
    # The current layout uses li.job-card-box; retain .job-card-wrapper
    # for the older layout and keep links scoped to their card.
    for selector in _CARD_SELECTORS:
        for card in find_all(tab, selector):
            links = _job_links(card)
            if links:
                cards.append((card, links[0]))
        if cards:
            return cards

    # Some result pages use another wrapper but still expose a genuine job URL.
    seen: set[str] = set()
    for link in _job_links(tab):
        card = _card_for_link(link)
        href = link.attr("href")
        if card and href not in seen:
            cards.append((card, link))
            seen.add(href)
    if cards:
        logger.info("Boss cards found through job-detail links")
    return cards


def _scroll_results(tab: Any) -> dict[str, Any]:
    """Scroll the list's own container, or the document when it owns scrolling."""
    return tab.run_js("""(() => {
        const cards = [...document.querySelectorAll('li.job-card-box, .job-card-wrapper')].filter(
            card => [...card.querySelectorAll("a[href*='/job_detail/']")].some(
                link => (link.getAttribute('href') || '').includes('.html')
            )
        );
        const links = document.querySelectorAll("a[href*='/job_detail/']");
        const last = cards[cards.length - 1] || [...links].reverse().find(
            link => (link.getAttribute('href') || '').includes('.html')
        );
        if (last) last.scrollIntoView({block: 'end'});
        for (let el = last && last.parentElement; el; el = el.parentElement) {
            const style = getComputedStyle(el);
            if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 5) {
                el.scrollTop = el.scrollHeight;
                return {target: 'results', top: el.scrollTop, height: el.scrollHeight};
            }
        }
        const page = document.scrollingElement || document.documentElement;
        page.scrollTop = page.scrollHeight;
        return {target: 'page', top: page.scrollTop, height: page.scrollHeight};
    })()""")


def _card_diagnostics(tab: Any) -> str:
    """Report observed DOM counts without dumping page or account data."""
    try:
        current_cards = len(find_all(tab, "li.job-card-box"))
        old_wrappers = len(find_all(tab, ".job-card-wrapper"))
        links = len(_job_links(tab))
        return (
            f"li.job-card-box={current_cards}, .job-card-wrapper={old_wrappers}, "
            f"job-detail links={links}"
        )
    except Exception as exc:
        return f"DOM unavailable ({type(exc).__name__})"


def _parse_boss_salary(text: str) -> SalaryRange | None:
    """Parse Boss salary text like '25-50K·16薪' into a CNY annual range."""
    match = re.search(r"(\d+)-(\d+)K", text)
    if not match:
        return None
    low = int(match.group(1)) * 1000
    high = int(match.group(2)) * 1000
    bonus_match = re.search(r"(\d+)薪", text)
    months = int(bonus_match.group(1)) if bonus_match else 12
    return SalaryRange(
        min_annual=low * months,
        max_annual=high * months,
        currency="CNY",
    )
