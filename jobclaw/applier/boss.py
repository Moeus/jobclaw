"""Boss直聘 auto-apply adapter — 打招呼 (chat greeting) flow."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Callable, Self
from urllib.parse import parse_qs, urlsplit

from DrissionPage.common import Keys

from jobclaw.ai import JobAI
from jobclaw.applier.base import BaseApplier
from jobclaw.applier.captcha import detect_captcha, notify_captcha
from jobclaw.applier.history import ApplyHistory
from jobclaw.browser import BossBrowser, find_all, find_first, find_first_visible
from jobclaw.config import Settings
from jobclaw.models import Application, ApplicationStatus, Job, JobSource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selectors — multiple fallbacks for resilience
# ---------------------------------------------------------------------------

_BTN_START_CHAT = [
    "a.btn-startchat",                          # primary CTA
    ".job-detail-box .btn-startchat",
    "a[ka='job_detail_chat']",
    "a:has-text('立即沟通')",
    "div.job-op a.btn",                          # broad fallback
]

_BTN_CONTINUE_CHAT = [
    "a:has-text('继续沟通')",
    "a.btn-startchat:has-text('继续沟通')",
]

_CHAT_INPUT = [
    ".chat-conversation .chat-editor #chat-input[contenteditable='true']",  # captured chat composer
    "#chat-input",
    ".chat-input textarea",
    "div.chat-editor textarea",
    "[contenteditable='true'].chat-input",
    "div.edit-area [contenteditable='true']",
    "textarea[name='msg']",
]

_BTN_SEND = [
    ".chat-conversation .chat-op button.btn-send",  # captured chat send control
    "button.btn-send",
    "button:has-text('发送')",
    ".chat-op button[type='submit']",
    "div.message-controls button.btn-v2",
]

_SENT_MESSAGES = ".chat-conversation .chat-record li.message-item"
_STARTCHAT_DIALOG = ".startchat-dialog"
_DIALOG_INPUT = ".startchat-dialog .edit-area textarea.input-area"
_DIALOG_SEND = ".startchat-dialog .edit-area .send-message"

_DAILY_LIMIT_TEXT = [
    "今日沟通人数已达上限",
    "今日投递次数已用完",
    "今天的机会已用完",
]


class BossApplier(BaseApplier):
    """Auto-apply to jobs on Boss直聘 via the 打招呼 chat flow.

    Usage::

        async with BossApplier(settings, ai) as applier:
            result = await applier.apply(job, resume_text)

    Search, login, and chat use the same persistent Boss browser profile.
    """

    def __init__(
        self,
        settings: Settings,
        ai: JobAI,
        *,
        notifier: object | None = None,
        history: ApplyHistory | None = None,
        progress: Callable[[str], None] | None = None,
        browser: BossBrowser | None = None,
    ) -> None:
        self._settings = settings
        self._ai = ai
        self._notifier = notifier
        self._history = history or ApplyHistory()
        self._progress = progress
        self._browser = browser
        self._owns_browser = False

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> Self:
        if self._browser is None:
            self._browser = BossBrowser(self._settings)
            self._browser.__enter__()
            self._owns_browser = True
        logger.info("BossApplier browser opened (headless=%s)", self._settings.jobclaw_headless)
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._browser and self._owns_browser:
            self._browser.__exit__(*args)
            self._browser = None
            self._owns_browser = False
        logger.info("BossApplier browser closed")

    # -- public API ---------------------------------------------------------

    async def apply(
        self, job: Job, resume_text: str, *, greeting: str | None = None,
        query: str | None = None,
    ) -> Application:  # noqa: C901
        """Send a greeting to the recruiter on Boss直聘.

        Returns an Application with appropriate status:
          - SUBMITTED  — greeting visible in the active conversation
          - FAILED     — unrecoverable error (captcha, limit, etc.)
          - CAPTCHA_BLOCKED — captcha detected, manual intervention needed
        """
        t0 = time.monotonic()
        logger.info("Boss apply START: %s @ %s [%s]", job.title, job.company, job.url)

        # --- pre-flight checks ------------------------------------------------
        if self._history.is_applied(job.id):
            logger.info("Skipped (already applied): %s", job.id)
            return self._make_app(
                job, ApplicationStatus.SUBMITTED,
                extra={"reason": "already_applied"},
            )

        if self._history.today_count() >= self._settings.boss_daily_limit:
            logger.warning("Daily limit reached (%d)", self._settings.boss_daily_limit)
            return self._make_app(
                job, ApplicationStatus.FAILED,
                extra={"reason": "daily_limit"},
            )

        if not self._browser:
            raise RuntimeError("BossApplier not initialised — use 'async with'.")

        page = self._browser.new_tab()
        opened_tabs = [page]

        try:
            return await self._do_apply(page, job, resume_text, t0, greeting, opened_tabs, query)
        except Exception as exc:
            logger.exception("Unexpected error during Boss apply: %s", exc)
            return self._make_app(
                job, ApplicationStatus.FAILED,
                extra={"reason": "unexpected_error", "error": str(exc)},
            )
        finally:
            for tab in reversed(opened_tabs):
                self._browser.close_tab(tab)

    # -- internal flow ------------------------------------------------------

    async def _do_apply(  # noqa: C901
        self,
        page: Any,
        job: Job,
        resume_text: str,
        t0: float,
        greeting: str | None,
        opened_tabs: list[Any],
        query: str | None,
    ) -> Application:
        """Core apply flow inside a browser page."""

        # 1. Navigate to job detail page
        logger.info("Navigating to %s", job.url)
        if not page.get(str(job.url), retry=0, timeout=30):
            raise RuntimeError(f"Could not open Boss job page: {job.url}")
        await self._human_delay(0.5, 1.5)

        # 2. Captcha check
        if detect_captcha(page):
            if self._notifier:
                await notify_captcha(self._notifier, str(job.url))
            return self._make_app(
                job, ApplicationStatus.CAPTCHA_BLOCKED,
                extra={"reason": "captcha"},
            )

        # 3. Check daily limit text on page
        body_el = find_first(page, "body")
        body = body_el.text if body_el else ""
        for limit_text in _DAILY_LIMIT_TEXT:
            if limit_text in body:
                logger.warning("Daily limit detected on page: %s", limit_text)
                return self._make_app(
                    job, ApplicationStatus.FAILED,
                    extra={"reason": "daily_limit"},
                )

        # 4. Find the detail-page action before opening any other tab.
        chat_btn = await self._find_element(page, _BTN_CONTINUE_CHAT + _BTN_START_CHAT)
        if not chat_btn:
            logger.error("Cannot find a chat button for %s", job.url)
            return self._make_app(
                job, ApplicationStatus.FAILED,
                extra={"reason": "button_not_found"},
            )

        # Generate before opening chat; direct callers may not pass a prepared greeting.
        if greeting is None:
            greeting = await self._ai.generate_greeting(resume_text, job)
        # Enter sends on this page. Keep the AI text on one line while typing.
        greeting = " ".join(greeting.split())
        if not greeting:
            raise ValueError("AI greeting is empty")

        # The button can change from "立即沟通" to "继续沟通" while the AI runs.
        chat_btn = find_first_visible(page, _BTN_CONTINUE_CHAT) or find_first_visible(page, _BTN_START_CHAT)
        if not chat_btn:
            return self._make_app(job, ApplicationStatus.FAILED, extra={"reason": "button_disappeared"})

        action = "continue" if "继续沟通" in chat_btn.text else "start"
        known_tabs = self._browser.tab_ids() if action == "continue" else set()
        self._emit(f"点击{'继续沟通，等待聊天页' if action == 'continue' else '立即沟通，等待弹窗'}")
        logger.info("Clicking Boss %s chat button", action)
        # A click can consume Boss's daily contact quota even if later UI
        # confirmation fails. Count it before clicking to avoid over-contacting.
        self._history.mark_applied(job.id, "attempted", query=query)
        if not chat_btn.click():
            return self._make_app(
                job, ApplicationStatus.FAILED,
                extra={"reason": "chat_button_click_failed"},
            )

        # 5. "立即沟通" uses an in-page dialog; "继续沟通" navigates to chat.
        active_page = page
        confirmation_location = "聊天页"
        if action == "start":
            failure, confirmation_location = await self._send_in_start_dialog(
                page, job, greeting, opened_tabs,
            )
        else:
            chat_page = await self._wait_for_chat_page(
                page, known_tabs, str(job.url), opened_tabs,
            )
            if chat_page is None:
                failure = "chat_page_not_opened"
            else:
                active_page = chat_page
                failure = await self._send_in_chat(chat_page, job, greeting)

        if failure:
            if detect_captcha(active_page):
                if self._notifier:
                    await notify_captcha(self._notifier, str(job.url))
                return self._make_app(
                    job, ApplicationStatus.CAPTCHA_BLOCKED,
                    extra={"reason": "captcha", "channel": action},
                )
            return self._make_app(
                job, ApplicationStatus.FAILED,
                extra={"reason": failure, "channel": action},
            )

        if detect_captcha(active_page):
            if self._notifier:
                await notify_captcha(self._notifier, str(job.url))
            return self._make_app(
                job, ApplicationStatus.CAPTCHA_BLOCKED,
                extra={"reason": "captcha"},
            )

        # Only a message visible in the dialog or chat record enters history.
        self._history.mark_applied(job.id, "submitted", query=query)
        self._emit(f"发送已在{confirmation_location}显示，已记录投递")
        elapsed = time.monotonic() - t0
        logger.info(
            "Boss apply SUCCESS: %s @ %s (%.1fs)",
            job.title, job.company, elapsed,
        )

        # Inter-apply delay (human-like pacing)
        delay = random.uniform(
            self._settings.boss_apply_delay_min,
            self._settings.boss_apply_delay_max,
        )
        logger.debug("Waiting %.1fs before next apply", delay)
        await asyncio.sleep(delay)

        return self._make_app(
            job, ApplicationStatus.SUBMITTED,
            extra={
                "greeting_sent": greeting,
                "channel": action,
                "response_time": round(elapsed, 2),
            },
        )

    # -- helpers ------------------------------------------------------------

    def _emit(self, message: str) -> None:
        logger.info("Boss apply: %s", message)
        if self._progress is not None:
            self._progress(message)

    async def _send_in_start_dialog(
        self, page: Any, job: Job, greeting: str, opened_tabs: list[Any],
    ) -> tuple[str | None, str]:
        """Send through the in-page dialog opened by 立即沟通."""
        dialog = await self._find_element(page, [_STARTCHAT_DIALOG], timeout=15)
        if not dialog:
            return "startchat_dialog_not_opened", "弹窗"
        chat_input = await self._find_element(page, [_DIALOG_INPUT])
        if not chat_input:
            return "dialog_input_not_found", "弹窗"

        if self._dialog_message_count(page, greeting):
            self._emit("相同招呼语已在弹窗显示，跳过重复发送")
            return None, "弹窗"
        self._emit("在弹窗输入招呼语")
        if not chat_input.click():
            return "dialog_input_click_failed", "弹窗"
        chat_input.input(greeting, clear=True)
        if not await self._wait_for_input_text(chat_input, greeting):
            return "dialog_input_not_filled", "弹窗"

        send_button = await self._find_enabled_dialog_send(page)
        if not send_button:
            return "dialog_send_disabled", "弹窗"
        self._emit("点击弹窗发送")
        if not send_button.click():
            return "dialog_send_click_failed", "弹窗"

        if await self._wait_for_dialog_message(page, greeting):
            return None, "弹窗"

        # Some versions close the dialog instead of retaining a message row.
        # Verify in the actual conversation before recording this application.
        self._emit("弹窗未显示发送记录，打开聊天页核对")
        if await self._verify_dialog_send_in_chat(page, job, greeting, opened_tabs):
            return None, "聊天页"
        return "dialog_send_unconfirmed", "聊天页"

    async def _verify_dialog_send_in_chat(
        self, page: Any, job: Job, greeting: str, opened_tabs: list[Any],
    ) -> bool:
        assert self._browser is not None
        close_button = find_first_visible(page, [".startchat-dialog a.close"])
        if close_button:
            if not close_button.click():
                return False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not find_first_visible(page, [_STARTCHAT_DIALOG]):
                    break
                await asyncio.sleep(0.1)
            else:
                return False
        continue_button = await self._find_element(page, _BTN_CONTINUE_CHAT, timeout=5)
        if not continue_button:
            return False
        known_tabs = self._browser.tab_ids()
        if not continue_button.click():
            return False
        chat_page = await self._wait_for_chat_page(
            page, known_tabs, str(job.url), opened_tabs,
        )
        if not chat_page:
            return False
        if not await self._wait_for_chat_context(chat_page, str(job.url), job.title):
            return False
        return await self._wait_for_sent_message(chat_page, greeting, 0)

    @classmethod
    def _dialog_message_count(cls, page: Any, greeting: str) -> int:
        message_list = find_first(page, ".startchat-dialog .message-list")
        if not message_list:
            return 0
        return int(cls._normalise(greeting) in cls._normalise(message_list.text))

    @classmethod
    async def _wait_for_dialog_message(
        cls, page: Any, greeting: str, timeout: float = 8,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cls._dialog_message_count(page, greeting):
                return True
            await asyncio.sleep(0.25)
        return False

    @staticmethod
    async def _find_enabled_dialog_send(page: Any, timeout: float = 10) -> Any | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            button = find_first_visible(page, [_DIALOG_SEND])
            if button and "disable" not in (button.attr("class") or "").split():
                return button
            await asyncio.sleep(0.25)
        return None

    async def _send_in_chat(self, chat_page: Any, job: Job, greeting: str) -> str | None:
        """Send through the full chat page opened by 继续沟通."""
        self._emit("聊天页已打开，核对岗位")
        if not await self._wait_for_chat_context(chat_page, str(job.url), job.title):
            return "wrong_chat_conversation"
        chat_input = await self._find_element(chat_page, _CHAT_INPUT)
        if not chat_input:
            return "chat_input_not_found"

        before_count = self._sent_message_count(chat_page, greeting)
        if before_count:
            self._emit("相同招呼语已在聊天记录中，跳过重复发送")
            return None

        self._emit("在聊天页输入招呼语")
        if not chat_input.click():
            return "chat_input_click_failed"
        chat_input.input(greeting, clear=True)
        if not await self._wait_for_input_text(chat_input, greeting):
            return "chat_input_not_filled"

        send_button = await self._find_enabled_send_button(chat_page)
        self._emit("点击聊天页发送")
        if send_button:
            if not send_button.click():
                return "send_click_failed"
        elif self._find_send_control(chat_page):
            return "send_button_disabled"
        else:
            chat_input.input(Keys.ENTER)

        if not await self._wait_for_sent_message(chat_page, greeting, before_count):
            return "send_unconfirmed"
        return None

    async def _wait_for_chat_page(
        self,
        detail_page: Any,
        known_tabs: set[str],
        job_url: str,
        opened_tabs: list[Any],
        timeout: float = 30,
    ) -> Any | None:
        """Find the chat opened by the detail-page button, in either tab."""
        assert self._browser is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            candidates = [detail_page]
            try:
                for tab_id in self._browser.tab_ids() - known_tabs:
                    candidates.append(self._browser.get_tab(tab_id))
            except Exception as exc:
                logger.debug("Waiting for Boss chat tab: %s", exc)

            for tab in candidates:
                try:
                    url = tab.url
                    parsed = urlsplit(url)
                    if parsed.hostname not in ("zhipin.com", "www.zhipin.com"):
                        continue
                    if parsed.path != "/web/geek/chat":
                        continue
                    if not self._chat_url_matches_job(url, job_url):
                        continue
                    if tab.tab_id != detail_page.tab_id and not any(
                        existing.tab_id == tab.tab_id for existing in opened_tabs
                    ):
                        self._browser.track_tab(tab)
                        opened_tabs.append(tab)
                    if find_first_visible(tab, _CHAT_INPUT):
                        return tab
                except Exception as exc:
                    logger.debug("Boss chat is still loading: %s", exc)
            await asyncio.sleep(0.25)
        return None

    @staticmethod
    def _chat_url_matches_job(chat_url: str, job_url: str) -> bool:
        """Reject a chat tab explicitly linked to another job."""
        chat_job_id = parse_qs(urlsplit(chat_url).query).get("jobId", [None])[0]
        expected_job_id = urlsplit(job_url).path.rsplit("/", 1)[-1].removesuffix(".html")
        return chat_job_id is None or chat_job_id == expected_job_id

    @classmethod
    def _chat_matches_job(cls, chat_page: Any, job_url: str, title: str) -> bool:
        """Check the chat URL or visible position before typing any message."""
        chat_url = chat_page.url
        if not cls._chat_url_matches_job(chat_url, job_url):
            return False
        position = find_first(chat_page, ".chat-conversation .position-name")
        return bool(position and cls._normalise(position.text) == cls._normalise(title))

    @classmethod
    async def _wait_for_chat_context(
        cls, chat_page: Any, job_url: str, title: str, timeout: float = 10,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cls._chat_matches_job(chat_page, job_url, title):
                return True
            await asyncio.sleep(0.25)
        return False

    @classmethod
    def _sent_message_count(cls, chat_page: Any, greeting: str) -> int:
        """Count matching outgoing bubbles, excluding recruiter messages."""
        wanted = cls._normalise(greeting)
        count = 0
        for message in find_all(chat_page, _SENT_MESSAGES):
            classes = (message.attr("class") or "").split()
            if "item-friend" in classes:
                continue
            if wanted in cls._normalise(message.text):
                count += 1
        return count

    @classmethod
    async def _wait_for_sent_message(
        cls, chat_page: Any, greeting: str, previous_count: int, timeout: float = 12,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cls._sent_message_count(chat_page, greeting) > previous_count:
                return True
            await asyncio.sleep(0.25)
        return False

    @classmethod
    async def _wait_for_input_text(
        cls, chat_input: Any, greeting: str, timeout: float = 3,
    ) -> bool:
        deadline = time.monotonic() + timeout
        wanted = cls._normalise(greeting)
        while time.monotonic() < deadline:
            current = (
                chat_input.property("value")
                if chat_input.tag in ("input", "textarea") else chat_input.text
            )
            if wanted in cls._normalise(current or ""):
                return True
            await asyncio.sleep(0.1)
        return False

    @staticmethod
    def _find_send_control(page: Any) -> Any | None:
        """Use the original send selectors without matching '发送验证码'."""
        for selector in _BTN_SEND:
            try:
                for button in find_all(page, selector):
                    if button.states.is_displayed and button.text.strip() == "发送":
                        return button
            except Exception as exc:
                logger.debug("Boss send selector %s failed: %s", selector, exc)
        return None

    @staticmethod
    async def _find_enabled_send_button(page: Any, timeout: float = 10) -> Any | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            button = BossApplier._find_send_control(page)
            if button:
                classes = (button.attr("class") or "").split()
                if "disabled" not in classes and button.states.is_enabled:
                    return button
            await asyncio.sleep(0.25)
        return None

    @staticmethod
    def _normalise(value: str) -> str:
        return " ".join(value.split())

    @staticmethod
    async def _find_element(
        page: Any, selectors: list[str], timeout: float = 10,
    ) -> Any | None:
        """Poll the original fallback selectors until one is visible."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            element = find_first_visible(page, selectors)
            if element:
                return element
            await asyncio.sleep(0.25)
        return None

    @staticmethod
    async def _human_delay(lo: float = 0.5, hi: float = 1.5) -> None:
        """Sleep for a random duration to mimic human interaction."""
        await asyncio.sleep(random.uniform(lo, hi))

    @staticmethod
    def _make_app(
        job: Job,
        status: ApplicationStatus,
        *,
        extra: dict | None = None,
    ) -> Application:
        """Create an Application result."""
        return Application(
            job_id=job.id,
            source=JobSource.BOSS,
            status=status,
            message=f"{job.title} @ {job.company}",
            applied_at=datetime.now(timezone.utc) if status == ApplicationStatus.SUBMITTED else None,
            extra=extra or {},
        )
