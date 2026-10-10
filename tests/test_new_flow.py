"""Tests for resume, configuration, AI, and notification logic."""

from __future__ import annotations

import asyncio
import json
from functools import wraps
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from jobclaw.ai import JobAI
from jobclaw.cli import _resume_path, _salary_filter, _search_settings
from jobclaw.config import Settings
from jobclaw.models import Job, JobSource, SalaryRange
from jobclaw.notifier.feishu import FeishuNotifier
from jobclaw.resume import load_resume


def run_async(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return wrapper


@pytest.fixture
def job() -> Job:
    return Job(
        id="boss-123", source=JobSource.BOSS, title="LLM实习生", company="测试公司",
        location="北京", url="https://www.zhipin.com/job_detail/123.html",
        description="开发智能体应用，熟悉Python", tags=["Python", "LLM"],
        salary=SalaryRange(min_annual=120000, max_annual=180000), job_type="internship",
    )


def test_resume_accepts_markdown_without_schema(tmp_path: Path) -> None:
    path = tmp_path / "me.md"
    path.write_text("# 简历\n\n做过智能体项目", encoding="utf-8")
    assert "智能体项目" in load_resume(path)


def test_search_queries_are_split_and_deduplicated() -> None:
    settings = Settings(_env_file=None, search_queries="LLM实习生, Python实习生, LLM实习生")
    assert settings.queries == ["LLM实习生", "Python实习生"]


def test_boss_job_type_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOSS_JOB_TYPE", "1902")
    settings = Settings(_env_file=None, search_queries="LLM实习生")
    with patch("jobclaw.cli.get_settings", return_value=settings):
        assert _search_settings(None)[1] == "internship"
        assert _search_settings("1901")[1] == "full-time"


def test_resume_path_from_environment_and_override(tmp_path: Path) -> None:
    configured = tmp_path / "configured.md"
    override = tmp_path / "override.md"
    configured.write_text("简历", encoding="utf-8")
    override.write_text("另一份简历", encoding="utf-8")
    settings = Settings(_env_file=None, resume_path=str(configured))
    assert _resume_path(None, settings) == configured
    assert _resume_path(override, settings) == override


def test_salary_floor_is_optional(job: Job) -> None:
    off = Settings(_env_file=None, salary_filter_enabled=False)
    assert _salary_filter([job], off) == [job]
    on = Settings(_env_file=None, salary_filter_enabled=True, min_annual_salary=190000)
    assert _salary_filter([job], on) == []


@run_async
async def test_ai_uses_resume_and_job_for_matching_and_greeting(job: Job) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        content = (
            '{"score": 0.91, "reasoning": ["项目匹配"], "matched_skills": ["Python"]}'
            if len(seen) == 1 else "你好，我做过智能体项目，想了解贵公司的LLM实习岗位。"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    settings = Settings(_env_file=None, openai_api_key="test", salary_filter_enabled=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = JobAI(settings, client)
        match = await ai.match_job("# 简历\n做过智能体项目", job)
        greeting = await ai.generate_greeting("# 简历\n做过智能体项目", job)

    assert match.score == 0.91
    assert "智能体项目" in greeting
    assert all("智能体项目" in call["messages"][1]["content"] for call in seen)
    assert all("开发智能体应用" in call["messages"][1]["content"] for call in seen)


@run_async
async def test_feishu_webhook_payload_and_signature() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = FeishuNotifier("https://open.feishu.cn/open-apis/bot/v2/hook/test", "secret", client)
        assert await notifier.send_text("测试消息")
    assert bodies[0]["content"]["text"] == "测试消息"
    assert bodies[0]["timestamp"] and bodies[0]["sign"]
