---
name: consolex-checks
description: How ConsoleX tasks work in Claude Code - connecting, adding tasks, saving a session as a task, and how deliveries are checked. Use when a ConsoleX task Skill (a cx-… Skill) is active, when ConsoleX reports a check result, or when the user asks how to use ConsoleX in Claude Code.
---

# ConsoleX in Claude Code

## Setting up (the user does this, once per computer)

- `/consolex:login` connects this computer: the browser opens, the user approves the code in ConsoleX, and the plugin saves the connection by itself. A development or self-hosted ConsoleX takes its API address: `/consolex:login https://…`.
- `/consolex:tasks` lists the user's ConsoleX tasks; `/consolex:add <words from a task name>` (or a ConsoleX task link) adds one as a `/cx-…` Skill, together with the ConsoleX Skills the task uses; the task Skill names them under "Skills for this task", so load those when a step needs them. `/consolex:logout` disconnects the computer.
- `/consolex:save [task name]` sends the current session (the user's messages, the final reply and the tool records, keys masked) to ConsoleX as a task draft; the user reviews and saves it there, and this result becomes the task's first run. Only the user runs it; never suggest it in place of finishing the work, and never send anything to ConsoleX yourself.
- These commands are handled by the plugin. Never edit `~/.consolex/credentials.json`, never ask the user for a token, and never create task Skills by hand.

## Working on a task

A ConsoleX task Skill (`cx-…`) carries one task's method and an output contract. When it loads, the plugin marks the session and adds the latest approved method to your context; follow that method.

You do not see the task's acceptance standard: it stays in ConsoleX so the check is independent of you. Do the work the method describes as well as you can; do not guess at or write to hidden checks. After a check you see only the items that did not pass, with their reasons.

When you hand over the result, end your reply with the two sections the output contract names, the delivery section last:

```
## Needs your confirmation      (or: ## 待你确认)
- Anything the owner must decide or verify; "None" if nothing
## ConsoleX delivery            (or: ## ConsoleX 交付)
- Deliverable: this reply        (or one "- File: relative/path" line per text file you wrote)
```

Write the confirmation section once in the whole reply. Many task methods already ask for a 「待你确认」 / "Needs your confirmation" section: then that is the one, so do not add a second copy.

Write each decision as a choice with its options ("Register cairninbox.com alone, or with .ai and .io too?"), so it can be put to the owner as a question.

Leave both sections out while you are asking a question or reporting progress: only a reply with the delivery section is checked. The judge also sees the tool calls and results of this session, so check facts with real tools (lookups, fetches, searches) rather than stating them; printing a result yourself (for example with echo) is not evidence. For file deliverables, list only text files you wrote or changed for this task, with paths relative to the current folder.

## After the check

ConsoleX checks the delivery against the task's saved standard with an independent (non-Claude) judge. When it passes and nothing needs confirmation, the plugin only shows the result.

When it passes (or could not be checked) and the delivery lists items under "Needs your confirmation", the turn continues once with a ConsoleX message that starts with the result. Then, without changing the deliverable or delivering again, give the owner the result in one line with the run link and ask about the items: with AskUserQuestion when you have it (one question per item that needs a decision, at most 4, each with 2-4 concrete options from the delivery; mention reminders in your message), otherwise as questions in text, and stop. Act on the owner's answers; if they change the deliverable, deliver again with the delivery section.

When a check fails or needs the owner's review, the turn continues once with a ConsoleX message listing the items and reasons. Then:

1. Do not change the deliverable and do not deliver again yet.
2. Tell the owner the result in one short message: each item with its reason, and the run link.
3. Ask whether to revise for these items: with AskUserQuestion when you have it (options "Revise these items" / "Keep as is"; items to confirm go into the same call as further questions), otherwise in one line, and stop.
4. Only after the owner says to revise: fix those items in the deliverable itself and deliver again with the delivery section. ConsoleX checks it again and the owner decides again.

Never invent facts, sources or approvals to pass a check, and do not add content only because a check named it; if a failed item asks for something the method never mentioned, say so to the owner. If an item needs the owner's decision or information you do not have, put it under "Needs your confirmation". A delivery made without the owner's go-ahead is not checked.

- `/consolex:check` checks the latest reply now; `/consolex:check off` stops checking this session.
- Every checked attempt appears in the task's run history in ConsoleX, labelled "External · Claude Code".
