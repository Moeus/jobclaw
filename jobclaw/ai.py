"""One OpenAI-compatible AI service for ranking jobs and writing greetings."""

from __future__ import annotations

import json
import logging
import re

import httpx

from jobclaw.config import Settings
from jobclaw.models import Job, Match

logger = logging.getLogger(__name__)


class JobAI:
    """Use Chat Completions with OpenAI, Qwen/DashScope, or a compatible server."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for AI greetings")
        self._settings = settings
        self._client = client

    async def _complete(self, system: str, user: str) -> str:
        url = f"{self._settings.openai_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self._settings.openai_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
        }

        async def request(client: httpx.AsyncClient) -> dict:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {self._settings.openai_api_key}"},
                json=payload,
                timeout=self._settings.jobclaw_request_timeout,
            )
            response.raise_for_status()
            return response.json()

        if self._client is None:
            async with httpx.AsyncClient() as client:
                data = await request(client)
        else:
            data = await request(self._client)
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("AI returned an empty response")
        return content.strip()

    def _job_context(self, job: Job) -> str:
        parts = [
            f"岗位：{job.title}", f"公司：{job.company}", f"地点：{job.location}",
            f"标签：{', '.join(job.tags)}", f"职位描述：{job.description}",
            f"链接：{job.url}",
        ]
        if self._settings.salary_filter_enabled and job.salary:
            parts.append(
                f"年薪范围：{job.salary.min_annual}-{job.salary.max_annual} "
                f"{job.salary.currency}"
            )
        return "\n".join(parts)

    async def match_job(self, resume_text: str, job: Job) -> Match:
        salary_instruction = (
            "考虑简历中的薪资期望与岗位薪资是否匹配。" if self._settings.salary_filter_enabled
            else "忽略简历和职位描述中的所有薪资信息，不因薪资调整分数。"
        )
        system = (
            "你是求职岗位匹配助手。将简历原文与岗位信息对照，按能力、经验、岗位方向、地点评分。"
            + salary_instruction
            + "只输出JSON对象，字段为score(0到1数字)、reasoning(字符串数组)、"
            "matched_skills(字符串数组)、missing_skills(字符串数组)。不要臆造简历经历。"
        )
        user = f"简历原文：\n{resume_text}\n\n岗位信息：\n{self._job_context(job)}"
        try:
            raw = await self._complete(system, user)
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()
            result = json.loads(cleaned)
            score = max(0.0, min(1.0, float(result["score"])))
            return Match(
                job_id=job.id, score=score, reasoning=result.get("reasoning", []),
                matched_skills=result.get("matched_skills", []),
                missing_skills=result.get("missing_skills", []),
            )
        except Exception as exc:
            logger.error("AI matching failed for %s: %s", job.url, exc)
            return Match(job_id=job.id, score=0.0, reasoning=[f"Matching failed: {exc}"])

    async def generate_greeting(self, resume_text: str, job: Job) -> str:
        system = (
            "你是求职者本人，正在给Boss直聘招聘方发第一条私信。"
            "按以下顺序自然地写成一段话：先表达对这个岗位的兴趣；"
            "再简要介绍自己与岗位相关的优势或技能；"
            "接着结合简历中的具体经历或项目，说明它与一到两个岗位要求的对应之处；"
            "最后表达希望进一步沟通，或希望有机会把简历发给对方参考。"
            "只使用提供的信息；岗位描述或简历信息不足时，宁可少写，也不要推测或硬凑匹配。"
            "语气平实、诚恳、自然，像日常工作沟通，不写宣传文案。"
            "避免‘非常荣幸’‘迫不及待’‘完美契合’等夸张表达，不用感叹号或表情符号。"
            "通常写成80到150个汉字，信息不足可以更短，不要重复表述优势和经历。"
            "直接输出一条消息正文，不要引号、标题、列表或Markdown。"
        )
        user = f"简历原文：\n{resume_text}\n\n岗位信息：\n{self._job_context(job)}"
        greeting = await self._complete(system, user)
        if len(greeting) > 500:
            raise ValueError("AI greeting exceeds 500 characters")
        return greeting
