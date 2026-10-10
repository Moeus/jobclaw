"""Command-line workflows for sequential Boss search and greetings."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from pathlib import Path

import click
import httpx

from jobclaw.ai import JobAI
from jobclaw.applier.boss import BossApplier
from jobclaw.applier.history import ApplyHistory
from jobclaw.auth.browser_login import cookies_valid, interactive_login
from jobclaw.browser import BossBrowser
from jobclaw.config import STATE_DIR, Settings, get_settings
from jobclaw.models import Application, ApplicationStatus, Job
from jobclaw.notifier import BroadcastNotifier
from jobclaw.resume import load_resume
from jobclaw.scraper.boss import BossScraper, BossSearchSession, BossSearchUnavailable

JOB_TYPE_ALIASES = {
    "all": "all",
    "1901": "full-time", "full-time": "full-time",
    "1902": "internship", "internship": "internship",
    "1903": "part-time", "part-time": "part-time",
}
JOB_TYPES = click.Choice(list(JOB_TYPE_ALIASES))


@click.group(help="JobClaw: search Boss jobs and write AI greetings.")
def main() -> None:
    pass


@main.command("validate-resume")
@click.option("--resume", type=click.Path(path_type=Path, dir_okay=False), default=None)
def validate_resume_command(resume: Path | None) -> None:
    path = _resume_path(resume, get_settings())
    text = load_resume(path)
    click.echo(f"Resume OK: {path} ({len(text)} characters)")


@main.command("scrape")
@click.option("--job-type", type=JOB_TYPES, default=None, help="Override BOSS_JOB_TYPE for this search.")
@click.option("--location", default=None, help="Fallback display location for Boss cards.")
@click.option("--limit", default=20, show_default=True, type=click.IntRange(min=1),
              help="Maximum cards to read per search term from the first page.")
def scrape_command(job_type: str | None, location: str | None, limit: int) -> None:
    """Search each keyword in SEARCH_QUERIES without applying."""
    _scrape(job_type, location, limit)


@main.command("run")
@click.option("--job-type", type=JOB_TYPES, default=None, help="Override BOSS_JOB_TYPE for this run.")
@click.option("--location", default=None, help="Fallback display location for Boss cards.")
@click.option("--resume", type=click.Path(path_type=Path, dir_okay=False), default=None)
@click.option("--limit", default=None, type=click.IntRange(min=1),
              help="Optional maximum cards to process per search term; by default, scroll until quota or no new cards.")
def run_command(
    job_type: str | None, location: str | None, resume: Path | None, limit: int | None,
) -> None:
    """Search -> handle each card -> AI greeting -> contact -> notify."""
    asyncio.run(_run_pipeline(job_type, location, resume, limit))


@main.command("login")
@click.option("--timeout", type=click.IntRange(min=1), default=5, show_default=True)
@click.option("--check", is_flag=True, help="Check the saved Boss browser login.")
def login_command(timeout: int, check: bool) -> None:
    _login(timeout, check)


def _login(timeout: int, check_only: bool) -> None:
    settings = get_settings()
    click.echo(f"JobClaw local state: {STATE_DIR}")
    if check_only:
        click.echo(f"Boss login valid={cookies_valid(settings)}")
        return
    click.echo("Log in to Boss manually in the opened browser")
    try:
        interactive_login(settings, timeout_minutes=timeout)
    except Exception as exc:
        raise click.ClickException(f"Boss login failed: {exc}") from exc
    click.echo("Boss login saved in the dedicated browser profile")


def _search_settings(job_type_override: str | None) -> tuple[Settings, str]:
    settings = get_settings()
    if not settings.queries:
        raise click.ClickException("Set SEARCH_QUERIES in .env (comma-separated keywords)")
    configured = job_type_override if job_type_override is not None else settings.boss_job_type
    job_type = JOB_TYPE_ALIASES.get(configured.strip().lower())
    if job_type is None:
        raise click.ClickException(
            "BOSS_JOB_TYPE must be all, 1901, 1902, 1903, full-time, internship, or part-time"
        )
    return settings, job_type


def _resume_path(override: Path | None, settings: Settings) -> Path:
    configured = override or settings.resume_path
    if not configured:
        raise click.ClickException("Set RESUME_PATH in .env or pass --resume")
    path = Path(configured).expanduser()
    if not path.is_file():
        raise click.ClickException(f"Resume file not found: {path}")
    return path


def _collect_jobs(
    settings: Settings, job_type: str, location: str | None, limit: int,
) -> list[Job]:
    return [
        job for _, jobs in _collect_jobs_by_query(settings, job_type, location, limit)
        for job in jobs
    ]


def _collect_jobs_by_query(
    settings: Settings, job_type: str, location: str | None, limit: int,
) -> list[tuple[str, list[Job]]]:
    """Keep card order and query ownership while deduplicating job URLs."""
    grouped: list[tuple[str, list[Job]]] = []
    seen_urls: set[str] = set()
    try:
        with BossScraper(settings) as scraper:
            for index, query in enumerate(settings.queries, start=1):
                jobs = scraper.scrape_jobs(
                    query=query, location=location, limit=limit, job_type=job_type,
                )
                unique_jobs: list[Job] = []
                for job in jobs:
                    url = str(job.url)
                    if url not in seen_urls:
                        seen_urls.add(url)
                        unique_jobs.append(job)
                grouped.append((query, unique_jobs))
                click.echo(
                    f"Boss search {index}/{len(settings.queries)} '{query}': "
                    f"cards={len(jobs)}, new={len(unique_jobs)}"
                )
    except BossSearchUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(f"Boss browser failed: {exc}") from exc
    return grouped


def _salary_filter(jobs: list[Job], settings: Settings) -> list[Job]:
    if not settings.salary_filter_enabled or settings.min_annual_salary is None:
        return jobs
    # A hard CNY threshold cannot be safely compared with an unknown or foreign currency.
    return [
        job for job in jobs
        if job.salary is not None
        and job.salary.currency == "CNY"
        and job.salary.max_annual is not None
        and job.salary.max_annual >= settings.min_annual_salary
    ]


def _scrape(job_type: str | None, location: str | None, limit: int) -> None:
    settings, job_type = _search_settings(job_type)
    jobs = _collect_jobs(settings, job_type, location, limit)
    jobs = _salary_filter(jobs, settings)
    click.echo(f"Total unique jobs: {len(jobs)}")


async def _run_pipeline(
    job_type: str | None, location: str | None, resume_path: Path | None,
    limit: int | None,
) -> None:
    settings, job_type = _search_settings(job_type)
    resume_text = load_resume(_resume_path(resume_path, settings))
    notifier = BroadcastNotifier(settings)
    history = ApplyHistory()
    click.echo(f"JobClaw local state: {STATE_DIR}")
    recorded_before = history.today_count()
    remaining = max(0, settings.boss_daily_limit - recorded_before)
    click.echo(
        f"Boss daily quota: limit={settings.boss_daily_limit}, "
        f"recorded_today={recorded_before}, remaining={remaining}"
    )
    if remaining == 0:
        click.echo("Daily quota reached; no search or chat started")
        return

    query_count = len(settings.queries)
    seen_urls: set[str] = set()
    daily_targets = [
        settings.boss_daily_limit // query_count
        + (index < settings.boss_daily_limit % query_count)
        for index in range(query_count)
    ]
    previous_by_query = [history.today_count(query) for query in settings.queries]
    quotas = [
        max(0, target - previous)
        for target, previous in zip(daily_targets, previous_by_query)
    ]
    used = [0] * query_count
    positions = [0] * query_count
    sessions: list[BossSearchSession | None] = [None] * query_count
    applications: list[Application] = []
    confirmed = 0
    halt_reason: str | None = None
    for index, query in enumerate(settings.queries):
        click.echo(
            f"Boss quota '{query}': daily_target={daily_targets[index]}, "
            f"already_today={previous_by_query[index]}, available={quotas[index]}"
        )

    async with httpx.AsyncClient() as client:
        ai = JobAI(settings, client)
        with BossBrowser(settings) as browser, BossScraper(settings, browser=browser) as scraper, ExitStack() as searches:
            async with BossApplier(
                settings, ai, notifier=notifier, history=history, browser=browser,
                progress=lambda message: click.echo(f"  {message}"),
            ) as applier:

                def open_search(query_index: int) -> BossSearchSession:
                    session = sessions[query_index]
                    if session is None:
                        query = settings.queries[query_index]
                        click.echo(f"Boss search {query_index + 1}/{query_count} '{query}'")
                        session = searches.enter_context(scraper.open_search(
                            query=query, location=location, job_type=job_type,
                            progress=lambda message: click.echo(f"  {message}"),
                        ))
                        sessions[query_index] = session
                        click.echo(
                            f"  Cards initially loaded={session.cards_read}, "
                            f"quota={quotas[query_index]}"
                        )
                    return session

                async def process_next(query_index: int) -> bool:
                    """Consume live cards until one chat is attempted or results end."""
                    nonlocal confirmed, halt_reason
                    query = settings.queries[query_index]
                    try:
                        session = open_search(query_index)
                    except BossSearchUnavailable as exc:
                        click.echo(f"  Search unavailable: {exc}")
                        halt_reason = "search_unavailable"
                        return False
                    while True:
                        try:
                            job = session.next_job(max_cards=limit)
                        except Exception as exc:
                            click.echo(f"  Search failed while loading more cards: {exc}")
                            halt_reason = "search_failed"
                            return False
                        if job is None:
                            return False
                        positions[query_index] += 1
                        click.echo(
                            f"[{query}] card {positions[query_index]}: "
                            f"{job.title} @ {job.company}"
                        )
                        click.echo(f"  Job URL: {job.url}")
                        url = str(job.url)
                        if url in seen_urls:
                            click.echo("  Skip: duplicate job URL from another search term")
                            continue
                        seen_urls.add(url)
                        if history.is_applied(job.id):
                            click.echo("  Skip: already recorded in application history")
                            continue
                        if not _salary_filter([job], settings):
                            click.echo("  Skip: below salary threshold or salary unavailable")
                            continue
                        click.echo("  Generating AI greeting")
                        try:
                            greeting = await ai.generate_greeting(resume_text, job)
                        except Exception as exc:
                            detail = str(exc).splitlines()
                            click.echo(
                                f"  Greeting failed: "
                                f"{detail[0] if detail else type(exc).__name__}"
                            )
                            continue
                        click.echo(f"  Greeting ready ({len(greeting)} characters); opening job detail")
                        result = await applier.apply(
                            job, resume_text, greeting=greeting, query=query,
                        )
                        applications.append(result)
                        if history.is_applied(job.id):
                            used[query_index] += 1
                        if result.status == ApplicationStatus.SUBMITTED:
                            confirmed += 1
                        reason = result.extra.get("reason")
                        detail = f", reason={reason}" if reason else ""
                        click.echo(
                            f"  Result: {result.status.value}{detail}; "
                            f"query_used={used[query_index]}, "
                            f"daily_used={history.today_count()}/{settings.boss_daily_limit}"
                        )
                        if result.status == ApplicationStatus.CAPTCHA_BLOCKED or (
                            result.status == ApplicationStatus.FAILED
                            and history.is_applied(job.id)
                        ) or reason == "daily_limit":
                            halt_reason = reason or result.status.value
                            click.echo(f"Stopping run: {halt_reason}")
                        return True
                # Keep each query's search tab alive while consuming and scrolling.
                for index, query in enumerate(settings.queries):
                    if halt_reason or history.today_count() >= settings.boss_daily_limit:
                        break
                    while (
                        used[index] < quotas[index]
                        and history.today_count() < settings.boss_daily_limit
                        and halt_reason is None
                    ):
                        if not await process_next(index):
                            break
                    click.echo(
                        f"Boss quota '{query}': used={used[index]}/{quotas[index]}"
                    )

                # Redistribute unused shares across the same live search tabs.
                if history.today_count() < settings.boss_daily_limit and halt_reason is None:
                    click.echo("Redistributing unused query quota across search results")
                while history.today_count() < settings.boss_daily_limit and halt_reason is None:
                    progressed = False
                    for index in range(query_count):
                        if history.today_count() >= settings.boss_daily_limit or halt_reason:
                            break
                        if await process_next(index):
                            progressed = True
                    if not progressed:
                        break

    total_jobs = len(seen_urls)
    recorded_after = history.today_count()
    if recorded_after < settings.boss_daily_limit and halt_reason is None:
        click.echo("Daily quota not filled: search pages yielded no more new cards")
    for index, query in enumerate(settings.queries):
        click.echo(
            f"Boss query final '{query}': this_run={used[index]}, "
            f"today={history.today_count(query)}/{daily_targets[index]}"
        )
    stopped = f", stopped={halt_reason}" if halt_reason else ""
    click.echo(
        f"JobClaw completed: jobs={total_jobs}, "
        f"applications_attempted={len(applications)}, confirmed={confirmed}, "
        f"daily_used={recorded_after}/{settings.boss_daily_limit}{stopped}"
    )
    if notifier.configured:
        await notifier.send_text(
            f"JobClaw completed: jobs={total_jobs}, "
            f"applications_attempted={len(applications)}, confirmed={confirmed}, "
            f"daily_used={recorded_after}/{settings.boss_daily_limit}{stopped}"
        )


if __name__ == "__main__":
    main()
