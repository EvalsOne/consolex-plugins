#!/usr/bin/env python3
"""Local MCP server: ConsoleX task tools, executed by ConsoleX.

Two modes:
- An isolated run (`consolex.py run`) starts it for one task (`CONSOLEX_TASK_UUID`): it lists that task's tools
  under their own names and forwards every call.
- Otherwise (an ordinary session, registered by the plugin) it offers two tools: `consolex_task_tools` lists the
  tools of an installed task Skill and `consolex_run_task_tool` runs one. The task is named by its Skill (cx-…),
  which the plugin's note gives when the task starts.

ConsoleX runs each call with the owner's keys and connections, bills it like a hosted run and records it. Keys,
OAuth tokens and Skill secrets never reach this computer; the device token is read from the shared ConsoleX
connection (`~/.consolex/credentials.json`), and the computer needs the owner's "Tools and files" permission.

Transport: MCP over stdio (JSON-RPC 2.0, one message per line). Standard library only, like the client.
"""
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

_path = Path(__file__).with_name('consolex_core.py')
_spec = importlib.util.spec_from_file_location('consolex_core', _path)
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
LIST_TIMEOUT = 20
CALL_TIMEOUT = 330  # a slow tool (search, browser) may run for minutes on the server

TASK_ARGUMENT = {'type': 'string', 'description': 'The ConsoleX task Skill, like "cx-weekly-note" (the ConsoleX note '
                                                  'names it when the task starts).'}
GATEWAY_TOOLS = [
    {'name': core.TASK_TOOLS_TOOL,
     'description': "List the ConsoleX tools of a ConsoleX task: each tool's name, what it does and its input schema. "
                    'Use it before calling one with %s.' % core.RUN_TASK_TOOL,
     'inputSchema': {'type': 'object', 'properties': {'task': TASK_ARGUMENT}, 'required': ['task']}},
    {'name': core.RUN_TASK_TOOL,
     'description': "Run one of a ConsoleX task's tools. ConsoleX runs it with the owner's connections, bills it like a "
                    'run in ConsoleX and records it. Arguments follow the input schema %s gives.' % core.TASK_TOOLS_TOOL,
     'inputSchema': {'type': 'object', 'properties': {
         'task': TASK_ARGUMENT,
         'tool': {'type': 'string', 'description': 'The tool name from %s.' % core.TASK_TOOLS_TOOL},
         'arguments': {'type': 'object', 'description': "The tool's arguments, as its input schema describes."}},
         'required': ['task', 'tool']}},
]


class ToolServer:
    def __init__(self, env, http=core.http_json):
        self.env, self.http = env, http
        self.task_uuid = env.get('CONSOLEX_TASK_UUID') or ''
        self.api = core.default_api_base(env)
        self.token = core.connection_for(env, self.api)[0]
        # Calls are recorded against the agent session, so the check sees them as evidence the server ran.
        self.session_id = env.get('CONSOLEX_SESSION_ID') or env.get('CLAUDE_CODE_SESSION_ID') or 'mcp-' + uuid.uuid4().hex
        self.tools = None
        self.problem = ''

    @property
    def gateway(self):
        return not self.task_uuid

    def _url(self, suffix):
        return '%s/api/operator/external/tasks/%s/%s' % (self.api, self.task_uuid, suffix)

    def list_tools(self):
        if self.gateway:
            # An isolated run has its task's own tools from a task-bound server; the session-wide pair stays out.
            return [] if self.env.get('CONSOLEX_ISOLATED') == '1' else GATEWAY_TOOLS
        if self.tools is not None:
            return self.tools
        self.tools = []
        if not self.token:
            self.problem = 'ConsoleX is not connected on this computer, or no task was given.'
            return self.tools
        status, body = self.http('GET', self._url('tools'), self.token, timeout=LIST_TIMEOUT)
        if status != 200 or not body.get('succ'):
            self.problem = body.get('error_msg') or ('HTTP %s' % status if status else 'ConsoleX unreachable')
            return self.tools
        self.tools = [{'name': item['name'], 'description': item.get('description') or '',
                       'inputSchema': item.get('input_schema') or {'type': 'object', 'properties': {}}}
                      for item in body.get('tools') or [] if isinstance(item, dict) and item.get('name')]
        return self.tools

    def call(self, name, arguments):
        arguments = arguments if isinstance(arguments, dict) else {}
        if self.gateway:
            return self._gateway_call(name, arguments)
        if name not in {tool['name'] for tool in self.list_tools()}:
            return 'Unknown tool %s for this ConsoleX task.%s' % (name, (' ' + self.problem) if self.problem else ''), True
        status, body = self.http('POST', self._url('tools/call'), self.token, {
            'session_id': self.session_id, 'call_id': uuid.uuid4().hex, 'tool': name,
            'arguments': arguments}, timeout=CALL_TIMEOUT)
        if status != 200 or not body.get('succ'):
            return 'ConsoleX could not run %s: %s' % (name, body.get('error_msg') or ('HTTP %s' % status if status else 'unreachable')), True
        return str(body.get('output') or ''), bool(body.get('is_error'))

    def _gateway_call(self, name, arguments):
        if name not in {tool['name'] for tool in self.list_tools()}:
            return 'Unknown tool %s.' % name, True
        if name == core.TASK_TOOLS_TOOL:
            tools, problem = core.task_tools(self.env, arguments.get('task'), http=self.http)
            if problem:
                return core.command_text(problem, self.env), True
            if not tools:
                return 'This ConsoleX task has no tools that can run now.', False
            return json.dumps({'task': arguments.get('task'), 'tools': tools}, ensure_ascii=False, indent=1), False
        text, is_error = core.run_task_tool(self.env, arguments.get('task'), arguments.get('tool'), arguments.get('arguments'),
                                            self.session_id, http=self.http, timeout=CALL_TIMEOUT)
        return core.command_text(text, self.env) if is_error else text, is_error

    def handle(self, message):
        """The JSON-RPC response to one message, or None for a notification."""
        if not isinstance(message, dict):
            return _error(None, -32600, 'Invalid request')
        method, ident, params = message.get('method'), message.get('id'), message.get('params') or {}
        if ident is None:
            return None  # notifications/initialized, notifications/cancelled, …
        if method == 'initialize':
            requested = params.get('protocolVersion')
            return _result(ident, {
                'protocolVersion': requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': 'consolex', 'version': '1.1.0'},
                'instructions': ('Tools of your ConsoleX tasks. ConsoleX runs each call with the owner\'s connections, '
                                 'records it and bills it like a run in ConsoleX.')})
        if method == 'ping':
            return _result(ident, {})
        if method == 'tools/list':
            return _result(ident, {'tools': self.list_tools()})
        if method == 'tools/call':
            text, is_error = self.call(str(params.get('name') or ''), params.get('arguments'))
            return _result(ident, {'content': [{'type': 'text', 'text': text}], 'isError': is_error})
        return _error(ident, -32601, 'Method not found: %s' % method)


def _result(ident, result):
    return {'jsonrpc': '2.0', 'id': ident, 'result': result}


def _error(ident, code, message):
    return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': code, 'message': message}}


def serve(stdin, stdout, server):
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = _error(None, -32700, 'Parse error')
        else:
            try:
                response = server.handle(message)
            except Exception as exc:  # A broken call answers an error; the server keeps running.
                response = _error(message.get('id') if isinstance(message, dict) else None, -32603, type(exc).__name__)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + '\n')
            stdout.flush()


if __name__ == '__main__':
    serve(sys.stdin, sys.stdout, ToolServer(dict(os.environ)))
