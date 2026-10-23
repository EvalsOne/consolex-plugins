#!/usr/bin/env python3
"""Shared ConsoleX client: device sign-in, task Skills, deliveries, evidence and checks.

Claude Code runs `consolex.py hook` for UserPromptSubmit, PostToolUse (Skill) and Stop with the
hook's JSON on stdin. Only a session that loaded a ConsoleX task Skill (a skill folder with a
`consolex.json`) is ever checked; every other session exits at once without a network call.

Connect once per computer: `/consolex:login` starts a device-code sign-in, opens the browser,
and a background poller saves the device token to `~/.consolex/credentials.json` (0600) once the
owner approves. That file is shared by every coding-agent integration on the computer.

Commands (handled here, from the prompt): `/consolex:login [api]`, `/consolex:logout`,
`/consolex:tasks [words]`, `/consolex:add [words | ConsoleX link]`, `/consolex:check [off]`,
`/consolex:save [task name]` (this session becomes a task draft the owner reviews in ConsoleX).

Flow for a task (docs/claude-code-checks.md in the ConsoleX service repo):
- Its Skill loads -> mark this session, fetch the Task's latest approved method, add it to context.
- Claude ends a turn with a "ConsoleX delivery" section -> submit the deliverable; ConsoleX checks
  it against the Task's saved standard and records the attempt. Not passed -> Claude reports the
  result and asks the owner; it revises only after the owner says so (never automatically).
- The acceptance standard never reaches this computer: Claude sees the method, and after a check
  only the items that did not pass. An agent that sees how it is judged writes to the checks.

Standard library only (macOS ships Python 3.9). Tasks are named by UUID and name, never by id.
"""
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

DEFAULT_API_BASE = 'https://api.evalsone.com'
CLIENT = 'claude_code'


def client(env):
    return env.get('CONSOLEX_RUNTIME', CLIENT)

TEXT_SUFFIXES = {'.md', '.markdown', '.txt', '.json', '.csv', '.tsv', '.html', '.htm', '.xml', '.yaml', '.yml',
                 '.rst', '.org', '.tex'}
MAX_FILE_BYTES = 200_000
MAX_TOTAL_CHARS = 400_000
MARKER_TTL_SECONDS = 7 * 24 * 3600
SUBMIT_TIMEOUT = 170
QUICK_TIMEOUT = 8
TRANSCRIPT_TAIL_BYTES = 4_000_000
HEARTBEAT_SECONDS = 120  # at most one "still working" signal per this many seconds
SESSION_TIMEOUT = 4

DELIVERY_HEADING = re.compile(r'^\s{0,3}#{1,4}\s*ConsoleX\s*(?:交付|delivery)\s*$', re.IGNORECASE | re.MULTILINE)
CONFIRM_HEADING = re.compile(r'^\s{0,3}#{1,4}\s*(?:待你确认|待您确认|Needs your confirmation)\s*$', re.IGNORECASE | re.MULTILINE)
ANY_HEADING = re.compile(r'^\s{0,3}#{1,6}\s', re.MULTILINE)
FILE_LINE = re.compile(r'^\s*[-*]\s*(?:文件|File)\s*[:：]\s*`?([^`\n]+?)`?\s*$', re.IGNORECASE | re.MULTILINE)
SKILL_NAME = re.compile(r'cx-[a-z0-9-]{1,70}')
NONE_ITEMS = {'无', '没有', 'none', 'n/a', 'nothing', '-'}


# ---------------------------------------------------------------- files and state

def consolex_home(env):
    return env.get('CONSOLEX_HOME') or os.path.join(os.path.expanduser('~'), '.consolex')


def skills_dir(env):
    return env.get('CONSOLEX_SKILLS_DIR') or os.path.join(os.path.expanduser('~'),
        '.claude' if client(env) == 'claude_code' else '.agents', 'skills')


def _write_private(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def credentials_path(env):
    return os.path.join(consolex_home(env), 'credentials.json')


def load_credentials(env):
    data = _read_json(credentials_path(env), {})
    return data if isinstance(data, dict) and isinstance(data.get('servers'), dict) else {'version': 1, 'servers': {}}


def save_connection(env, api_base, connection):
    data = load_credentials(env)
    data['servers'][api_base] = connection
    data['default'] = api_base
    _write_private(credentials_path(env), data)


def forget_connection(env, api_base):
    data = load_credentials(env)
    data['servers'].pop(api_base, None)
    if data.get('default') == api_base:
        data['default'] = next(iter(data['servers']), None)
    _write_private(credentials_path(env), data)


def default_api_base(env, argument=''):
    explicit = argument.strip().rstrip('/')
    if explicit:
        return explicit if '://' in explicit else 'https://' + explicit
    return (env.get('CONSOLEX_API_BASE') or load_credentials(env).get('default') or DEFAULT_API_BASE).rstrip('/')


def connection_for(env, api_base):
    """(token, connection) for a server; CONSOLEX_TOKEN overrides for unattended runs."""
    connection = load_credentials(env)['servers'].get(api_base.rstrip('/')) or {}
    return env.get('CONSOLEX_TOKEN') or connection.get('token') or '', connection


def _state_dir(env):
    base = env.get('CONSOLEX_DATA') or env.get('CLAUDE_PLUGIN_DATA') or consolex_home(env)
    if client(env) != 'claude_code':
        base = os.path.join(base, client(env))
    path = os.path.join(base, 'sessions')
    os.makedirs(path, exist_ok=True)
    return path


def _marker_path(env, session_id):
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', str(session_id or ''))[:128]
    return os.path.join(_state_dir(env), safe + '.json') if safe else None


def load_marker(env, session_id):
    path = _marker_path(env, session_id)
    marker = _read_json(path, None) if path and os.path.exists(path) else None
    if not marker or time.time() - float(marker.get('started_at') or 0) > MARKER_TTL_SECONDS:
        return None
    return marker


def save_marker(env, marker):
    path = _marker_path(env, marker['session_id'])
    _write_private(path, marker)


def clear_marker(env, session_id):
    path = _marker_path(env, session_id)
    if path and os.path.exists(path):
        os.remove(path)


def _cleanup_old_markers(env):
    try:
        folder = _state_dir(env)
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            if time.time() - os.path.getmtime(full) > MARKER_TTL_SECONDS:
                os.remove(full)
    except OSError:
        pass


def find_skill_dir(name, env, cwd):
    """The folder of a ConsoleX task Skill called `name`, or None."""
    if not name or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', name):
        return None
    roots = [os.path.join(root, '.claude' if client(env) == 'claude_code' else '.agents', 'skills')
             for root in (env.get('CLAUDE_PROJECT_DIR'), cwd) if root]
    if client(env) != 'claude_code':
        parent = os.path.abspath(cwd)
        while parent != os.path.dirname(parent):
            roots.append(os.path.join(parent, '.agents', 'skills'))
            parent = os.path.dirname(parent)
    roots.append(skills_dir(env))
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.isfile(os.path.join(candidate, 'consolex.json')):
            return candidate
    return None


def load_config(skill_dir):
    with open(os.path.join(skill_dir, 'consolex.json'), encoding='utf-8') as handle:
        return json.load(handle)


def _zh(config):
    return (config or {}).get('language') == 'zh'


def http_json(method, url, token, payload=None, timeout=30):
    """(status, body) for a JSON request; status 0 means ConsoleX could not be reached."""
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    headers = {'Content-Type': 'application/json', 'User-Agent': 'consolex-coding-agents/1.0'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode('utf-8') or '{}')
        except ValueError:
            body = {}
        return exc.code, body
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, {'error_msg': str(exc)}


def _task_url(config, env, suffix):
    api = (env.get('CONSOLEX_API_BASE') or config.get('api_base') or '').rstrip('/')
    return '%s/api/operator/external/tasks/%s/%s' % (api, config.get('task_uuid'), suffix)


def _api(config, env):
    return (env.get('CONSOLEX_API_BASE') or config.get('api_base') or '').rstrip('/')


def _app(config, env):
    return (env.get('CONSOLEX_APP_BASE') or config.get('app_base') or '').rstrip('/')


def _token(config, env):
    return connection_for(env, _api(config, env))[0]


def _run_link(config, env, result):
    path = (result or {}).get('run_path')
    return (_app(config, env) + path) if path and _app(config, env) else ''


def _context(data, text):
    return {'hookSpecificOutput': {'hookEventName': data.get('hook_event_name'), 'additionalContext': text}}


# ---------------------------------------------------------------- connecting (device code)

def _pending_login_path(env):
    return os.path.join(consolex_home(env), 'pending-login.json')


def _login_status_path(env):
    return os.path.join(consolex_home(env), 'login-status.json')


def poll_once(env, http=http_json, now=None):
    """One poll of a pending sign-in. Returns 'pending', 'approved', 'denied', 'expired' or 'none'."""
    pending = _read_json(_pending_login_path(env), None)
    if not pending:
        return 'none'
    now = time.time() if now is None else now
    if now > float(pending.get('expires_at') or 0):
        os.remove(_pending_login_path(env))
        _write_private(_login_status_path(env), {'status': 'expired', 'at': now})
        return 'expired'
    status, body = http('POST', pending['api_base'] + '/api/operator/external/device/token', '',
                        {'device_code': pending['device_code']}, timeout=QUICK_TIMEOUT)
    outcome = body.get('status') if status == 200 else 'pending'
    if outcome == 'approved' and body.get('token'):
        save_connection(env, pending['api_base'], {
            'token': body['token'], 'app_base': pending.get('app_base'), 'device': (body.get('device') or {}).get('name'),
            'account': (body.get('account') or {}).get('email'), 'client': pending.get('client', CLIENT), 'connected_at': int(now)})
        os.remove(_pending_login_path(env))
        _write_private(_login_status_path(env), {'status': 'approved', 'api_base': pending['api_base'],
                                                 'account': (body.get('account') or {}).get('email'), 'at': now})
        return 'approved'
    if outcome in ('denied', 'expired', 'invalid'):
        os.remove(_pending_login_path(env))
        _write_private(_login_status_path(env), {'status': outcome, 'at': now})
        return outcome
    return 'pending'


def poll_until_done(env):
    """Background poller started by `/consolex:login`: waits for the browser approval."""
    while True:
        pending = _read_json(_pending_login_path(env), None)
        if not pending:
            return
        time.sleep(max(2, int(pending.get('interval') or 5)))
        if poll_once(env) != 'pending':
            return


def _device_name():
    """How the computer is shown on the approval page and in Settings: the Mac's name, else the host name."""
    if sys.platform == 'darwin':
        try:
            name = subprocess.run(['scutil', '--get', 'ComputerName'], capture_output=True, text=True, timeout=2).stdout.strip()
            if name:
                return name[:120]
        except (OSError, subprocess.SubprocessError):
            pass
    name = socket.gethostname()
    return name[:120] if name and not re.fullmatch(r'[0-9.:]+', name) else 'Claude Code'


def _open_browser(url, env):
    if env.get('CONSOLEX_NO_BROWSER'):
        return False
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _spawn_poller(env):
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), 'poll'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ, **{key: value for key, value in env.items() if key.startswith('CONSOLEX_')}})
        return True
    except OSError:
        return False


def on_login(data, env, argument, http=http_json, spawn=_spawn_poller):
    api_base = default_api_base(env, argument)
    token, connection = connection_for(env, api_base)
    if token and not argument.strip().endswith('--again'):
        status, body = http('GET', api_base + '/api/operator/external/device', token, timeout=QUICK_TIMEOUT)
        if status == 200 and body.get('succ'):
            return _context(data, 'ConsoleX is already connected on this computer: %s, account %s, device "%s". '
                                  'Tell the user in one line; /consolex:logout disconnects it.' % (
                                      api_base, (body.get('account') or {}).get('email'), (body.get('device') or {}).get('name')))
    status, body = http('POST', api_base + '/api/operator/external/device/start', '',
                        {'client': client(env), 'name': _device_name()}, timeout=QUICK_TIMEOUT)
    if status != 200 or not body.get('succ'):
        return _context(data, 'ConsoleX sign-in could not start (%s, %s). Tell the user; if this is a self-hosted or '
                              'development ConsoleX, run /consolex:login <its API address>.' % (
                                  api_base, body.get('error_msg') or 'HTTP %s' % status))
    _write_private(_pending_login_path(env), {
        'client': client(env), 'api_base': api_base, 'device_code': body['device_code'], 'interval': body.get('interval') or 5,
        'expires_at': time.time() + float(body.get('expires_in') or 600),
        'app_base': (body.get('verification_uri') or '').split('/connect/')[0] or None})
    opened = _open_browser(body['verification_uri_complete'], env)
    polling = spawn(env)
    return _context(data, (
        'ConsoleX sign-in started for %s. %s Approve the code %s on the ConsoleX page: %s . '
        'The code expires in 10 minutes. %s Tell the user exactly this in two short lines; do not run any commands.') % (
        api_base, 'The browser was opened.' if opened else 'Open this link in a browser.', body['user_code'],
        body['verification_uri_complete'],
        'ConsoleX saves the connection automatically once approved; afterwards /consolex:tasks lists the tasks.'
        if polling else 'After approving, run /consolex:login again to finish.'))


def on_logout(data, env, argument, http=http_json):
    api_base = default_api_base(env, argument)
    token, _ = connection_for(env, api_base)
    if not token:
        return _context(data, 'ConsoleX was not connected on this computer (%s). Tell the user in one line.' % api_base)
    http('DELETE', api_base + '/api/operator/external/device', token, timeout=QUICK_TIMEOUT)
    forget_connection(env, api_base)
    return _context(data, 'ConsoleX is disconnected on this computer (%s); the device was removed from the account. '
                          'Tell the user in one line.' % api_base)


def _not_connected(data, api_base):
    return _context(data, 'ConsoleX is not connected on this computer (%s). Tell the user to run /consolex:login '
                          'first. Do not run anything else.' % api_base)


def _installed_skills(env):
    """{task uuid: skill folder name} of the task Skills installed for this user."""
    installed = {}
    root = skills_dir(env)
    try:
        names = os.listdir(root)
    except OSError:
        return installed
    for name in names:
        config = _read_json(os.path.join(root, name, 'consolex.json'), None)
        if isinstance(config, dict) and config.get('task_uuid'):
            installed[config['task_uuid']] = name
    return installed


def _task_lines(tasks, installed):
    lines = []
    for item in tasks:
        status = 'installed as /%s' % installed[item['uuid']] if item['uuid'] in installed else 'not installed'
        lines.append('- %s — %s, %s' % (item['name'], 'has an acceptance standard' if item.get('standard')
                                         else 'no acceptance standard yet', status))
    return '\n'.join(lines)


def _list_tasks(env, api_base, token, query, http):
    url = api_base + '/api/operator/external/tasks'
    if query:
        url += '?' + urllib.parse.urlencode({'q': query})
    return http('GET', url, token, timeout=QUICK_TIMEOUT)


def on_tasks(data, env, argument, http=http_json):
    api_base = default_api_base(env)
    token, _ = connection_for(env, api_base)
    if not token:
        return _not_connected(data, api_base)
    status, body = _list_tasks(env, api_base, token, argument.strip(), http)
    if status == 401:
        return _not_connected(data, api_base)
    if status != 200:
        return _context(data, 'ConsoleX could not list tasks (%s). Tell the user.' % (body.get('error_msg') or 'HTTP %s' % status))
    tasks = body.get('tasks') or []
    if not tasks:
        return _context(data, 'No ConsoleX task matches. Tell the user in one line.')
    return _context(data, ('ConsoleX tasks (%s of %s):\n%s\n\nShow this list to the user as is. To add one to Claude Code '
                           'they run /consolex:add followed by words from its name. Do not run anything.') % (
        len(tasks), body.get('total', len(tasks)), _task_lines(tasks, _installed_skills(env))))


def install_skill(env, bundle):
    """Write a task Skill into the skills folder; an installed copy of the same task is updated in place."""
    config = json.loads(bundle['files']['consolex.json'])
    root = skills_dir(env)
    folder = _installed_skills(env).get(config['task_uuid']) or bundle['skill']
    if not SKILL_NAME.fullmatch(folder):
        raise ValueError('Unexpected Skill name')
    target = os.path.join(root, folder)
    os.makedirs(target, exist_ok=True)
    for name in ('SKILL.md', 'consolex.json'):
        content = bundle['files'][name]
        if name == 'SKILL.md' and folder != bundle['skill']:
            content = content.replace('name: %s\n' % bundle['skill'], 'name: %s\n' % folder, 1)
        with open(os.path.join(target, name), 'w', encoding='utf-8') as handle:
            handle.write(content)
    return folder, target


def on_add(data, env, argument, http=http_json):
    api_base = default_api_base(env)
    token, _ = connection_for(env, api_base)
    if not token:
        return _not_connected(data, api_base)
    query = argument.strip()
    if not query:
        return on_tasks(data, env, '', http=http)
    status, body = _list_tasks(env, api_base, token, query, http)
    if status == 401:
        return _not_connected(data, api_base)
    if status != 200:
        return _context(data, 'ConsoleX could not find tasks (%s). Tell the user.' % (body.get('error_msg') or 'HTTP %s' % status))
    tasks = body.get('tasks') or []
    if not tasks:
        return _context(data, 'No ConsoleX task matches "%s". Tell the user; /consolex:tasks lists all tasks.' % query)
    if len(tasks) > 1:
        return _context(data, ('Several ConsoleX tasks match "%s":\n%s\n\nAsk the user which one, then they run '
                               '/consolex:add with more words of its name. Do not run anything.') % (
            query, _task_lines(tasks[:10], _installed_skills(env))))
    task = tasks[0]
    status, bundle = http('GET', api_base + '/api/operator/external/tasks/%s/skill' % task['uuid'], token, timeout=QUICK_TIMEOUT)
    if status != 200 or not bundle.get('succ'):
        return _context(data, 'ConsoleX could not export "%s" (%s). Tell the user.' % (
            task['name'], bundle.get('error_msg') or 'HTTP %s' % status))
    folder, target = install_skill(env, bundle)
    outcome = install_task_skills(env, api_base, token, task['uuid'], bundle.get('skills') or [], http=http)
    names = outcome['installed'] + outcome['updated'] + outcome['current'] + outcome['kept']
    if names:
        _list_task_skills(target, names, json.loads(bundle['files']['consolex.json']).get('language') == 'zh')
    note = '' if task.get('standard') else (' It has no acceptance standard yet, so deliveries are recorded but not '
                                            'checked until one is saved in ConsoleX.')
    note += _skills_note(outcome, bundle.get('skipped_skills') or [])
    return _context(data, ('Added the ConsoleX task "%s" as the Skill /%s (%s).%s Tell the user in two to four lines: start '
                           'it with /%s or just ask for the task. Do not run anything.') % (
        task['name'], folder, target, note, folder))


# ---------------------------------------------------------------- the ConsoleX Skills a task uses

SKILL_MANIFEST = '.consolex-skill.json'  # marks a Skill folder this client installed (never the owner's own)
SKILL_FOLDER = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
SKILL_FILES_TIMEOUT = 90
MAX_SKILL_FILE_COUNT = 300
_FRONTMATTER_NAME = re.compile(r'\A---[ \t]*\r?\n(?:.*\r?\n)*?name:[ \t]*["\']?([^"\'\r\n]+?)["\']?[ \t]*\r?\n')
SKIPPED_REASONS = {'shared_agent': "it belongs to a shared Agent and stays its owner's",
                   'not_yours': 'it belongs to someone else', 'unavailable': 'it was deleted or switched off',
                   'consolex_only': 'it only works inside ConsoleX', 'too_many': 'a task installs at most 20 Skills'}


def _installed_task_skills(env):
    """{ConsoleX Skill uuid: (folder name, manifest)} for the Skills installed with tasks."""
    found, root = {}, skills_dir(env)
    try:
        names = os.listdir(root)
    except OSError:
        return found
    for name in names:
        manifest = _read_json(os.path.join(root, name, SKILL_MANIFEST), None)
        if isinstance(manifest, dict) and manifest.get('skill_uuid'):
            found[manifest['skill_uuid']] = (name, manifest)
    return found


def _skill_relpath(path):
    """A file path inside a Skill folder, or ValueError: never absolute, `..`, a drive or an empty segment."""
    parts = str(path or '').replace('\\', '/').split('/')
    if not path or str(path).startswith('/') or any(part in ('', '.', '..') for part in parts) or ':' in parts[0]:
        raise ValueError('Unsafe path in a Skill: %r' % path)
    return os.path.join(*parts)


def _skill_folder_name(skill_md, fallback):
    """The folder a Skill installs into: its SKILL.md `name` when that is a valid Skill name, else ConsoleX's slug."""
    match = _FRONTMATTER_NAME.match(skill_md.decode('utf-8', 'replace'))
    name = match[1].strip().lower() if match else ''
    return name if SKILL_FOLDER.fullmatch(name) else fallback


def _write_skill_folder(root, folder, files, manifest):
    """Write a Skill folder in full, then swap it in, so a failed download never leaves half a Skill."""
    staging = os.path.join(root, '.consolex-staging-' + uuid.uuid4().hex)
    try:
        for relative, content in files.items():
            path = os.path.join(staging, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as handle:
                handle.write(content)
        with open(os.path.join(staging, SKILL_MANIFEST), 'w', encoding='utf-8') as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=1)
        target = os.path.join(root, folder)
        if os.path.exists(target):
            old = os.path.join(root, '.consolex-old-' + uuid.uuid4().hex)
            os.replace(target, old)
            os.replace(staging, target)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_task_skills(env, api_base, token, task_uuid, skills, http=http_json):
    """Install or update the ConsoleX Skills a task uses beside its task Skill.

    A Skill whose installed copy has the same version is left as is. A folder of the same name that this client
    did not install (the owner's own Skill) is never replaced. Environment variable values stay in ConsoleX;
    the result lists the names the owner sets on this computer and the ConsoleX tools a Skill relies on."""
    outcome = {'installed': [], 'updated': [], 'current': [], 'kept': [], 'failed': [], 'env': {}, 'tools': {}}
    root = skills_dir(env)
    installed = _installed_task_skills(env)
    for item in skills:
        if not isinstance(item, dict) or not item.get('uuid'):
            continue
        existing = installed.get(item['uuid'])
        if existing and existing[1].get('version') == item.get('version') and os.path.isfile(
                os.path.join(root, existing[0], 'SKILL.md')):
            folder, manifest = existing
            if task_uuid not in manifest.get('tasks', []):
                manifest['tasks'] = (manifest.get('tasks') or []) + [task_uuid]
                _write_private(os.path.join(root, folder, SKILL_MANIFEST), manifest)
            outcome['current'].append(folder)
        else:
            status, body = http('GET', '%s/api/operator/external/tasks/%s/skills/%s' % (api_base, task_uuid, item['uuid']),
                                token, timeout=SKILL_FILES_TIMEOUT)
            if status != 200 or not body.get('succ'):
                outcome['failed'].append((item.get('name') or item.get('title'), body.get('error_msg') or 'HTTP %s' % status))
                continue
            try:
                entries = body.get('files') or []
                if len(entries) > MAX_SKILL_FILE_COUNT:
                    raise ValueError('too many files')
                files = {_skill_relpath(entry['path']): base64.b64decode(entry['data'], validate=True) for entry in entries}
                if 'SKILL.md' not in files:
                    raise ValueError('no SKILL.md')
            except (KeyError, TypeError, ValueError) as exc:
                outcome['failed'].append((item.get('name') or item.get('title'), str(exc)[:200]))
                continue
            folder = existing[0] if existing else _skill_folder_name(files['SKILL.md'], item.get('name') or 'skill')
            if not existing and os.path.exists(os.path.join(root, folder)):
                outcome['kept'].append(folder)  # The owner's own Skill (or another one) already has this name.
                continue
            tasks = sorted(set((existing[1].get('tasks') if existing else None) or []) | {task_uuid})
            _write_skill_folder(root, folder, files, {
                'skill_uuid': item['uuid'], 'title': item.get('title'), 'version': item.get('version'),
                'hash': body.get('hash'), 'api_base': api_base, 'tasks': tasks, 'installed_at': int(time.time())})
            outcome['updated' if existing else 'installed'].append(folder)
        if item.get('env'):
            outcome['env'][folder] = list(item['env'])
        if item.get('tools'):
            outcome['tools'][folder] = list(item['tools'])
    return outcome


_SKILLS_SECTION = re.compile(r'\n## (?:Skills for this task|本任务用到的 Skills)\n.*\Z', re.S)


def _list_task_skills(task_dir, names, zh):
    """Name the installed Skills at the end of the task Skill (replacing an earlier list), so the agent loads them."""
    path = os.path.join(task_dir, 'SKILL.md')
    try:
        with open(path, encoding='utf-8') as handle:
            text = _SKILLS_SECTION.sub('', handle.read()).rstrip('\n') + '\n'
    except OSError:
        return
    if names:
        listed = ('、' if zh else ', ').join('`%s`' % name for name in names)
        text += (('\n## 本任务用到的 Skills\n\n这个任务会用到这些 Skill（随任务一起安装）：%s。做到相关步骤时加载使用。\n') if zh else
                 ('\n## Skills for this task\n\nThis task uses these Skills, installed with it: %s. Load them when a step needs them.\n')) % listed
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)


SKILLS_REFRESH_SECONDS = 3600


def _refresh_state_path(env):
    return os.path.join(consolex_home(env), 'skills-refresh.json')


def on_session_start(data, env, spawn=None):
    """SessionStart: refresh the installed tasks and their Skills in the background, at most once an hour.

    Never blocks the session: only the throttle check and a detached process run here."""
    write_launcher(env)
    if not _installed_skills(env):
        return None
    state = _read_json(_refresh_state_path(env), {})
    state = state if isinstance(state, dict) else {}
    key = os.path.abspath(skills_dir(env))
    if time.time() - float(state.get(key) or 0) < SKILLS_REFRESH_SECONDS:
        return None
    state[key] = time.time()
    _write_private(_refresh_state_path(env), state)
    (spawn or _spawn_refresher)(env)
    return None


def _spawn_refresher(env):
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), 'refresh'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ, **{key: value for key, value in env.items() if key.startswith('CONSOLEX_')}})
        return True
    except OSError:
        return False


def refresh_installed_tasks(env, http=http_json):
    """Re-export every installed task (method on disk) and bring its ConsoleX Skills up to date.

    A task this computer can no longer read (deleted, signed out, unreachable) keeps its installed copy."""
    results = {}
    for task_uuid, folder in _installed_skills(env).items():
        config = _read_json(os.path.join(skills_dir(env), folder, 'consolex.json'), {})
        token = _token(config, env)
        if not token or _outdated(config):
            continue
        status, bundle = http('GET', _task_url(config, env, 'skill'), token, timeout=QUICK_TIMEOUT)
        if status != 200 or not bundle.get('succ'):
            results[task_uuid] = 'HTTP %s' % status
            continue
        _, target = install_skill(env, bundle)
        outcome = install_task_skills(env, _api(config, env), token, task_uuid, bundle.get('skills') or [], http=http)
        _list_task_skills(target, outcome['installed'] + outcome['updated'] + outcome['current'] + outcome['kept'],
                          json.loads(bundle['files']['consolex.json']).get('language') == 'zh')
        results[task_uuid] = outcome
    return results


def _skills_note(outcome, skipped):
    parts = []
    for key, label in (('installed', 'installed'), ('updated', 'updated'), ('current', 'already up to date')):
        if outcome[key]:
            parts.append('%s %s' % (label, ', '.join(outcome[key])))
    note = (' Its ConsoleX Skills: %s.' % '; '.join(parts)) if parts else ''
    if outcome['kept']:
        note += (' You already have a Skill named %s, which was kept and not replaced; the task uses yours.' %
                 ', '.join(outcome['kept']))
    for name, why in outcome['failed']:
        note += ' Could not install the Skill %s (%s).' % (name, why)
    for item in skipped:
        note += ' Not installed: %s, because %s.' % (
            'the Skill %s' % item['name'] if item.get('name') else 'one Skill',
            SKIPPED_REASONS.get(item.get('reason'), 'it cannot be exported'))
    for folder, names in outcome['env'].items():
        note += (' The Skill %s reads environment variables %s: set them on this computer if you use them '
                 '(the values saved in ConsoleX stay there).' % (folder, ', '.join(names)))
    for folder, names in outcome['tools'].items():
        note += (' The Skill %s relies on ConsoleX tools (%s), which are not available here; Claude uses its own tools '
                 'for those steps.' % (folder, ', '.join(names)))
    return note


# ---------------------------------------------------------------- reading a delivery

def _section(text, heading):
    match = heading.search(text)
    if not match:
        return None, None
    start = match.end()
    following = ANY_HEADING.search(text, start)
    end = following.start() if following else len(text)
    return match, text[start:end]


def parse_delivery(message):
    """The declared delivery in a reply, or None when the reply declares none."""
    if not isinstance(message, str):
        return None
    match, section = _section(message, DELIVERY_HEADING)
    if not match:
        return None
    files = [item.strip() for item in FILE_LINE.findall(section or '') if item.strip()]
    body = (message[:match.start()] + message[match.start() + len(match.group(0)) + len(section or ''):]).strip()
    body, items = _merge_confirm_sections(body)
    return {'kind': 'files' if files else 'reply', 'files': files[:20], 'body': body, 'confirm_items': items[:20]}


def _merge_confirm_sections(text):
    """(text with at most one confirmation section, its items).

    A task's own method often asks for 「待你确认」 and the output contract asks for it too; a reply that writes both
    would reach the judge with the section twice. Later copies fold into the first, items deduplicated, "无"/"None"
    only when nothing is left."""
    sections = []
    for match in CONFIRM_HEADING.finditer(text):
        following = ANY_HEADING.search(text, match.end())
        sections.append((match, following.start() if following else len(text)))
    items = []
    for match, end in sections:
        for line in text[match.end():end].splitlines():
            item = re.sub(r'^\s*(?:[-*]|\d+[.)])\s*', '', line).strip()[:1000]
            if item and item.lower().strip('。.') not in NONE_ITEMS and item not in items:
                items.append(item)
    if len(sections) < 2:
        return text, items
    first, first_end = sections[0]
    none = '无' if re.search(r'[\u4e00-\u9fff]', first.group(0)) else 'None'
    merged = first.group(0).rstrip() + '\n' + ('\n'.join('- ' + item for item in items) if items else '- ' + none) + '\n\n'
    out, cursor = text[:first.start()] + merged, first_end
    for match, end in sections[1:]:
        out += text[cursor:match.start()]
        cursor = end
    out += text[cursor:]
    return re.sub(r'\n{3,}', '\n\n', out).strip(), items


def read_files(paths, cwd, env, config, since):
    """(text, problems) for declared deliverable files; only recent text files under allowed roots."""
    roots = [os.path.realpath(root) for root in
             [cwd, env.get('CLAUDE_PROJECT_DIR')] + list(config.get('extra_roots') or []) if root]
    chunks, problems, total = [], [], 0
    zh = _zh(config)
    for raw in paths:
        path = os.path.realpath(os.path.expanduser(raw if os.path.isabs(os.path.expanduser(raw)) else os.path.join(cwd, raw)))
        if not any(path == root or path.startswith(root + os.sep) for root in roots):
            problems.append(('%s：不在当前项目目录里（可在 consolex.json 的 extra_roots 里加上它的目录）' if zh else
                             '%s: outside the project folder (add its folder to extra_roots in consolex.json)') % raw)
            continue
        if os.path.splitext(path)[1].lower() not in TEXT_SUFFIXES:
            problems.append(('%s：不是文本文件，无法检查' if zh else '%s: not a text file, so it cannot be checked') % raw)
            continue
        if not os.path.isfile(path):
            problems.append(('%s：文件不存在' if zh else '%s: file not found') % raw)
            continue
        if os.path.getmtime(path) + 2 < since:
            problems.append(('%s：本次任务开始后没有改过这个文件' if zh else '%s: not changed since this task started') % raw)
            continue
        if os.path.getsize(path) > MAX_FILE_BYTES:
            problems.append(('%s：文件超过 200 KB' if zh else '%s: larger than 200 KB') % raw)
            continue
        try:
            with open(path, encoding='utf-8') as handle:
                content = handle.read()
        except (OSError, UnicodeDecodeError):
            problems.append(('%s：不是 UTF-8 文本' if zh else '%s: not UTF-8 text') % raw)
            continue
        total += len(content)
        if total > MAX_TOTAL_CHARS:
            problems.append(('%s：交付文件合计超过 40 万字符' if zh else '%s: deliverable files exceed 400,000 characters') % raw)
            break
        chunks.append('=== %s ===\n%s' % (raw, content))
    return '\n\n'.join(chunks), problems


def transcript_facts(transcript_path):
    """(model, last assistant text) from the end of the session transcript."""
    model, last_text = None, ''
    if not transcript_path or not os.path.isfile(transcript_path):
        return model, last_text
    try:
        with open(transcript_path, 'rb') as handle:
            size = os.path.getsize(transcript_path)
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            lines = handle.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return model, last_text
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get('type') != 'assistant':
            continue
        message = entry.get('message') or {}
        model = model or message.get('model')
        if not last_text:
            texts = [part.get('text') for part in message.get('content') or []
                     if isinstance(part, dict) and part.get('type') == 'text' and part.get('text')]
            if texts:
                last_text = '\n'.join(texts)
        if model and last_text:
            break
    return model, last_text


def _entry_time(entry):
    stamp = entry.get('timestamp')
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def owner_answered_since(transcript_path, since):
    """True when the owner answered an AskUserQuestion after `since` (epoch seconds) in this transcript."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return False
    try:
        with open(transcript_path, 'rb') as handle:
            size = os.path.getsize(transcript_path)
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            lines = handle.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return False
    asked = set()
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get('message') or {}).get('content')
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get('type') == 'tool_use' and part.get('name') == 'AskUserQuestion':
                asked.add(part.get('id'))
            elif part.get('type') == 'tool_result' and part.get('tool_use_id') in asked and not part.get('is_error'):
                answered_at = _entry_time(entry)
                if answered_at is not None and answered_at >= since:
                    return True
    return False


# ---------------------------------------------------------------- tool evidence

MAX_TOOL_RECORDS = 80
MAX_TOOL_INPUT_CHARS = 2000
MAX_TOOL_OUTPUT_CHARS = 8000
MAX_TOOL_TOTAL_CHARS = 300_000
# Bookkeeping and conversation tools are not evidence of the work (the desktop app's own tools start with mcp__ccd_).
SKIPPED_TOOLS = {'AskUserQuestion', 'TodoWrite', 'ToolSearch', 'Skill', 'EnterPlanMode', 'ExitPlanMode'}
SKIPPED_TOOL_PREFIXES = ('mcp__ccd_',)
_SECRET_PATTERNS = [
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.DOTALL), '[redacted private key]'),
    (re.compile(r'(?i)(bearer\s+)[^\s"\']+'), r'\1[redacted]'),
    (re.compile(r'(?i)((?:api[_-]?key|access[_-]?token|token|password|passwd|secret|authorization)["\']?\s*[:=]\s*["\']?)[^\s"\',;]+'),
     r'\1[redacted]'),
    (re.compile(r'\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}'), '[redacted]'),
    (re.compile(r'\b(?:cxd|cxt|ghp|gho|ghs|github_pat|xox[abprs])_[A-Za-z0-9_-]{10,}'), '[redacted]'),
    (re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}'), '[redacted]'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), '[redacted]'),
]


def redact(text):
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _clip(text, limit):
    """(text, clipped): long text keeps its head and tail, where commands print their results."""
    if len(text) <= limit:
        return text, False
    head, tail = int(limit * 0.75), int(limit * 0.25)
    return '%s\n[… %s characters omitted …]\n%s' % (text[:head], len(text) - head - tail, text[-tail:]), True


def _tool_input(name, value):
    """The part of a tool's input that says what was done; file contents being written are not evidence."""
    if not isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    keep = {
        'Bash': ('command', 'description'), 'Read': ('file_path', 'offset', 'limit'), 'Write': ('file_path',),
        'Edit': ('file_path',), 'MultiEdit': ('file_path',), 'NotebookEdit': ('notebook_path',),
        'Grep': ('pattern', 'path', 'glob'), 'Glob': ('pattern', 'path'), 'WebFetch': ('url', 'prompt'),
        'WebSearch': ('query',), 'Agent': ('description', 'subagent_type', 'prompt'), 'Task': ('description', 'prompt'),
    }.get(name)
    picked = {key: value[key] for key in keep if key in value} if keep else value
    if name == 'Write' and isinstance(value.get('content'), str):
        picked = dict(picked, content_chars=len(value['content']))
    return json.dumps(picked, ensure_ascii=False, default=str)


def _result_text(content, saved_output):
    """(text, images) of a tool result; an output saved beside the transcript is read from its file."""
    images = 0
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get('type') == 'text':
                parts.append(part.get('text') or '')
            elif part.get('type') == 'image':
                images += 1
        text = '\n'.join(parts)
    else:
        text = content if isinstance(content, str) else ''
    if saved_output and (not text.strip() or 'tool-results/' in text):
        try:
            with open(saved_output, encoding='utf-8', errors='replace') as handle:
                text = handle.read(MAX_TOOL_OUTPUT_CHARS * 4)
        except OSError:
            pass
    return text, images


def collect_tool_evidence(transcript_path, since):
    """Tool calls and results in this session since the task started, from Claude Code's own transcript.

    Returns (records, notes). Claude Code writes tool results itself, so the model cannot edit them,
    but anything a command prints is reported as printed. Only the main conversation is read:
    subagents' own steps stay in their own files and are not included."""
    notes = {'dropped': 0, 'images': 0, 'clipped': 0, 'unanswered': 0}
    if not transcript_path or not os.path.isfile(transcript_path):
        return None, notes
    saved_dir = os.path.join(os.path.splitext(transcript_path)[0], 'tool-results')
    calls, order = {}, []
    try:
        with open(transcript_path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get('isSidechain'):
                    continue
                stamp = _entry_time(entry)
                if stamp is not None and stamp < since:
                    continue
                content = (entry.get('message') or {}).get('content')
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get('type') == 'tool_use' and part.get('id'):
                        name = str(part.get('name') or '')
                        if name in SKIPPED_TOOLS or name.startswith(SKIPPED_TOOL_PREFIXES):
                            continue
                        calls[part['id']] = {'name': name, 'input': part.get('input'), 'result': None}
                        order.append(part['id'])
                    elif part.get('type') == 'tool_result' and part.get('tool_use_id') in calls:
                        calls[part['tool_use_id']]['result'] = part
    except OSError:
        return None, notes
    records, total = [], 0
    for call_id in reversed(order):  # The latest work matters most; older calls are dropped first.
        call = calls[call_id]
        if call['result'] is None:
            notes['unanswered'] += 1
            continue
        if len(records) >= MAX_TOOL_RECORDS or total >= MAX_TOOL_TOTAL_CHARS:
            notes['dropped'] += 1
            continue
        saved = os.path.join(saved_dir, re.sub(r'[^A-Za-z0-9_-]', '', call_id) + '.txt')
        text, images = _result_text(call['result'].get('content'), saved if os.path.isfile(saved) else None)
        notes['images'] += images
        tool_input, _ = _clip(redact(_tool_input(call['name'], call['input'])), MAX_TOOL_INPUT_CHARS)
        output, clipped = _clip(redact(text), MAX_TOOL_OUTPUT_CHARS)
        notes['clipped'] += int(clipped)
        if images and not text.strip():
            output = '[%s image(s) omitted]' % images
        total += len(tool_input) + len(output)
        records.append({'name': call['name'][:200], 'input': tool_input, 'output': output,
                        'is_error': call['result'].get('is_error') is True, 'clipped': clipped})
    records.reverse()
    return records, notes


def tool_evidence(env, data, marker):
    """(records, notes) to submit, or (None, notes) when not collected or turned off."""
    if str(env.get('CONSOLEX_TOOL_EVIDENCE') or '').strip().lower() in ('0', 'off', 'false', 'no'):
        return None, {'off': True}
    if 'tools' in data:
        return data['tools'], data.get('tool_notes') or {}
    return collect_tool_evidence(data.get('transcript_path'), float(marker.get('started_at') or 0))


# ---------------------------------------------------------------- messages

def short_name(text, limit=60):
    """A check name cut at a word boundary with an ellipsis (names saved from long descriptions can be cut mid-word)."""
    text = ' '.join(str(text or '').split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(' ')
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(' ,.;:，。；：、') + '…'


def _check_lines(items, zh):
    """One line per check that did not pass: its full description from ConsoleX (names can be shortened), then the reason."""
    lines = []
    for item in items:
        label = item.get('description') or item.get('name') or item.get('key')
        reason = (item.get('reason') or '').strip()
        lines.append('- %s%s' % (label, ('：' if zh else ': ') + reason if reason else ''))
    return '\n'.join(lines)


def _confirm_list(items, zh):
    if not items:
        return ''
    return (('\n\n这次交付还列出了待负责人确认的项：\n%s' if zh else
             '\n\nThe delivery also lists items for the owner to confirm:\n%s') % '\n'.join('- %s' % item for item in items))


def report_reason(config, summary, link, confirm_items=()):
    """The block reason that makes Claude report a result and ask the owner, without revising anything."""
    if _zh(config):
        return (summary + ('\n运行记录：%s' % link if link else '') + _confirm_list(confirm_items, True) + '\n\n'
                '现在不要修改交付物，也不要重新交付。用一条简短的消息（用对话的语言）告诉负责人这个结果：逐项列出上面没通过或要确认的检查项和原因，附上运行记录链接。'
                '然后问负责人要不要按这些项修改。能用 AskUserQuestion 就用它，第一个问题只给两个选项：「按这些项修改」和「先不改」。'
                + ('待负责人确认的项里，需要负责人做决定的，在同一次 AskUserQuestion 里各加一个问题（总共最多 4 个），每个给 2–4 个来自交付内容的具体选项；只是提醒的写在消息里。'
                   if confirm_items else '') +
                '负责人选了「按这些项修改」，再修改交付物并照输出约定重新交付（保留"ConsoleX 交付"段）；选了「先不改」就结束。'
                '没有 AskUserQuestion 就用一句话提问，然后停下，等负责人在下一条消息里回答。'
                '这条消息里不要写"ConsoleX 交付"段。需要负责人决定、或你手上没有的信息，不要编造。')
    return (summary + ('\nRun: %s' % link if link else '') + _confirm_list(confirm_items, False) + '\n\n'
            'Do not change the deliverable or deliver again now. In one short message, in the language of the conversation, '
            'tell the owner this result: list the checks above that failed or need review, with their reasons and the run link. '
            'Then ask the owner whether to revise the delivery for these items. If you have AskUserQuestion, use it; its first '
            'question has exactly two options: "Revise these items" and "Keep as is". '
            + ('For each item to confirm that needs the owner\'s decision, add one question to the same AskUserQuestion call '
               '(at most 4 questions in all), each with 2-4 concrete options taken from the delivery; mention items that are '
               'only reminders in your message. ' if confirm_items else '') +
            'If the owner picks "Revise these items", revise the deliverable and deliver again following the output contract '
            '(keep the "ConsoleX delivery" section); if "Keep as is", stop. Without AskUserQuestion, ask in one line and stop; '
            'the owner answers in the next message. Do not include a "ConsoleX delivery" section in this message, and do not '
            'invent anything that needs the owner\'s decision or information you do not have.')


def confirm_reason(config, result_line, confirm_items):
    """The block reason that makes Claude put the delivery's items to confirm in front of the owner as a question card."""
    if _zh(config):
        return (result_line + _confirm_list(confirm_items, True) + '\n\n'
                '现在不要修改交付物，也不要重新交付。用一条简短的消息（用对话的语言）告诉负责人：先用一句话说明上面的检查结果，附上运行记录链接；'
                '再请负责人确认这些项。能用 AskUserQuestion 就用它：每个需要负责人做决定的项一个问题（最多 4 个），'
                '每个问题给 2–4 个来自交付内容的具体选项；只是提醒、不需要决定的项写在消息里。'
                '没有 AskUserQuestion 就把这些项写成问题，然后停下，等负责人在下一条消息里回答。'
                '负责人回答后按回答继续；如果因此要改交付物，照输出约定重新交付（保留"ConsoleX 交付"段）。'
                '这条消息里不要写"ConsoleX 交付"段，也不要重复交付内容。需要负责人决定、或你手上没有的信息，不要编造。')
    return (result_line + _confirm_list(confirm_items, False) + '\n\n'
            'Do not change the deliverable or deliver again now. In one short message, in the language of the conversation, '
            'give the owner the check result above in one line with the run link, then ask the owner to confirm these items. '
            'If you have AskUserQuestion, use it: one question for each item that needs the owner\'s decision (at most 4), '
            'each with 2-4 concrete options taken from the delivery; mention items that are only reminders in your message. '
            'Without AskUserQuestion, write the items as questions and stop; the owner answers in the next message. After the '
            'owner answers, act on the answers; if that changes the deliverable, deliver again following the output contract '
            '(keep the "ConsoleX delivery" section). Do not include a "ConsoleX delivery" section in this message, do not '
            'repeat the deliverable, and do not invent anything that needs the owner\'s decision or information you do not have.')


def result_summary(config, result, attempt):
    zh = _zh(config)
    parts = []
    if result.get('failed'):
        parts.append(('未通过：\n%s' if zh else 'Failed:\n%s') % _check_lines(result['failed'], zh))
    if result.get('needs_review'):
        parts.append(('需要负责人确认：\n%s' if zh else 'Needs the owner\'s review:\n%s') % _check_lines(result['needs_review'], zh))
    if result.get('failed'):
        head = ('ConsoleX 检查了这次交付（第 %s 次），没有通过验收标准。' if zh else
                'ConsoleX checked this delivery (attempt %s); it did not pass the acceptance standard.') % attempt
    else:
        head = ('ConsoleX 检查了这次交付（第 %s 次），有检查项需要负责人确认。' if zh else
                'ConsoleX checked this delivery (attempt %s); some checks need the owner\'s review.') % attempt
    return head + '\n' + '\n'.join(parts)


def user_message(config, result, attempt, link):
    zh = _zh(config)
    verdict = result.get('verdict')
    tail = (' · ' + link) if link else ''
    if (result.get('judge') or {}).get('independent') is False:
        tail += (' · 评审未确认跨模型家族独立' if zh else ' · cross-family judge independence unconfirmed')
    judge = (result.get('judge') or {}).get('model')
    judged = (' · %s %s' % ('评审' if zh else 'judge', judge)) if judge else ''
    join = '、' if zh else ', '
    if verdict == 'passed':
        return ('ConsoleX ✓ 第 %s 次交付通过验收%s%s' if zh else 'ConsoleX ✓ delivery passed on attempt %s%s%s') % (attempt, judged, tail)
    if verdict == 'needs_review':
        names = join.join(short_name(item.get('name')) for item in result.get('needs_review') or [])
        return ('ConsoleX：第 %s 次交付需要你确认：%s%s' if zh else 'ConsoleX: attempt %s needs your review: %s%s') % (attempt, names, tail)
    if verdict == 'failed':
        names = join.join(short_name(item.get('name')) for item in result.get('failed') or [])
        return ('ConsoleX ✗ 第 %s 次交付未通过：%s；等你决定要不要修改%s' if zh else
                'ConsoleX ✗ attempt %s failed: %s; waiting for your decision%s') % (attempt, names, tail)
    if verdict == 'not_configured':
        return ('ConsoleX：交付已记录（这个任务还没有验收标准）%s' if zh else 'ConsoleX: delivery recorded (this task has no acceptance standard yet)%s') % tail
    return ('ConsoleX：交付已记录，但检查没能完成%s' if zh else 'ConsoleX: delivery recorded, but the check could not complete%s') % tail


SKILL_SCHEMA_VERSION = 3  # 3: task Skills no longer carry the acceptance standard


def _outdated(config):
    return not config.get('task_uuid') or int(config.get('schema_version') or 0) < SKILL_SCHEMA_VERSION


def _outdated_note(config):
    return ('This ConsoleX task Skill comes from an older export, which still lists the acceptance standard (agents no '
            'longer see it, so the check stays independent). Tell the user to run /consolex:add %s to update it; '
            'deliveries are not checked until then.' % (config.get('task_name') or ''))


# ---------------------------------------------------------------- hook handlers

def activate(data, env, skill_dir, http=http_json):
    """Mark the session as working on this Task and add the latest approved method to context."""
    config = load_config(skill_dir)
    if _outdated(config):
        return _context(data, _outdated_note(config))
    zh = _zh(config)
    session_id = data.get('session_id')
    marker = load_marker(env, session_id) or {}
    if marker.get('skill_dir') != skill_dir:
        marker = {'session_id': session_id, 'skill_dir': skill_dir, 'task_uuid': config.get('task_uuid'),
                  'started_at': time.time(), 'chains': {}}
    marker.pop('standard', None)  # Markers from plugin 0.6 and earlier kept a copy of the standard.
    marker['revision'] = config.get('revision')
    token = _token(config, env)
    if not token:
        save_marker(env, marker)
        return _context(data, ('ConsoleX 还没有在这台电脑上连接（%s），这个任务的交付无法检查。请告诉用户先运行 /consolex:login。'
                               if zh else 'ConsoleX is not connected on this computer (%s), so deliveries for this task '
                               'cannot be checked. Tell the user to run /consolex:login first.') % _api(config, env))
    status, body = http('GET', _task_url(config, env, 'method'), token, timeout=QUICK_TIMEOUT)
    exported = (config.get('revision') or {}).get('version')
    if status != 200 or not body.get('succ'):
        if status:
            session_event(config, env, data, marker, 'start', http=http)
        save_marker(env, marker)
        reason = body.get('error_msg') or ('HTTP %s' % status if status else 'unreachable')
        return _context(data, ('ConsoleX：拿不到最新方法（%s），按 Skill 里的 v%s 做。' if zh else
                               'ConsoleX: could not fetch the latest method (%s); follow v%s in the Skill.') % (reason, exported))
    marker['revision'] = body.get('revision')
    session_event(config, env, data, marker, 'start', http=http)
    save_marker(env, marker)
    latest = (body.get('revision') or {}).get('version')
    if latest == exported:
        note = ('ConsoleX：Skill 里的方法就是最新已批准的版本（v%s）。交付时照输出约定写，ConsoleX 会独立检查。' if zh else
                'ConsoleX: the method in the Skill is the latest approved (v%s). Follow the output contract when you deliver; ConsoleX checks it independently.') % latest
    else:
        note = (('ConsoleX：这个任务有更新的已批准方法 v%s（Skill 里是 v%s），以下面为准。\n\n## 方法\n%s\n\n## 输出约定\n%s') if zh else
                ('ConsoleX: this task has a newer approved method v%s (the Skill holds v%s); follow this one.\n\n## Method\n%s\n\n'
                 '## Output contract\n%s')) % (latest, exported, body.get('instructions') or '', body.get('output_contract') or '')
    if isinstance(body.get('skills'), list):
        note += _sync_task_skills(env, config, skill_dir, token, body['skills'], zh, http)
    note += _task_tools_note(body.get('task_tools'), os.path.basename(os.path.normpath(skill_dir)), env, zh)
    return _context(data, note)


# Tools of the plugin's ConsoleX tool service in an ordinary session (MCP in Claude Code and Codex, native in DSH).
TASK_TOOLS_TOOL = 'consolex_task_tools'
RUN_TASK_TOOL = 'consolex_run_task_tool'


def _tool_label(item, zh):
    if item.get('kind') == 'mcp':
        return ('%s（MCP，%s 个工具）' if zh else '%s (MCP, %s tools)') % (item['name'], item.get('tools_count') or 0)
    return item['name']


def _task_tools_note(info, skill, env, zh):
    """Which ConsoleX tools the task uses and how this session reaches them; '' when it uses none."""
    if not isinstance(info, dict):
        return ''  # an older ConsoleX server
    if info.get('shared_agent'):
        return ('\n\nConsoleX：这个任务用的是共享 Agent，它的工具只在 ConsoleX 里运行，这里调用不了。需要这些工具的步骤，先告诉用户。'
                if zh else "\n\nConsoleX: this task uses a shared Agent; its tools run only in ConsoleX and can't be called "
                'here. Tell the user before a step that needs them.')
    items = [item for item in info.get('items') or [] if isinstance(item, dict) and item.get('name')]
    waiting = [str(name) for name in info.get('waiting') or [] if name]
    note = ''
    if items:
        names = ('、' if zh else ', ').join(_tool_label(item, zh) for item in items)
        if not info.get('allowed'):
            note = (('\n\nConsoleX：这个任务在 ConsoleX 里配置了这些工具：%s。这台电脑还没获准使用任务的工具和文件：请用户在 ConsoleX 的'
                     '「设置 › 编码代理」里为这台电脑勾选「工具和文件」，然后重新开始这个任务。在那之前，需要这些工具的步骤先告诉用户做不了，'
                     '用户同意后再用你自己的工具代替。') if zh else
                    ("\n\nConsoleX: in ConsoleX this task uses these tools: %s. This computer is not yet allowed to use the "
                     "tasks' tools and files: ask the user to tick \"Tools and files\" for it in ConsoleX Settings > Coding "
                     'agents, then start the task again. Until then, tell the user which steps need those tools, and use '
                     'your own tools instead only if the user agrees.')) % names
        elif env.get('CONSOLEX_ISOLATED') == '1':
            note = (('\n\nConsoleX：这个任务的 ConsoleX 工具（%s）已经作为 consolex 的 MCP 工具提供，需要时直接调用；'
                     'ConsoleX 会用负责人的连接执行、计费并记录。') if zh else
                    ("\n\nConsoleX: this task's ConsoleX tools (%s) are available as the consolex MCP tools; call them when a "
                     "step needs them. ConsoleX runs each call with the owner's connections, bills it and records it.")) % names
        else:
            note = (('\n\nConsoleX：这个任务在 ConsoleX 里配置了这些工具：%s。用 ConsoleX 插件提供的 %s 和 %s 调用它们，task 参数填 "%s"：'
                     '先用 %s 查工具名和参数，再用 %s 调用。ConsoleX 会用负责人的连接执行、计费并记录。需要这些工具的步骤不要换成你自己的办法；'
                     '调用失败时把原因告诉用户。看不到这两个工具时，告诉用户重启这个会话（新装或更新 ConsoleX 插件后需要重启）。') if zh else
                    ('\n\nConsoleX: in ConsoleX this task uses these tools: %s. Call them through the ConsoleX plugin\'s %s and '
                     '%s with task "%s": %s gives their names and inputs, then %s runs one. ConsoleX runs each call with the '
                     "owner's connections, bills it and records it. Don't replace these tools with your own for the steps that "
                     'need them; when a call fails, tell the user why. If you cannot see these two tools, tell the user to '
                     'restart this session (needed after the ConsoleX plugin is installed or updated).')) % (
                names, TASK_TOOLS_TOOL, RUN_TASK_TOOL, skill, TASK_TOOLS_TOOL, RUN_TASK_TOOL)
    if waiting:
        note += (('\n\nConsoleX：%s 在 ConsoleX 里还在等密钥或连接，补上之前用不了。' if zh else
                  '\n\nConsoleX: %s still need a key or a connection in ConsoleX and cannot run until then.') %
                 ('、' if zh else ', ').join(waiting))
    return note


def command_text(text, env):
    """`/consolex:login` in the syntax of this runtime (Codex `$consolex-login`, DSH `/consolex login`)."""
    if client(env) == 'codex':
        return re.sub(r'/consolex:([a-z]+)', r'$consolex-\1', text)
    if client(env) == 'deepseek_harness':
        return re.sub(r'/consolex:([a-z]+)', r'/consolex \1', text)
    return text


def _task_ref(env, task, cwd=None):
    """(config, problem): the installed task Skill named `task` (cx-…, also with a leading / or $)."""
    name = str(task or '').strip().lstrip('/$')
    if not SKILL_NAME.fullmatch(name):
        return None, 'Give task as the name of an installed ConsoleX task Skill, like "cx-weekly-note".'
    folder = find_skill_dir(name, env, cwd or os.getcwd())
    if not folder:
        return None, 'The ConsoleX task %s is not installed on this computer; add it with /consolex:add.' % name
    return load_config(folder), ''


def _tool_service_problem(status, body):
    if status == 401:
        return 'ConsoleX is not connected on this computer; run /consolex:login.'
    if status == 403 and body.get('error_type') == 'PERMISSION':
        return ("This computer may not use your tasks' tools and files: tick \"Tools and files\" for it in ConsoleX "
                'Settings > Coding agents.')
    return body.get('error_msg') or ('HTTP %s' % status if status else 'ConsoleX is unreachable')


def task_tools(env, task, http=http_json, cwd=None):
    """(tools, problem): the task's ConsoleX tools with their input schemas, for an ordinary session."""
    config, problem = _task_ref(env, task, cwd)
    if not config:
        return [], problem
    token = _token(config, env)
    if not token:
        return [], 'ConsoleX is not connected on this computer; run /consolex:login.'
    status, body = http('GET', _task_url(config, env, 'tools'), token, timeout=20)
    if status != 200 or not body.get('succ'):
        return [], _tool_service_problem(status, body)
    return [{'name': item['name'], 'title': item.get('title') or '', 'description': item.get('description') or '',
             'input_schema': item.get('input_schema') or {'type': 'object', 'properties': {}}}
            for item in body.get('tools') or [] if isinstance(item, dict) and item.get('name')], ''


def run_task_tool(env, task, tool, arguments, session_id, http=http_json, cwd=None, timeout=330):
    """(text, is_error): one call of the task's ConsoleX tool, run, billed and recorded by ConsoleX."""
    config, problem = _task_ref(env, task, cwd)
    if not config:
        return problem, True
    token = _token(config, env)
    if not token:
        return 'ConsoleX is not connected on this computer; run /consolex:login.', True
    status, body = http('POST', _task_url(config, env, 'tools/call'), token, {
        'session_id': str(session_id or '')[:128] or 'session-' + uuid.uuid4().hex, 'call_id': uuid.uuid4().hex,
        'tool': str(tool or ''), 'arguments': arguments if isinstance(arguments, dict) else {}}, timeout=timeout)
    if status != 200 or not body.get('succ'):
        return 'ConsoleX could not run %s: %s' % (tool, _tool_service_problem(status, body)), True
    return str(body.get('output') or ''), bool(body.get('is_error'))


def _sync_task_skills(env, config, skill_dir, token, skills, zh, http):
    """Bring the ConsoleX Skills of the task being started up to date; a note only when something changed."""
    outcome = install_task_skills(env, _api(config, env), token, config['task_uuid'], skills, http=http)
    names = outcome['installed'] + outcome['updated'] + outcome['current'] + outcome['kept']
    _list_task_skills(skill_dir, names, zh)
    changed = outcome['installed'] + outcome['updated']
    note = ''
    if changed:
        note = (('\n\nConsoleX 刚更新了这个任务的 Skills：%s。需要时加载使用。' if zh else
                 '\n\nConsoleX just updated this task\'s Skills: %s. Load them when a step needs them.') % ', '.join(changed))
    for name, why in outcome['failed']:
        note += ('\n\nConsoleX 没能更新 Skill %s（%s），按已安装的版本做。' if zh else
                 '\n\nConsoleX could not update the Skill %s (%s); use the installed copy.') % (name, why)
    return note


SESSION_STATES = {'start': 'working', 'seen': 'working', 'idle': 'idle', 'ask': 'asking'}


def session_event(config, env, data, marker, event, http=http_json):
    """Tell ConsoleX this session started, is working, finished its turn, asks the owner, or ended on the task,
    so its run list shows it. Best effort: a failure never affects the session."""
    token = _token(config, env)
    if not token or not data.get('session_id'):
        return
    marker['beat_at'] = time.time()
    if event in SESSION_STATES:
        marker['state'] = SESSION_STATES[event]
    try:
        http('POST', _task_url(config, env, 'sessions'), token,
             {'session_id': data.get('session_id'), 'event': event, 'runtime': client(env)}, timeout=SESSION_TIMEOUT)
    except Exception:
        pass


def _session_config(env, data):
    """(marker, config) for a session marked with a current task Skill, else (None, None)."""
    marker = load_marker(env, data.get('session_id'))
    if not marker:
        return None, None
    try:
        config = load_config(marker['skill_dir'])
    except (OSError, ValueError, KeyError):
        return None, None
    return (None, None) if _outdated(config) else (marker, config)


def heartbeat(data, env, http=http_json):
    """A "working" signal for a session marked with a task; unmarked sessions do nothing.

    Throttled while the session is already working; sent at once when it was idle or asking."""
    marker, config = _session_config(env, data)
    if not marker:
        return
    working = marker.get('state', 'working') == 'working'
    if working and time.time() - float(marker.get('beat_at') or 0) < HEARTBEAT_SECONDS:
        return
    session_event(config, env, data, marker, 'seen', http=http)
    save_marker(env, marker)


def session_state(data, env, event, http=http_json):
    """Report that the turn ended (`idle`) or that a question to the owner is open (`ask`); not throttled."""
    marker, config = _session_config(env, data)
    if marker:
        session_event(config, env, data, marker, event, http=http)
        save_marker(env, marker)


def on_session_end(data, env, http=http_json):
    session_state(data, env, 'end', http=http)
    return None


def build_output(config, env, marker, data, delivery):
    """(deliverable text, agent note, problems) for a declared delivery."""
    if delivery['kind'] == 'files':
        text, problems = read_files(delivery['files'], data.get('cwd') or os.getcwd(), env, config,
                                    float(marker.get('started_at') or 0))
        return text, delivery['body'][:4000], problems
    return delivery['body'], '', ([] if delivery['body'].strip() else ['empty deliverable'])


def submit(config, env, marker, data, delivery, output_text, agent_note, *, trigger, attempt, prompt_id, http=http_json):
    """(status, body) of submitting one delivery attempt to ConsoleX."""
    model = data.get('model') or transcript_facts(data.get('transcript_path'))[0]
    payload = {'runtime': client(env), 'session_id': data.get('session_id'), 'prompt_id': prompt_id,
               'attempt': attempt, 'trigger': trigger, 'output_text': output_text,
               'deliverable': {'kind': delivery['kind'], 'files': delivery['files']},
               'confirm_items': delivery['confirm_items'], 'agent_note': agent_note, 'model': model,
               'revision_version': (marker.get('revision') or {}).get('version')}
    tools, notes = tool_evidence(env, data, marker)
    if tools is not None:
        payload['tools'] = tools
        payload['tool_notes'] = notes
    if env.get('CONSOLEX_ISOLATED') == '1':
        payload['isolated'] = True  # Started by `consolex run`: the run record says so.
    if env.get('CONSOLEX_TOOL_SESSION'):
        payload['tool_session_id'] = env['CONSOLEX_TOOL_SESSION']  # where this run's ConsoleX tool calls are recorded
    return http('POST', _task_url(config, env, 'runs'), _token(config, env), payload, timeout=SUBMIT_TIMEOUT)


def on_stop(data, env, http=http_json):
    marker = load_marker(env, data.get('session_id'))
    if not marker:
        return None
    try:
        config = load_config(marker['skill_dir'])
    except (OSError, ValueError, KeyError):
        return None
    message = data.get('last_assistant_message')
    if not isinstance(message, str) or not message.strip():
        message = data.get('last_assistant_message') or transcript_facts(data.get('transcript_path'))[1]
    delivery = parse_delivery(message)
    if not delivery:
        return None  # Asking a question or reporting progress, not delivering.
    if _outdated(config):
        return {'systemMessage': 'ConsoleX: this task Skill is outdated; run /consolex:add to update it. Not checked.'}
    zh = _zh(config)
    prompt_id = str(data.get('prompt_id') or 'turn')
    chains = marker.setdefault('chains', {})
    chain = chains.get(prompt_id)
    if data.get('stop_hook_active'):
        if not chain or not chain.get('reported_at'):
            return None  # Continuing for another hook's reason; never start a loop from here.
        if not (data.get('owner_answered') or owner_answered_since(data.get('transcript_path'), float(chain['reported_at']))):
            # Claude delivered again right after reporting a result, without the owner's answer.
            return {'systemMessage': ('ConsoleX：没等你确认就重新交付了，这次没有检查；要检查就运行 /consolex:check。' if zh else
                                      'ConsoleX: delivered again without your answer, so this was not checked; '
                                      'run /consolex:check to check it.')}
    chain = chain or {}
    if not _token(config, env):
        return {'systemMessage': ('ConsoleX：这台电脑还没有连接（运行 /consolex:login），这次交付没有检查。' if zh else
                                  'ConsoleX: not connected on this computer (run /consolex:login); this delivery was not checked.')}
    # Attempts are numbered per session, so the run history reads 1, 2, 3 across the owner's go-aheads.
    attempt = int(marker.get('attempts') or 0) + 1
    output_text, agent_note, problems = build_output(config, env, marker, data, delivery)
    if problems:
        # The declared deliverable cannot be read: nothing to check, so no run; the owner decides what next.
        summary = (('ConsoleX 读不到这次声明的交付，所以没有检查：\n- %s\n（交付文件要写相对当前目录的路径，并且是本次任务里改过的文本文件。）' if zh else
                    'ConsoleX could not read the declared deliverable, so it was not checked:\n- %s\n(Deliverable files must be '
                    'paths relative to the current folder, to text files changed in this task.)') % '\n- '.join(problems))
        message = ('ConsoleX：交付读不到，没有检查：%s；等你决定要不要修改' if zh else
                   'ConsoleX: deliverable unreadable, not checked: %s; waiting for your decision') % '; '.join(problems)
        return _report(env, marker, chain, prompt_id, report_reason(config, summary, '', delivery['confirm_items']), message)
    status, result = submit(config, env, marker, data, delivery, output_text, agent_note,
                            trigger='stop', attempt=attempt, prompt_id=prompt_id, http=http)
    marker['attempts'] = attempt
    chain['attempt'] = attempt
    chains[prompt_id] = chain
    if status != 200 or not (result or {}).get('succ'):
        why = (result or {}).get('error_msg') or ('HTTP %s' % status if status else 'unreachable')
        # No local fallback: the standard is only on the server, so an unreachable ConsoleX means not checked.
        message = ('ConsoleX：这次交付没有检查（%s）。' if zh else 'ConsoleX: this delivery was not checked (%s).') % why
        return _finish(env, marker, chain, prompt_id, config, message, delivery['confirm_items'])
    link = _run_link(config, env, result)
    if result.get('verdict') in ('failed', 'needs_review'):
        summary = result_summary(config, result, attempt)
        return _report(env, marker, chain, prompt_id, report_reason(config, summary, link, delivery['confirm_items']),
                       user_message(config, result, attempt, link))
    return _finish(env, marker, chain, prompt_id, config, user_message(config, result, attempt, link), delivery['confirm_items'])


def _finish(env, marker, chain, prompt_id, config, message, confirm_items):
    """End the turn with the result line, or, when the delivery lists items to confirm, block once so Claude asks the owner."""
    if confirm_items:
        return _report(env, marker, chain, prompt_id, confirm_reason(config, message, confirm_items), message)
    save_marker(env, marker)
    return {'systemMessage': message}


def _report(env, marker, chain, prompt_id, reason, message):
    """Block once so Claude reports the result and asks the owner; it never revises on its own."""
    chain['reported_at'] = time.time()
    marker.setdefault('chains', {})[prompt_id] = chain
    save_marker(env, marker)
    return {'decision': 'block', 'reason': reason, 'systemMessage': message}


def on_check_command(data, env, argument, http=http_json):
    """`/consolex:check [off]`: check the latest reply now, or stop checking this session."""
    marker = load_marker(env, data.get('session_id'))
    if argument.strip().lower() == 'off':
        if marker:
            on_session_end(data, env, http=http)
        clear_marker(env, data.get('session_id'))
        return _context(data, 'ConsoleX checks are off for this session. Tell the user in one line.')
    if not marker:
        return _context(data, 'No ConsoleX task is active in this session: tell the user to start one with its /cx-… '
                              'command (or /consolex:add to install it) first. Do not run anything.')
    config = load_config(marker['skill_dir'])
    if _outdated(config):
        return _context(data, _outdated_note(config))
    if not _token(config, env):
        return _not_connected(data, _api(config, env))
    message = data.get('last_assistant_message') or transcript_facts(data.get('transcript_path'))[1]
    delivery = parse_delivery(message) or {'kind': 'reply', 'files': [], 'body': (message or '').strip(), 'confirm_items': []}
    if not delivery['body'] and delivery['kind'] == 'reply':
        return _context(data, 'There is no reply to check yet. Tell the user in one line.')
    output_text, agent_note, problems = build_output(config, env, marker, data, delivery)
    if problems:
        return _context(data, 'ConsoleX could not read the deliverable: ' + '; '.join(problems) + '. Tell the user.')
    attempt = int(marker.get('attempts') or 0) + 1
    status, result = submit(config, env, marker, data, delivery, output_text, agent_note, trigger='manual', attempt=attempt,
                            prompt_id='check-' + str(data.get('prompt_id') or int(time.time())), http=http)
    marker['attempts'] = attempt
    save_marker(env, marker)
    if status != 200 or not (result or {}).get('succ'):
        return _context(data, 'ConsoleX could not check the delivery (%s). Tell the user.' % (
            (result or {}).get('error_msg') or 'HTTP %s' % status))
    summary = {key: result.get(key) for key in ('verdict', 'failed', 'needs_review', 'confirm_items', 'judge')}
    summary['run'] = _run_link(config, env, result)
    return _context(data, 'ConsoleX check result (manual): ' + json.dumps(summary, ensure_ascii=False))


# ---------------------------------------------------------------- saving a session as a task

MAX_SAVED_MESSAGES = 100
MAX_SAVED_MESSAGE_CHARS = 40_000
MAX_SAVED_REPLY_CHARS = 400_000
SAVE_TIMEOUT = 30
# Text Claude Code itself puts in user entries: slash commands, their output, shell escapes and reminders.
_GENERATED_USER_TEXT = re.compile(r'^\s*<(?:command-name|command-message|command-args|local-command-stdout|'
                                  r'local-command-stderr|bash-input|bash-stdout|bash-stderr|system-reminder)>')
_COMMAND_ARGS = re.compile(r'<command-name>/?([^<]+)</command-name>.*?<command-args>(.*?)</command-args>', re.S)


def session_messages(transcript_path):
    """The owner's own messages in this session's main conversation, oldest first, keys masked.

    Tool results, slash commands and their output, meta entries and subagent steps are left out, except
    the request typed after a Skill or custom command (`/cx-weekly-note write this week's note`): Claude
    Code expands such a command into a meta prompt, while built-in commands print their own output."""
    texts, command = [], None
    if not transcript_path or not os.path.isfile(transcript_path):
        return texts
    try:
        with open(transcript_path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get('type') != 'user' or entry.get('isSidechain'):
                    continue
                content = (entry.get('message') or {}).get('content')
                if isinstance(content, list):
                    if any(isinstance(part, dict) and part.get('type') == 'tool_result' for part in content):
                        continue
                    content = '\n'.join(part.get('text') or '' for part in content
                                        if isinstance(part, dict) and part.get('type') == 'text')
                text = content.strip() if isinstance(content, str) else ''
                if entry.get('isMeta'):
                    if command and text and not text.startswith('Caveat:'):
                        texts.append(_clip(redact(command), MAX_SAVED_MESSAGE_CHARS)[0])
                    command = None
                    continue
                command = None
                invoked = _COMMAND_ARGS.search(text) if text.startswith('<command-') else None
                if invoked and invoked[2].strip() and not invoked[1].strip().startswith('consolex:'):
                    command = '/%s %s' % (invoked[1].strip(), invoked[2].strip())
                elif text and not _GENERATED_USER_TEXT.match(text):
                    texts.append(_clip(redact(text), MAX_SAVED_MESSAGE_CHARS)[0])
    except OSError:
        return []
    return texts[-MAX_SAVED_MESSAGES:]


def session_reply(transcript_path):
    """(model, text) of the latest reply that answers the owner, for `/consolex:save`.

    Not the reply that relays a ConsoleX command's result, nor one ConsoleX asked for through Stop hook
    feedback to report a check: right after a check or a first save, the session's result is the work."""
    model, reply, answering = None, '', True
    if not transcript_path or not os.path.isfile(transcript_path):
        return model, reply
    try:
        with open(transcript_path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get('isSidechain'):
                    continue
                content = (entry.get('message') or {}).get('content')
                if entry.get('type') == 'user':
                    if isinstance(content, list):
                        if any(isinstance(part, dict) and part.get('type') == 'tool_result' for part in content):
                            continue
                        content = '\n'.join(part.get('text') or '' for part in content
                                            if isinstance(part, dict) and part.get('type') == 'text')
                    text = content.strip() if isinstance(content, str) else ''
                    if entry.get('isMeta'):
                        if text.startswith('Stop hook feedback') and 'ConsoleX' in text:
                            answering = False
                    elif text:
                        invoked = _COMMAND_ARGS.search(text) if text.startswith('<command-') else None
                        answering = not (invoked and invoked[1].strip().startswith('consolex:'))
                elif entry.get('type') == 'assistant' and answering and isinstance(content, list):
                    texts = [part.get('text') for part in content
                             if isinstance(part, dict) and part.get('type') == 'text' and part.get('text')]
                    if texts:
                        reply, model = '\n'.join(texts), (entry.get('message') or {}).get('model') or model
    except OSError:
        return None, ''
    return model, reply


def on_save(data, env, argument, http=http_json):
    """`/consolex:save [name]`: send this session to ConsoleX as a task draft; the owner reviews and saves it there.

    Sent: the owner's messages, the final reply, the model id and the tool records since the session started
    (keys masked, at most 80 records). Nothing is saved as a task until the owner saves it in ConsoleX.
    Claude Code's come from its transcript; another runtime passes them in `data` (`session_messages`,
    `final_reply`, `model`, `tools`), collected from its own events, because its log is not Claude's."""
    api_base = default_api_base(env)
    token, connection = connection_for(env, api_base)
    if not token:
        return _not_connected(data, api_base)
    if 'session_messages' in data:
        requests, model, reply = list(data['session_messages']), data.get('model'), data.get('final_reply') or ''
    else:
        transcript = data.get('transcript_path')
        requests = session_messages(transcript)
        model, reply = session_reply(transcript)
    if not requests or not (reply or '').strip():
        return _context(data, 'There is nothing to save yet: this session has no request with a reply. Tell the user in '
                              'one line. Do not run anything.')
    reply, clipped = _clip(redact(reply.strip()), MAX_SAVED_REPLY_CHARS)
    tools, notes = tool_evidence(env, data, {'started_at': 0})
    payload = {'request_id': str(uuid.uuid4()), 'session_id': data.get('session_id'), 'client': client(env),
               'name': argument.strip()[:255], 'user_messages': requests, 'final_reply': reply, 'model': model,
               'tools': tools, 'tool_notes': {key: value for key, value in notes.items() if key != 'off'}}
    status, body = http('POST', api_base + '/api/operator/external/drafts', token, payload, timeout=SAVE_TIMEOUT)
    if status == 401:
        return _not_connected(data, api_base)
    if status == 206:
        return _context(data, 'Saving sessions as ConsoleX tasks needs a paid plan. Tell the user in one line.')
    if status != 200 or not body.get('succ'):
        return _context(data, 'ConsoleX could not save this session (%s). Tell the user in one line.' % (
            body.get('error_msg') or 'HTTP %s' % status))
    link = body.get('review_url') or ((connection.get('app_base') or env.get('CONSOLEX_APP_BASE') or '').rstrip('/')
                                      + (body.get('review_path') or ''))
    if tools is None:
        sent_tools = 'no tool records (turned off with CONSOLEX_TOOL_EVIDENCE)' if notes.get('off') else 'no tool records'
    else:
        sent_tools = '%s tool call(s)%s' % (len(tools), ' (%s older ones left out)' % notes['dropped'] if notes.get('dropped') else '')
    organize_error = (body.get('draft') or {}).get('organize_error')
    return _context(data, (
        'Sent this session to ConsoleX as a task draft: %s of your message(s), the final reply (%s characters%s) and %s, '
        'with keys masked. Review it in ConsoleX: %s . There you edit the method and approve the checks; when you save, '
        'this result becomes the task\'s first run and is checked against them.%s Tell the user exactly this in two or '
        'three lines. Do not run anything.') % (
        len(requests), len(reply), ', shortened' if clipped else '', sent_tools, link,
        (' AI organizing was not available (%s), so the draft starts from your latest message.' % organize_error)
        if organize_error else ''))


# ---------------------------------------------------------------- isolated runs
#
# `consolex run <task>` (Codex: `consolex-codex run`, DeepSeek Harness: `consolex-dsh run`) starts a clean agent
# session that has only what the task has in ConsoleX: the task Skill and its ConsoleX Skills, its files, and its
# ConsoleX tools (executed and billed by ConsoleX). It loads none of the owner's own Skills, plugins, MCP servers,
# hooks, instructions or memory. A run happens in a session started with these options, so it is a terminal
# command, not a command inside a session; the run command inside the agent prints it.

ISOLATED_TOOLS = 'Skill,Read,Write,Edit,Glob,Grep'
ISOLATED_ENV = {'CLAUDE_CODE_DISABLE_BUNDLED_SKILLS': '1', 'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1',
                'CLAUDE_CODE_DISABLE_CLAUDE_MDS': '1', 'ENABLE_CLAUDEAI_MCP_SERVERS': 'false', 'CONSOLEX_ISOLATED': '1'}
MAX_TASK_FILE_COUNT = 50
FILES_TIMEOUT = 120


def runs_dir(env):
    return env.get('CONSOLEX_RUNS_DIR') or os.path.join(os.path.expanduser('~'), 'ConsoleX')


LAUNCHERS = {'claude_code': 'consolex', 'codex': 'consolex-codex', 'deepseek_harness': 'consolex-dsh'}
RUNTIME_LABELS = {'claude_code': 'Claude Code', 'codex': 'Codex', 'deepseek_harness': 'DeepSeek Harness'}


def scripts_dir():
    """The folder of this client in an installed package (the plugin's or the integration's scripts/)."""
    return os.path.dirname(os.path.abspath(__file__))


def plugin_root():
    """The Claude Code plugin folder this script runs from (scripts/ is inside it)."""
    return os.path.dirname(scripts_dir())


def launcher_path(env):
    return os.path.join(consolex_home(env), 'bin', LAUNCHERS.get(client(env), 'consolex'))


def write_launcher(env):
    """`~/.consolex/bin/consolex` (or `consolex-codex`, `consolex-dsh`): runs this installed client, rewritten at
    each session start so it follows package updates."""
    if env.get('CONSOLEX_ISOLATED') == '1':
        return None  # An isolated session runs from a copy; the launcher points at the owner's own install.
    path = launcher_path(env)
    entry = 'consolex.py' if client(env) == 'claude_code' else 'consolex_core.py'
    script = os.path.join(scripts_dir(), entry)
    if not os.path.isfile(script):
        return None
    body = ('#!/bin/sh\n# Written by the ConsoleX %s integration; points at its current version.\n'
            'CONSOLEX_RUNTIME=%s exec %s %s "$@"\n') % (RUNTIME_LABELS.get(client(env), 'agent'), client(env),
                                                     _sh(sys.executable or 'python3'), _sh(script))
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        if _read_text(path) != body:
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(body)
            os.chmod(path, 0o755)
    except OSError:
        return None
    return path


def _sh(value):
    return "'%s'" % str(value).replace("'", "'\\''")


def _read_text(path):
    try:
        with open(path, encoding='utf-8') as handle:
            return handle.read()
    except OSError:
        return None


def _find_one_task(env, api_base, token, words, http):
    status, body = _list_tasks(env, api_base, token, words.strip(), http)
    if status == 401:
        raise ValueError('ConsoleX is not connected on this computer (%s); run /consolex:login in Claude Code first.' % api_base)
    if status != 200:
        raise ValueError('ConsoleX could not list tasks (%s).' % (body.get('error_msg') or 'HTTP %s' % status))
    tasks = body.get('tasks') or []
    if not tasks:
        raise ValueError('No ConsoleX task matches "%s".' % words)
    if len(tasks) > 1:
        raise ValueError('Several ConsoleX tasks match "%s":\n%s\nAdd more words of the name.' % (
            words, '\n'.join('- ' + item['name'] for item in tasks[:10])))
    return tasks[0]


MAX_TASK_FILE_BYTES = 50_000_000


def _fetch_bytes(url, limit=MAX_TASK_FILE_BYTES, timeout=FILES_TIMEOUT):
    """GET a short-lived download link; ValueError past `limit` bytes."""
    request = urllib.request.Request(url, headers={'User-Agent': 'consolex-coding-agents/1.0'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('larger than %s MB' % (limit // 1_000_000))
    return data


def download_task_files(env, api_base, token, task_uuid, target, http=http_json, fetch=_fetch_bytes):
    """The task's own files (its attachments in ConsoleX) into `target`; (names, problem).

    ConsoleX answers short-lived download links for the files the owner uploaded; they are fetched directly."""
    status, body = http('GET', '%s/api/operator/external/tasks/%s/files' % (api_base, task_uuid), token, timeout=QUICK_TIMEOUT)
    if status != 200 or not body.get('succ'):
        return [], body.get('error_msg') or 'HTTP %s' % status
    names, problems = [], []
    for item in (body.get('files') or [])[:MAX_TASK_FILE_COUNT]:
        name = re.sub(r'[\x00-\x1f]', '_', os.path.basename(str(item.get('name') or item.get('key') or 'file').replace('\\', '/')))[:180]
        if name in ('', '.', '..'):
            name = str(item.get('key') or 'file')
        try:
            data = fetch(item['url'])
        except (KeyError, ValueError, OSError) as exc:
            problems.append('%s (%s)' % (name, str(exc)[:120]))
            continue
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, name), 'wb') as handle:
            handle.write(data)
        names.append(name)
    if body.get('skipped'):
        problems.append('%s file(s) that are not your uploads were left out' % body['skipped'])
    return names, '; '.join(problems)


def prepare_isolated_run(env, words, http=http_json, fetch=_fetch_bytes):
    """Set up the run folder of one task and return what `launch_isolated` needs."""
    api_base = default_api_base(env)
    token, _ = connection_for(env, api_base)
    if not token:
        raise ValueError('ConsoleX is not connected on this computer (%s); run /consolex:login in Claude Code first.' % api_base)
    task = _find_one_task(env, api_base, token, words, http)
    status, bundle = http('GET', api_base + '/api/operator/external/tasks/%s/skill' % task['uuid'], token, timeout=QUICK_TIMEOUT)
    if status != 200 or not bundle.get('succ'):
        raise ValueError('ConsoleX could not export "%s" (%s).' % (task['name'], bundle.get('error_msg') or 'HTTP %s' % status))
    folder = bundle['skill']
    runtime = client(env)
    root = os.path.join(runs_dir(env), folder if runtime == 'claude_code' else '%s-%s' % (folder, LAUNCHERS[runtime].split('-')[-1]))
    skills = os.path.join(root, '.claude' if runtime == 'claude_code' else '.agents', 'skills')
    run_env = {**env, 'CONSOLEX_SKILLS_DIR': skills}
    os.makedirs(skills, exist_ok=True)
    _, target = install_skill(run_env, bundle)
    outcome = install_task_skills(run_env, api_base, token, task['uuid'], bundle.get('skills') or [], http=http)
    names = outcome['installed'] + outcome['updated'] + outcome['current'] + outcome['kept']
    _list_task_skills(target, names, json.loads(bundle['files']['consolex.json']).get('language') == 'zh')
    files, files_problem = download_task_files(env, api_base, token, task['uuid'], os.path.join(root, 'files'), http=http, fetch=fetch)
    status, listed = http('GET', api_base + '/api/operator/external/tasks/%s/tools' % task['uuid'], token, timeout=QUICK_TIMEOUT)
    tools = [item['name'] for item in (listed.get('tools') or [])] if status == 200 and listed.get('succ') else []
    tools_problem = '' if status == 200 else (listed.get('error_msg') or 'HTTP %s' % status)
    # One id for this run's ConsoleX tool calls: the MCP server records them under it and the delivery names it, so
    # the check finds them whatever session id the agent itself uses.
    prepared = {'runtime': runtime, 'task': task, 'root': root, 'skill': folder, 'skills': names, 'files': files,
                'tools': tools, 'skills_dir': skills, 'api_base': api_base, 'tool_session': 'run-' + uuid.uuid4().hex,
                'problems': [p for p in (
                    files_problem and 'task files: ' + files_problem, tools_problem and 'ConsoleX tools: ' + tools_problem,
                    *('Skill %s: %s' % item for item in outcome['failed'])) if p]}
    ISOLATION[runtime]['prepare'](env, prepared)
    return prepared


def _mcp_server(env, prepared):
    """The local MCP server of this task: command, args and env (it reads the shared device token itself)."""
    return {'command': sys.executable or 'python3', 'args': [os.path.join(scripts_dir(), 'consolex_mcp.py')],
            'env': {'CONSOLEX_TASK_UUID': prepared['task']['uuid'], 'CONSOLEX_API_BASE': prepared['api_base'],
                    'CONSOLEX_HOME': consolex_home(env), 'CONSOLEX_SESSION_ID': prepared['tool_session']}}


def _claude_prepare(env, prepared):
    # Only this folder's settings load: allow the task's ConsoleX tools without a prompt each time, and nothing else.
    root = prepared['root']
    _write_private(os.path.join(root, '.claude', 'settings.json'), {'permissions': {'allow': ['mcp__consolex']},
                                                                    'autoMemoryEnabled': False})
    prepared['mcp_config'] = os.path.join(root, '.claude', 'consolex-mcp.json')
    _write_private(prepared['mcp_config'], {'mcpServers': {'consolex': {'type': 'stdio', **_mcp_server(env, prepared)}}})


def isolated_command(prepared, request='', headless=False):
    """(argv, env additions) of the agent session for an isolated run."""
    return ISOLATION[prepared['runtime']]['command'](prepared, request, headless)


def _claude_command(prepared, request='', headless=False):
    prompt = ('/%s %s' % (prepared['skill'], request)).strip()
    # The tools are allowed on the command line: Claude Code ignores a folder's own `permissions.allow` until the
    # folder is trusted, and a new run folder never is.
    argv = ['claude', '--setting-sources', 'project', '--strict-mcp-config', '--mcp-config', prepared['mcp_config'],
            '--tools', ISOLATED_TOOLS, '--allowedTools=mcp__consolex', '--plugin-dir', plugin_root()]
    if headless:
        argv += ['-p', '--output-format', 'json']
    argv.append(prompt)
    return argv, {**ISOLATED_ENV, 'CONSOLEX_SKILLS_DIR': os.path.join(prepared['root'], '.claude', 'skills'),
                  'CONSOLEX_API_BASE': prepared['api_base'], 'CONSOLEX_TOOL_SESSION': prepared['tool_session']}


DSH_PROFILE = 'consolex-isolated'
# DeepSeek Harness rows switched off in an isolated run, so it has what a Claude Code isolated run has: files,
# Skills and the task's ConsoleX tools; no shell, web, subagents, workflows or goal/todo loops.
DSH_DISABLED_ROWS = ('agent-instructions', 'tool-bash', 'tool-pwsh', 'tool-jobs', 'tool-web', 'tool-goal', 'tool-todo',
                     'tool-ralph', 'tool-workflow', 'tool-subagent', 'tool-subagent-fork', 'tool-subagent-control',
                     'tool-subagent-list-agents', 'tool-str-replace-editor')


def _dsh_package():
    """The installed ConsoleX DeepSeek Harness bundle (index.mjs beside scripts/)."""
    return os.environ.get('CONSOLEX_DSH_PACKAGE') or os.path.dirname(scripts_dir())


def _dsh_prepare(env, prepared):
    """A dedicated DSH home with one profile (base + headless + this plugin) and a per-run patch.

    The separate home keeps the owner's DSH skills, AGENTS.md, settings and sessions out; an empty agents home keeps
    `~/.agents/skills` out; the patch limits Skills to the run folder and mounts the ConsoleX tools over MCP."""
    import shutil as _shutil
    home = env.get('CONSOLEX_DSH_HOME') or os.path.join(consolex_home(env), 'dsh-home')
    profile = os.path.join(home, 'profiles', DSH_PROFILE)
    package = _dsh_package()
    if not os.path.isfile(os.path.join(package, 'index.mjs')):
        raise ValueError('The ConsoleX DeepSeek Harness bundle was not found next to this client (%s).' % package)
    target = os.path.join(profile, 'node_modules', 'consolex-dsh-plugin')
    if os.path.realpath(package) != os.path.realpath(target):  # never delete the copy this client runs from
        _shutil.rmtree(target, ignore_errors=True)
        _shutil.copytree(package, target, ignore=_shutil.ignore_patterns('node_modules', '*.test.mjs', '__pycache__'))
    _write_private(os.path.join(profile, 'package.json'), {
        'name': 'dsh-profile-' + DSH_PROFILE, 'private': True, 'dependencies': {},
        'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless', 'consolex-dsh-plugin']}}})
    with open(os.path.join(profile, 'cordis.patch.yml'), 'w', encoding='utf-8') as handle:
        handle.write('[]\n')
    agents_home = os.path.join(home, 'empty-agents-home')
    os.makedirs(agents_home, exist_ok=True)
    server = _mcp_server(env, prepared)
    rows = [{'id': 'consolex', 'config': {'python': server['command'], 'apiBase': prepared['api_base'], 'home': consolex_home(env)}},
            {'id': 'skill-filesystem', 'config': {'includeDefaultRoots': False, 'customSkillDirs': [prepared['skills_dir']]}}]
    rows += [{'id': row, 'disabled': True} for row in DSH_DISABLED_ROWS]
    rows.append({'insert': [{'id': 'consolex-tools', 'name': '@deepseek-ai/dsh-mcp-client', 'config': {
        'serverName': 'consolex', 'transport': 'stdio', 'command': server['command'], 'args': server['args'],
        'cwd': prepared['root'], 'env': server['env']}}]})
    prepared['dsh_patch'] = os.path.join(prepared['root'], 'consolex-dsh.yml')
    with open(prepared['dsh_patch'], 'w', encoding='utf-8') as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=1)  # JSON is YAML.
    prepared.update(dsh_home=home, dsh_agents_home=agents_home)


def _dsh_command(prepared, request='', headless=True):
    """DeepSeek Harness runs one-shot here (its interactive surface is the web app)."""
    prompt = ('/%s %s' % (prepared['skill'], request)).strip()
    argv = ['dsh', '--profile', DSH_PROFILE, '--patch', prepared['dsh_patch'], prompt]
    return argv, {'DSH_HOME': prepared['dsh_home'], 'DSH_AGENTS_HOME': prepared['dsh_agents_home'],
                  'DSH_TELEMETRY_DISABLED': '1', 'CONSOLEX_ISOLATED': '1', 'CONSOLEX_RUNTIME': 'deepseek_harness',
                  'CONSOLEX_SKILLS_DIR': prepared['skills_dir'], 'CONSOLEX_API_BASE': prepared['api_base'],
                  'CONSOLEX_TOOL_SESSION': prepared['tool_session']}


CODEX_HOOK_OWNER = 'ConsoleX delivery checks'
_CODEX_HOOK_LABELS = {'PreToolUse': 'pre_tool_use', 'PermissionRequest': 'permission_request', 'PostToolUse': 'post_tool_use',
                      'PreCompact': 'pre_compact', 'PostCompact': 'post_compact', 'SessionStart': 'session_start',
                      'SessionEnd': 'session_end', 'UserPromptSubmit': 'user_prompt_submit', 'SubagentStart': 'subagent_start',
                      'SubagentStop': 'subagent_stop', 'Stop': 'stop', 'Interrupt': 'interrupt'}
_CODEX_NO_MATCHER = {'UserPromptSubmit', 'Stop', 'Interrupt'}
CODEX_FEATURES_OFF = ('apps', 'plugins', 'memories', 'goals', 'image_generation', 'tool_suggest', 'skill_mcp_dependency_install',
                      'browser_use', 'computer_use', 'in_app_browser', 'view_image', 'multi_agent')


def codex_hook_trust(hooks_path, hooks):
    """{trust key: hash} that marks each ConsoleX hook trusted, as Codex 0.162.1 computes it.

    The hash covers the event, matcher and handler (not the file path); the key is the real path of hooks.json with
    the event, group and handler index. Written into the run's own config.toml, so no trust check is bypassed."""
    import hashlib as _hashlib
    trust = {}
    for event, groups in hooks.items():
        for group_index, group in enumerate(groups):
            for handler_index, handler in enumerate(group.get('hooks', [])):
                timeout = handler.get('timeout')
                timeout = max(1, min(3, timeout or 1)) if event in ('SessionEnd', 'Interrupt') else max(1, timeout or 600)
                normalized = {'type': 'command', 'command': handler['command'], 'timeout': timeout, 'async': bool(handler.get('async'))}
                if handler.get('statusMessage') is not None:
                    normalized['statusMessage'] = handler['statusMessage']
                identity = {'event_name': _CODEX_HOOK_LABELS[event], 'hooks': [normalized]}
                if event not in _CODEX_NO_MATCHER and group.get('matcher') is not None:
                    identity['matcher'] = group['matcher']
                encoded = json.dumps(identity, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
                trust['%s:%s:%s:%s' % (hooks_path, _CODEX_HOOK_LABELS[event], group_index, handler_index)] = (
                    'sha256:' + _hashlib.sha256(encoded.encode('utf-8')).hexdigest())
    return trust


def _toml(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return '[%s]' % ', '.join(_toml(item) for item in value)
    if isinstance(value, dict):
        return '{ %s }' % ', '.join('%s = %s' % (json.dumps(str(key)), _toml(item)) for key, item in value.items())
    return json.dumps(str(value))  # a TOML basic string


def _codex_prepare(env, prepared):
    """A Codex home for this run: the owner's sign-in linked (never copied: refresh tokens are single-use), ConsoleX
    hooks trusted in its own config, only the ConsoleX MCP server, and no user skills, plugins, memories, AGENTS.md,
    apps or web search. A fake empty HOME keeps `~/.agents/skills` out."""
    import shlex as _shlex
    base = os.path.join(consolex_home(env), 'codex-isolated', os.path.basename(prepared['root']))
    codex_home, fake_home = os.path.join(base, 'codex'), os.path.join(base, 'home')
    os.makedirs(codex_home, exist_ok=True)
    os.makedirs(fake_home, exist_ok=True)
    codex_home = os.path.realpath(codex_home)
    signed_in = os.path.join(env.get('CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex'), 'auth.json')
    if not os.path.isfile(signed_in):
        raise ValueError('Codex is not signed in on this computer (%s); sign in to Codex first.' % signed_in)
    link = os.path.join(codex_home, 'auth.json')
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(os.path.realpath(signed_in), link)
    package = os.environ.get('CONSOLEX_CODEX_PACKAGE') or os.path.dirname(scripts_dir())
    template = os.path.join(package, 'hooks', 'hooks.json')
    hooks = _read_json(template, {}).get('hooks') or {}
    if not hooks:
        raise ValueError('The ConsoleX Codex hooks were not found next to this client (%s).' % template)
    command = '%s %s' % (_shlex.quote(sys.executable or 'python3'), _shlex.quote(os.path.join(package, 'scripts', 'consolex.py')))
    for groups in hooks.values():
        for group in groups:
            for handler in group.get('hooks', []):
                handler['command'], handler['statusMessage'] = command, CODEX_HOOK_OWNER
    hooks_path = os.path.join(codex_home, 'hooks.json')
    _write_private(hooks_path, {'hooks': hooks})
    root, server = os.path.realpath(prepared['root']), _mcp_server(env, prepared)
    lines = ['# Written by ConsoleX for one isolated run; replaced on the next run.',
             'project_doc_max_bytes = 0', 'project_root_markers = []', 'web_search = "disabled"',
             'check_for_update_on_startup = false']
    if env.get('CONSOLEX_CODEX_MODEL'):
        lines.append('model = %s' % _toml(env['CONSOLEX_CODEX_MODEL']))
    lines += ['', '[history]', 'persistence = "none"', '', '[skills.bundled]', 'enabled = false', '', '[features]']
    lines += ['%s = false' % name for name in CODEX_FEATURES_OFF]
    lines += ['', '[memories]', 'use_memories = false', 'generate_memories = false', '',
              '[shell_environment_policy]', 'exclude = ["CONSOLEX_*"]', '',
              '[projects.%s]' % _toml(root), 'trust_level = "trusted"', '',
              '[mcp_servers.consolex]', 'command = %s' % _toml(server['command']), 'args = %s' % _toml(server['args']),
              'cwd = %s' % _toml(root), 'env = %s' % _toml(server['env']), 'env_vars = ["CONSOLEX_TOKEN"]', 'required = true',
              'startup_timeout_sec = 20', 'tool_timeout_sec = 330', 'default_tools_approval_mode = "approve"',
              'omit_tools_from = ["deferred"]']
    for key, digest in codex_hook_trust(hooks_path, hooks).items():
        lines += ['', '[hooks.state.%s]' % _toml(key), 'trusted_hash = %s' % _toml(digest)]
    with open(os.path.join(codex_home, 'config.toml'), 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')
    prepared.update(codex_home=codex_home, fake_home=fake_home, consolex_home=consolex_home(env))


def _codex_command(prepared, request='', headless=False):
    prompt = ('$%s %s' % (prepared['skill'], request)).strip()
    common = ['--sandbox', 'workspace-write', '-C', os.path.realpath(prepared['root'])]
    argv = (['codex', 'exec', '--skip-git-repo-check', *common, '--json', prompt] if headless
            else ['codex', '--no-daemon', *common, prompt])
    return argv, {'CODEX_HOME': prepared['codex_home'], 'HOME': prepared['fake_home'], 'CODEX_API_KEY': None,
                  'CONSOLEX_HOME': prepared['consolex_home'], 'CONSOLEX_ISOLATED': '1', 'CONSOLEX_RUNTIME': 'codex',
                  'CONSOLEX_SKILLS_DIR': prepared['skills_dir'], 'CONSOLEX_API_BASE': prepared['api_base'],
                  'CONSOLEX_TOOL_SESSION': prepared['tool_session']}


def _not_yet(*_args, **_kwargs):
    raise ValueError('Isolated runs are not available for this agent yet.')


ISOLATION = {'claude_code': {'prepare': _claude_prepare, 'command': _claude_command},
             'codex': {'prepare': _codex_prepare, 'command': _codex_command},
             'deepseek_harness': {'prepare': _dsh_prepare, 'command': _dsh_command, 'one_shot': True}}


def run_main(argv, env=None, http=http_json, exec_=os.execvpe, out=sys.stdout, fetch=_fetch_bytes):
    """`consolex run [--headless] <task words> [-- request]`: start an isolated run of one task."""
    env = dict(os.environ) if env is None else env
    headless = '--headless' in argv
    argv = [item for item in argv if item != '--headless']
    words, _, request = ' '.join(argv).partition(' -- ')
    if not words.strip():
        out.write('Usage: %s run [--headless] <words from the task name> [-- what you want this time]\n' % LAUNCHERS.get(client(env), 'consolex'))
        return 2
    try:
        prepared = prepare_isolated_run(env, words, http=http, fetch=fetch)
    except ValueError as exc:
        out.write('ConsoleX: %s\n' % exc)
        return 1
    out.write('ConsoleX isolated run of "%s" in %s\n  Skills: %s\n  ConsoleX tools: %s\n  Files: %s\n%s' % (
        prepared['task']['name'], prepared['root'], ', '.join(prepared['skills']) or 'none',
        ', '.join(prepared['tools']) or 'none', ', '.join(prepared['files']) or 'none',
        ''.join('  Not available: %s\n' % problem for problem in prepared['problems'])))
    out.flush()
    headless = headless or ISOLATION[prepared['runtime']].get('one_shot', False)
    command, extra = isolated_command(prepared, request.strip(), headless)
    run_env = {key: value for key, value in {**env, **extra}.items() if value is not None}  # None unsets
    os.chdir(prepared['root'])
    if headless:
        return subprocess.run(command, env=run_env, stdin=subprocess.DEVNULL).returncode
    exec_(command[0], command, run_env)
    return 0


def on_run_command(data, env, argument, http=http_json):
    """`/consolex:run <task>`: the terminal command that starts an isolated run (it needs a fresh session)."""
    launcher = write_launcher(env) or launcher_path(env)
    words = argument.strip() or '<words from the task name>'
    label = RUNTIME_LABELS.get(client(env), 'agent')
    once = ISOLATION.get(client(env), {}).get('one_shot')
    return _context(data, (
        'An isolated run starts a new %s session that has only what this ConsoleX task has: its Skill and '
        'ConsoleX Skills, its files, and its ConsoleX tools (run and billed by ConsoleX; this computer needs the '
        '"tools and files" permission, chosen when it was connected). None of your own Skills, plugins, MCP servers, '
        'hooks, instructions or memory load. Run this in a terminal:\n\n%s run %s\n\n%s '
        'Tell the user exactly this in a few lines. Do not run it yourself.') % (
        label, _sh(launcher), _sh(words), 'It runs the task once and prints the result.' if once
        else 'Add --headless to run it once without a session window.'))


COMMANDS = {'consolex:login': on_login, 'consolex:logout': on_logout, 'consolex:tasks': on_tasks,
            'consolex:add': on_add, 'consolex:check': on_check_command, 'consolex:save': on_save,
            'consolex:run': on_run_command}


def handle(data, env, http=http_json):
    event = data.get('hook_event_name')
    cwd = data.get('cwd') or os.getcwd()
    if event == 'SessionStart':
        return on_session_start(data, env)
    if event == 'SessionEnd':
        return on_session_end(data, env, http=http)
    if event == 'Stop':
        output = on_stop(data, env, http=http)
        if (output or {}).get('decision') == 'block':
            heartbeat(data, env, http=http)  # Claude continues this turn.
        else:
            session_state(data, env, 'idle', http=http)  # The turn ended; Claude waits for the owner.
        return output
    if event == 'UserPromptSubmit':
        heartbeat(data, env, http=http)
    if data.get('tool_name') == 'AskUserQuestion':
        # The owner is asked something (PreToolUse) or has answered (PostToolUse).
        if event == 'PreToolUse':
            session_state(data, env, 'ask', http=http)
        elif event == 'PostToolUse':
            heartbeat(data, env, http=http)
        return None
    if event == 'PostToolUse':
        skill = ((data.get('tool_input') or {}).get('skill') or '').strip()
        skill_dir = find_skill_dir(skill, env, cwd)
        return activate(data, env, skill_dir, http=http) if skill_dir else None
    if event == 'UserPromptSubmit':
        prompt = (data.get('prompt') or '').strip()
        if not prompt.startswith('/'):
            return None
        command, _, argument = prompt[1:].partition(' ')
        handler = COMMANDS.get(command)
        if handler:
            return handler(data, env, argument, http=http)
        skill_dir = find_skill_dir(command, env, cwd)
        if skill_dir:
            _cleanup_old_markers(env)
            return activate(data, env, skill_dir, http=http)
    return None


def main():
    try:
        data = json.loads(sys.stdin.read() or '{}')
    except ValueError:
        return 0
    try:
        output = handle(data, dict(os.environ))
    except Exception as exc:  # A broken check must never break the user's session.
        output = {'systemMessage': 'ConsoleX: skipped (%s: %s)' % (type(exc).__name__, exc)}
    if output:
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
    return 0



if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'hook':
        sys.exit(main())
    if len(sys.argv) > 1 and sys.argv[1] == 'poll':
        poll_until_done(dict(os.environ))
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == 'refresh':
        refresh_installed_tasks(dict(os.environ))
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == 'run':
        sys.exit(run_main(sys.argv[2:]))
    sys.stderr.write('usage: consolex.py hook|poll|refresh|run  (hooks read the hook JSON on stdin; run <task words> starts an isolated run)\n')
    sys.exit(2)
