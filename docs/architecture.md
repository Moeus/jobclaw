# Architecture

```text
SEARCH_QUERIES -> Boss search -> ordered cards -> optional salary floor
                                               |
UTF-8 Markdown/text resume --------------------+-> JobAI.generate_greeting
                                                         |
                                  new detail tab -> dialog/chat -> history -> notifications
```

`BossBrowser` owns a dedicated persistent Chrome profile under this project's `.jobclaw/browser/boss`. History is under `.jobclaw/apply_history.json`. The manual login command, scraper, and applier use the same profile. During `run`, scraper and applier share one browser session. Search opens one tab per query without a separate incognito context and retains it throughout the run. Once its loaded cards are consumed, the search session scrolls the same tab and waits for unseen job URLs; it stops after repeated scrolls yield no new cards. Detail and chat tabs are opened separately. Browser navigation uses DrissionPage `none` load mode and `retry=0`, then waits for real `/job_detail/*.html` links in the current `li.job-card-box` cards. The older `.job-card-wrapper` remains a fallback. Job type and city come from `BOSS_JOB_TYPE` and `BOSS_CITY`.

`jobclaw/browser.py` translates the retained CSS selectors to DrissionPage's `css:` locator syntax. Playwright-only `:has-text()` selectors are matched by their CSS base and descendant text. The scraper retains its card and field selectors. The applier branches by the visible detail-page button: Start Chat uses the in-page `.startchat-dialog` textarea and send control; Continue Chat uses the full chat page's contenteditable composer and send button. Captcha handling stops and notifies rather than solving the challenge.

`JobAI` sends Chat Completions requests to an OpenAI-compatible endpoint. The `run` flow only calls `generate_greeting` with unstructured resume text and job content; `match_job` remains an independent API but is not called. Salary is included only when `SALARY_FILTER_ENABLED=true`. AI generation and Boss browser actions run sequentially per card. Each new card is deduplicated and checked against application history before AI generation or opening the detail tab. Each query receives a daily share, with unused shares redistributed to other queries when cards run out. History counts button attempts in China time before clicking and upgrades them to submitted after a visible message confirmation. Telegram, Discord, and Feishu notifications are independent channels.
