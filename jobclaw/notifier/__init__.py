"""Notifier subpackage for alerts and status updates."""

from __future__ import annotations

from jobclaw.config import Settings
from jobclaw.notifier.discord import DiscordNotifier
from jobclaw.notifier.feishu import FeishuNotifier
from jobclaw.notifier.telegram import TelegramNotifier


class BroadcastNotifier:
    def __init__(self, settings: Settings) -> None:
        self._channels = []
        if settings.telegram_bot_token and settings.telegram_chat_id:
            self._channels.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id))
        if settings.discord_webhook_url:
            self._channels.append(DiscordNotifier(settings.discord_webhook_url))
        if settings.feishu_webhook_url:
            self._channels.append(
                FeishuNotifier(settings.feishu_webhook_url, settings.feishu_webhook_secret)
            )

    async def send_text(self, text: str) -> bool:
        results = [await channel.send_text(text) for channel in self._channels]
        return bool(results) and all(results)

    @property
    def configured(self) -> bool:
        return bool(self._channels)
