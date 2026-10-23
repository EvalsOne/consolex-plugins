#!/usr/bin/env python3
"""Claude Code entry point for the shared ConsoleX client.

The plugin folder carries its own copy of the client (`consolex_core.py` beside this file), because
Claude Code installs a plugin by copying only this folder into its cache. tests/test_claude_code_plugin.py
keeps that copy identical to integrations/coding_agents/consolex_core.py, the source to edit; after
changing it, copy it here (or run integrations/coding_agents/build.py, which does the same for dist/).
"""
from pathlib import Path
_core = Path(__file__).with_name('consolex_core.py')
if not _core.is_file():
    _core = Path(__file__).resolve().parents[4] / 'coding_agents' / 'consolex_core.py'
exec(compile(_core.read_text(encoding='utf-8'), str(_core), 'exec'), globals())
