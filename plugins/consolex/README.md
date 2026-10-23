# ConsoleX for Claude Code

Do your [ConsoleX](https://consolex.ai) tasks in Claude Code and have every delivery checked against the task's saved acceptance standard before Claude finishes.

A ConsoleX task holds a method (how to do the work) and an acceptance standard (what a good result must meet). This plugin adds a task to Claude Code as a `/cx-…` Skill. When Claude hands over the result, ConsoleX checks it with an independent (non-Claude) judge and records the attempt in the task's run history. Claude never sees the acceptance standard, only the method, so the check stays independent. When a check fails or needs review, Claude sees just those items, tells you, and changes nothing until you say so.

## Requirements

- Claude Code with plugin support.
- `python3` (3.9 or later) on your `PATH`. The plugin uses only the Python standard library; nothing is installed.
- A ConsoleX account on a paid plan to connect a computer.

## Install

```bash
claude plugin marketplace add EvalsOne/consolex-plugins
claude plugin install consolex@consolex
```

Or inside Claude Code: `/plugin marketplace add EvalsOne/consolex-plugins`, then `/plugin install consolex@consolex`. Restart Claude Code (or run `/reload-plugins`) afterwards.

If adding the marketplace fails because you have no GitHub SSH key, set `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1` and try again.

Updates: `claude plugin marketplace update consolex`, then `claude plugin update consolex@consolex`, or turn on auto-update for the `consolex` marketplace under `/plugin` → Marketplaces.

## Use

| Command | What it does |
| --- | --- |
| `/consolex:login` | Connects this computer once. Your browser opens; approve the code in ConsoleX. |
| `/consolex:tasks [words]` | Lists your ConsoleX tasks and whether each is installed here. |
| `/consolex:add <words from a task name or a task link>` | Installs the task as a `/cx-…` Skill, with the ConsoleX Skills it uses. Run it again to update. |
| `/cx-<task name>` | Starts the task. You can also just ask Claude for it. |
| `/consolex:check [off]` | Checks the current task's latest delivery now; `off` turns checks off for this session. |
| `/consolex:save [task name]` | Sends this session to ConsoleX as a task draft that you review and save there. |
| `/consolex:run <task>` | Prints the terminal command for an isolated run of the task (a new Claude Code session with only the task's Skills, files and ConsoleX tools). |
| `/consolex:logout` | Disconnects this computer. You can also remove computers in ConsoleX under Settings › Coding agents. |

## What the plugin runs, stores and sends

Everything runs locally with your user permissions. The plugin talks only to the ConsoleX API (`https://api.evalsone.com`, or the address you pass to `/consolex:login`).

**Hooks.** The plugin registers hooks that run `python3 scripts/consolex.py hook`:

- `SessionStart`: writes the launcher `~/.consolex/bin/consolex` (used for isolated runs). If you have installed ConsoleX tasks, it also starts a background refresh of those task Skills, at most once an hour.
- `UserPromptSubmit`: handles the `/consolex:…` commands. When you start a `/cx-…` task it marks the session, fetches the task's latest approved method and tells ConsoleX a session started; while that session works it reports activity at most every two minutes. In any other session it exits at once without a network call.
- `PostToolUse` on `Skill`: the same as starting a task, when Claude loads a `cx-…` Skill itself.
- `PreToolUse` / `PostToolUse` on `AskUserQuestion`: in a task session only, tells ConsoleX that a question to you is open, then answered.
- `Stop`: in a task session only, submits a reply that has a "ConsoleX delivery" section for checking and shows the result. Other sessions exit at once without a network call.
- `SessionEnd`: in a task session only, tells ConsoleX the session ended.

**MCP server.** `scripts/consolex_mcp.py` (stdio, run with `python3`) gives Claude two tools, `consolex_task_tools` and `consolex_run_task_tool`, to call the tools of your ConsoleX tasks. ConsoleX runs each call with your connections, records it and bills it like a run in ConsoleX. This needs the "Tools and files" permission for the computer in ConsoleX.

**Stored on your computer.**

- `~/.consolex/credentials.json` (mode 0600): the device token from `/consolex:login`. Remove it with `/consolex:logout`.
- `~/.consolex/bin/consolex`: the isolated-run launcher.
- `~/.claude/skills/cx-…`: task Skills you add, and the ConsoleX Skills they use. Your own Skill of the same name is never replaced. Task Skills hold no secrets.
- Session state in Claude Code's plugin data folder, kept for 7 days.

**Sent to ConsoleX with a delivery.** The delivery (the reply, or the text files it names), the "Needs your confirmation" items, the model id, and the tool calls of this Claude Code session since the task started: commands, URLs, queries and file paths with their results. Long outputs are shortened, images are not sent, and keys and tokens are masked. Only the latest 80 tool calls are sent, so use one session per task. Set `CONSOLEX_TOOL_EVIDENCE=off` to send only the delivery.

**Sent with `/consolex:save`.** Your messages in the session, the final reply, the model id and the tool records (keys masked). Nothing is sent unless you run the command.

Sessions where you have not started a ConsoleX task send nothing.

## Settings

| Variable | Effect |
| --- | --- |
| `CONSOLEX_TOOL_EVIDENCE=off` | Send only the delivery, not the session's tool calls. |
| `CONSOLEX_NO_BROWSER=1` | `/consolex:login` prints the link instead of opening the browser. |
| `CONSOLEX_HOME` | Folder for the connection (default `~/.consolex`). |
| `CONSOLEX_SKILLS_DIR` | Where `/consolex:add` installs Skills (default `~/.claude/skills`). |
| `CONSOLEX_API_BASE`, `CONSOLEX_APP_BASE` | A self-hosted or development ConsoleX. |

## Limits

- Deliverables are text: the reply, or UTF-8 text files changed since the task started.
- Only this session's main tool calls count as evidence; checks that need more come back as "needs review".
- Cloud routines do not load user plugins.

## Privacy and support

- Privacy policy: https://consolex.ai/legal/privacy
- Terms: https://consolex.ai/legal/terms
- Issues: https://github.com/EvalsOne/consolex-plugins/issues

## License

MIT. See [LICENSE](LICENSE).
