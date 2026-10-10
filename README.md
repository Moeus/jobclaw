# JobClaw

[English](README_EN.md)

JobClaw 用 DrissionPage 控制 Chrome 搜索 Boss 直聘岗位，逐张读取岗位卡片，并通过 OpenAI 兼容接口结合简历原文生成打招呼消息。默认模型配置面向千问（Qwen）百炼兼容接口。`run` 不进行岗位评分。

## 安装

需要 Python 3.11+ 和本机安装的 Chrome 或其他 Chromium 浏览器。在项目目录运行：

```powershell
uv sync
Copy-Item .env.example .env
```

在 `.env` 中填写 `OPENAI_API_KEY`、`SEARCH_QUERIES` 和 `RESUME_PATH`。搜索词用英文逗号分隔，例如 `SEARCH_QUERIES=LLM实习生,Python实习生`。简历为 UTF-8 编码的 `.md`、`.markdown` 或 `.txt` 文件，不需要固定字段。简历原文与岗位内容会发送到配置的模型接口。`RESUME_PATH` 可以写成 `C:/resumes/me.md`。

可设置 `OPENAI_BASE_URL`、`OPENAI_MODEL` 适配其他 OpenAI 兼容服务。示例使用百炼北京地域的兼容接口；地域、API Key 和 Base URL 应对应。[百炼 Base URL 文档](https://help.aliyun.com/zh/model-studio/base-url)

## 登录与浏览器会话

首次迁移到 DrissionPage 后，运行一次手动登录：

```powershell
uv run jobclaw login
uv run jobclaw login --check
```

Boss 登录、搜索和沟通共用一个**独立持久的 Chrome 用户目录**，默认位置为项目内的 `.jobclaw/browser/boss`，默认调试端口为 `9334`。投递记录保存在项目内的 `.jobclaw/apply_history.json`，uv 下载缓存配置在 `.jobclaw/uv-cache`；整个 `.jobclaw/` 已加入 `.gitignore`。若系统设置了 `UV_CACHE_DIR`，它会覆盖项目内的 uv 缓存配置。可通过 `BOSS_BROWSER_USER_DATA_DIR` 改浏览器资料位置，通过 `BOSS_BROWSER_PORT` 改调试端口；若找不到浏览器，可设置 `BOSS_BROWSER_PATH`。不要同时启动两个使用同一用户目录和端口的 JobClaw 进程。默认目录已从用户主目录改到项目目录，旧浏览器登录资料不会自动迁移，请重新执行 `login`。

`JOBCLAW_HEADLESS=false` 便于观察页面及手动完成登录。验证码出现时程序会停止该次投递并通知，不会自动破解或绕过。

## 搜索与运行

```powershell
uv run jobclaw validate-resume
uv run jobclaw scrape --limit 20
uv run jobclaw run
```

`scrape` 只读取搜索页当前加载的卡片。`run` 为每个搜索词按 `BOSS_DAILY_LIMIT` 平均分配当日剩余额度，保留搜索标签页并逐张处理卡片：先按 URL 去重并检查投递历史，再做可选薪资过滤、生成 AI 招呼语，用新标签页打开岗位详情并沟通。当前卡片处理完后，程序在原搜索页下滑、等待新卡片，并继续处理，直到该搜索词额度用完或连续下滑仍无新卡片。生成失败时跳过该卡片，不点击沟通。某个搜索词没有更多卡片时，未用额度会轮流分给其他搜索词。没有岗位评分或固定前五名限制。

“立即沟通”会打开详情页内的打招呼弹窗，程序使用弹窗的 `textarea.input-area` 和 `.send-message`；“继续沟通”会跳转聊天页，程序使用 `#chat-input` 和 `button.btn-send`。只有消息在弹窗消息列表或对应聊天记录中出现才记为已发送。此检查基于页面显示，不是服务端送达回执。终端会打印搜索词配额、逐卡处理、AI 生成、发送路径、确认状态和失败原因，不打印简历或招呼语正文。

`BOSS_DAILY_LIMIT` 按中国时间统计当日已记录的沟通尝试。点击沟通按钮前先记录 `attempted`，发送确认后更新为 `submitted`；结果不确定时停止本轮，以免超额或重复发送。浏览器资料与投递记录都只保存在本项目目录。AI 请求与浏览器操作逐张顺序执行。

`.env` 中的 `BOSS_JOB_TYPE` 可设为 `all`（不限）、`1901`（全职）、`1902`（实习）、`1903`（兼职）；也支持 `full-time`、`internship`、`part-time`。命令行 `--job-type` 可临时覆盖。`BOSS_CITY` 为 Boss 城市代码，默认 `100010000`。每个搜索词分别搜索，按岗位 URL 去重。`scrape --limit` 是当前搜索页的读取上限；`run` 默认持续下滑，直到额度用完或没有新卡片，若显式传入 `run --limit 20` 则每个搜索词最多处理 20 张卡片。`--limit` **不是每日投递额度**。`--resume` 可临时覆盖 `RESUME_PATH`。

导航不会自动重试，程序等待真实岗位链接出现在当前的 `li.job-card-box` 卡片中，同时兼容旧的 `.job-card-wrapper`。若没有出现，会停止并报告页面 URL、卡片及岗位链接数量。若页面仍在自动跳转，先用 `login --check` 核对持久会话，再在可见浏览器里检查页面状态。框架切换不能保证解决站点自身的登录或验证跳转。

## 薪资与通知

默认 `SALARY_FILTER_ENABLED=false`：不按薪资过滤，也不向模型单独传结构化薪资；卡片描述原文仍可能包含薪资。设为 `true` 后，招呼语可以参考薪资；再设置 `MIN_ANNUAL_SALARY` 可启用人民币年薪硬性下限。缺少可比较薪资的岗位会被跳过。

可配置 Telegram、Discord、飞书任意组合。飞书群自定义机器人填 `FEISHU_WEBHOOK_URL`，若开启签名校验，再填 `FEISHU_WEBHOOK_SECRET`。运行结束发送摘要，Boss 验证码会立即通知。

## 主要文件

- `jobclaw/browser.py`：持久 Chrome 会话及选择器适配
- `jobclaw/auth/browser_login.py`：手动登录和会话检查
- `jobclaw/scraper/boss.py`：搜索及岗位卡片提取
- `jobclaw/applier/boss.py`：沟通流程及原有按钮、输入框选择器
- `jobclaw/ai.py`：招呼语生成；保留独立匹配接口，但 `run` 不调用
- `jobclaw/cli.py`：命令与执行流程

DrissionPage 的[许可证](https://github.com/g1879/DrissionPage/blob/master/LICENSE)对商业使用有限制。
