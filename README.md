# ConsoleX plugins

Plugins that let coding agents do your [ConsoleX](https://consolex.ai) tasks and have each delivery checked against the task's saved acceptance standard.

| Plugin | Agent | Folder |
| --- | --- | --- |
| `consolex` | Claude Code | [`plugins/consolex`](plugins/consolex) |

## Claude Code

```bash
claude plugin marketplace add EvalsOne/consolex-plugins
claude plugin install consolex@consolex
```

Then run `/consolex:login` in Claude Code to connect your computer, and `/consolex:add <words from a task name>` to add a task. Requirements, every command, and exactly what the plugin runs, stores and sends are in the [plugin README](plugins/consolex/README.md).

## Repository layout

- `.claude-plugin/marketplace.json`: the Claude Code marketplace, named `consolex`.
- `plugins/consolex/`: the Claude Code plugin (hooks, commands, one Skill and a local MCP server; Python standard library only).

This repository is generated from the ConsoleX source on each release. Please report problems in [Issues](https://github.com/EvalsOne/consolex-plugins/issues).

## License

MIT. See [LICENSE](LICENSE).
