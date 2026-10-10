"""Feishu custom-bot webhook notifications."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

import httpx

logger = logging.getLogger(__name__)


class FeishuNotifier:
    def __init__(
        self, webhook_url: str, secret: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._secret = secret
        self._client = client

    async def send_text(self, text: str) -> bool:
        payload: dict = {"msg_type": "text", "content": {"text": text}}
        if self._secret:
            timestamp = str(int(time.time()))
            key = f"{timestamp}\n{self._secret}".encode("utf-8")
            digest = hmac.new(key, digestmod=hashlib.sha256).digest()
            payload.update({"timestamp": timestamp, "sign": base64.b64encode(digest).decode()})
        try:
            async def request(client: httpx.AsyncClient) -> dict:
                response = await client.post(self._webhook_url, json=payload, timeout=10)
                response.raise_for_status()
                return response.json()

            if self._client is None:
                async with httpx.AsyncClient() as client:
                    result = await request(client)
            else:
                result = await request(self._client)
            code = result.get("code", result.get("StatusCode", 0))
            if code != 0:
                raise ValueError(f"Feishu webhook error {code}: {result.get('msg', result)}")
            return True
        except Exception as exc:
            logger.error("Feishu notification failed: %s", exc)
            return False
