# JobClaw

[中文](README.md)

JobClaw uses DrissionPage to search Boss Zhipin in Chrome and processes job cards one at a time. An OpenAI-compatible Chat Completions API writes each greeting from the resume and job information. `run` does not score jobs. The default AI settings target Qwen on DashScope.

## Setup

Requires Python 3.11+ and a locally installed Chrome or other Chromium browser.

```powershell
uv sync
Copy-Item .env.example .env
```

Set `OPENAI_API_KEY`, comma-separated `SEARCH_QUERIES`, and `RESUME_PATH` in `.env`. Resumes may be UTF-8 `.md`, `.markdown`, or `.txt` files without a fixed schema. Their full text and the job content are sent to the configured AI endpoint. `RESUME_PATH` can be `C:/resumes/me.md`. Set `OPENAI_BASE_URL` and `OPENAI_MODEL` for a different OpenAI-compatible service. [DashScope base URLs](https://help.aliyun.com/zh/model-studio/base-url)

## Login and browser profile

After migrating to DrissionPage, log in manually once:

```powershell
uv run jobclaw login
uv run jobclaw login --check
```

Login, search, and chat share a persistent Chrome profile in this project's `.jobclaw/browser/boss` directory by default, using debugging port `9334`. Application history is stored in `.jobclaw/apply_history.json`, and the project config places uv's download cache in `.jobclaw/uv-cache`. An existing `UV_CACHE_DIR` environment variable overrides that uv setting. `.jobclaw/` is ignored by Git. `BOSS_BROWSER_USER_DATA_DIR`, `BOSS_BROWSER_PORT`, and `BOSS_BROWSER_PATH` can change the profile directory, debugging port, and browser executable. Do not run two JobClaw processes with the same profile and port at once. The previous home-directory profile is not migrated automatically; log in again after switching defaults. Keep `JOBCLAW_HEADLESS=false` for visible troubleshooting. Captchas stop the affected run and trigger configured notifications.

## Search and run

```powershell
uv run jobclaw validate-resume
uv run jobclaw scrape --limit 20
uv run jobclaw run
```

`scrape` reads cards already loaded on the search page. `run` divides the day's remaining `BOSS_DAILY_LIMIT` across search terms and keeps each search tab open. For each card it deduplicates the URL, checks application history, applies any configured salary floor, generates a greeting, opens the job detail in a new tab, and contacts the recruiter. Once the loaded cards are processed, it scrolls the same search page and waits for new cards until that term's quota is met or repeated scrolls yield no new cards. Failed greeting generation skips the card. Unused shares from exhausted terms are offered to other terms in round-robin order. There is no ranking step or fixed top-five limit.

Start Chat opens an in-page dialog using `textarea.input-area` and `.send-message`. Continue Chat navigates to the full conversation using `#chat-input` and `button.btn-send`. A greeting is marked submitted only when it appears in the dialog message list or matching conversation. This is a UI check, not a server delivery receipt. The CLI prints term quotas, card progress, greeting generation, selected send path, confirmation, and failure reasons without printing resume or greeting text. AI requests and browser actions run sequentially by card.

The daily cap uses China time and conservatively counts a contact-button attempt even if confirmation later fails. An uncertain send stops the run. Both browser state and application history are kept in the project directory.

Set `BOSS_JOB_TYPE` to `all`, `1901` (full-time), `1902` (internship), or `1903` (part-time); the corresponding names also work. `--job-type` overrides it for one command. `BOSS_CITY` defaults to `100010000`. Each comma-separated search term is searched separately and results are deduplicated by URL. `scrape --limit` caps cards read from the currently loaded results. By default, `run` keeps scrolling until its quota is met or no new cards appear; an explicit `run --limit 20` caps processing at 20 cards per term. This flag does not change the daily contact quota. `--resume` overrides `RESUME_PATH` for one command.

Navigation retries are disabled and the scraper waits for real job links inside current `li.job-card-box` cards, retaining `.job-card-wrapper` as a fallback. If cards do not appear, it reports the current URL and counts of cards and job links. If the page still redirects repeatedly, check the persistent login with `login --check` and inspect the visible browser. Changing frameworks cannot guarantee that a site's own login or verification redirects will stop.

## Salary and notifications

`SALARY_FILTER_ENABLED=false` disables salary filtering and omits a separate structured salary field from the AI prompt; raw card descriptions may still mention salary. Set it to `true` to include structured salary context, and optionally set `MIN_ANNUAL_SALARY` for a CNY annual floor. Configure any combination of Telegram, Discord, and Feishu webhooks. Set `FEISHU_WEBHOOK_SECRET` when the Feishu bot uses signature validation.

DrissionPage's [license](https://github.com/g1879/DrissionPage/blob/master/LICENSE) restricts commercial use.
