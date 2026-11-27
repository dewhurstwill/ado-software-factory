"""super-board-forge-ado.py: the Azure DevOps TRACKER backend for super-board-forge.py.

Not a command. The forge loads it and mixes `AdoVerbs` into `AzureDevOpsBackend`
(after `bind(forge_module)` hands over the forge's Usage/ForgeError/parser helpers).
Work items are tickets, one team's Kanban board is the board, tags are labels, and
the claim is the tag `sb:claimed` written under a revision test. Pull-request verbs
are not here. Contract and mapping: skills/super-board/references/providers.md.

Transport is urllib only. Reads get three attempts and share the GitHub halt file
(exit 79); writes are never blindly retried, except a JSON-Patch carrying a `/rev`
test that the server rejected as stale, which definitely did not apply.
"""
from __future__ import annotations

import base64
import hashlib
import html
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
from pathlib import Path

forge = None  # the forge module, set by bind()

TAG_CLAIM = 'sb:claimed'
TAG_NOT_PLANNED = 'not-planned'
COLUMNS = ['Backlog', 'Ready', 'Building', 'QA', 'Review', 'Blocked', 'Done']
DEFAULT_TYPES = {'feature': 'User Story', 'bug': 'Bug', 'qa': 'User Story'}
DEFAULT_BODY_FIELD = 'System.Description'
DEFAULT_BODY_FIELDS = {'Bug': 'Microsoft.VSTS.TCM.ReproSteps'}  # the Bug form shows Repro Steps
API = '7.1'
API_COMMENTS = '7.1-preview.4'
API_IDENTITY = '7.1-preview'
ARM_RESOURCE = '499b84ac-1321-427f-aa17-267ca6975798'
PAT_VAR = 'AZURE_DEVOPS_EXT_PAT'
EXIT_NOT_FOUND, EXIT_CLAIMED, EXIT_REFUSED, EXIT_FAILED = 3, 4, 65, 69
# When columns are reused for stages that do not match by name or state, prefer these
# slots first (later stages: cards there are furthest along).
REUSE_ORDER = ['Review', 'Building', 'QA', 'Blocked', 'Ready']


def bind(module):
    global forge
    forge = module


# ---- small pure helpers -------------------------------------------------------
def q(text):
    return urllib.parse.quote(str(text), safe='')


def plain(text):
    """Undo the service's storage encoding of Markdown long text. Seen live: a work item body or
    comment written as `"x" & 🙋` reads back `&quot;x&quot; &amp; &#128587;` (`&`, `"` and
    characters outside the BMP become entities; titles are not touched). One unescape is the
    exact inverse: a literal `&amp;` in the source is stored as `&amp;amp;`."""
    return html.unescape(text) if isinstance(text, str) else text


def tags_of(value):
    return [t.strip() for t in (value or '').split(';') if t.strip()]


def has_tag(tags, name):
    return name.lower() in [t.lower() for t in tags]


def without_tag(tags, name):
    return [t for t in tags if t.lower() != name.lower()]


def is_claim_tag(tag):
    low = tag.lower()
    return low == TAG_CLAIM or low.startswith(TAG_CLAIM + '-')


def claim_tags(tags):
    return [t for t in tags if is_claim_tag(t)]


def visible_tags(tags):
    return [t for t in tags if not is_claim_tag(t)]


def tag_ops(before, add=(), remove=()):
    """JSON-Patch ops for System.Tags. The service MERGES on `add` (never removes) and SETS on
    `replace`/`remove`, so: adding = `add` of only the missing tags (atomic, no rev test needed);
    removing = `replace` with the exact final list (`remove` when it ends empty)."""
    gone = {r.lower() for r in remove}
    kept = [t for t in before if t.lower() not in gone]
    if len(kept) != len(before):
        final = kept + [t for t in dict.fromkeys(add) if not has_tag(kept, t)]
        if not final:
            return [{'op': 'remove', 'path': '/fields/System.Tags'}]
        return [{'op': 'replace', 'path': '/fields/System.Tags', 'value': '; '.join(final)}]
    new = [t for t in dict.fromkeys(add) if not has_tag(before, t)]
    return [{'op': 'add', 'path': '/fields/System.Tags', 'value': '; '.join(new)}] if new else []


def is_merge_only(ops):
    return all(op['op'] == 'add' and op['path'] == '/fields/System.Tags' for op in ops)


def need(condition, why):
    if not condition:
        raise ValueError(why)


def is_stale_rev(status, text):
    """True when a rev-tested JSON-Patch was refused for a stale revision (it did not apply).
    Observed: 412 VS403351, and 409 TF26071 when two patches race from one revision; both count,
    as does the message text."""
    if status in (409, 412):
        return True
    return re.search(r'VS403351|TF26071|\brev(?:ision)?\b[^.]{0,80}(?:mismatch|does not match|conflict|stale)|test operation',
                     text or '', re.I) is not None


def number(value):
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return None


def org_name(url):
    parsed = urllib.parse.urlparse(url)
    segments = [s for s in parsed.path.split('/') if s]
    return segments[-1] if segments else parsed.netloc.split('.')[0]


# ---- response validators --------------------------------------------------------
def v_item(value):
    need(isinstance(value, dict) and type(value.get('id')) is int and type(value.get('rev')) is int
         and isinstance(value.get('fields'), dict), 'work item malformed')


def v_board(value):
    need(isinstance(value, dict) and isinstance(value.get('columns'), list) and value['columns'], 'board columns missing')
    need(all(isinstance(c, dict) and isinstance(c.get('name'), str) and c['name'] for c in value['columns']),
         'board column identity missing')
    ref = ((value.get('fields') or {}).get('columnField') or {}).get('referenceName')
    need(isinstance(ref, str) and ref, 'board column field missing')


def v_batch(value):
    need(isinstance(value, dict) and isinstance(value.get('value'), list), 'work item batch missing')
    need(all(w is None or (isinstance(w, dict) and type(w.get('id')) is int and isinstance(w.get('fields'), dict))
             for w in value['value']), 'work item batch malformed')


def v_wiql(value):
    need(isinstance(value, dict) and isinstance(value.get('workItems'), list), 'WIQL result missing')
    need(all(isinstance(w, dict) and type(w.get('id')) is int for w in value['workItems']), 'WIQL ids malformed')


def v_states(value):
    need(isinstance(value, dict) and isinstance(value.get('value'), list) and value['value'], 'work item states missing')
    need(all(isinstance(s, dict) and isinstance(s.get('name'), str) and isinstance(s.get('category'), str)
             for s in value['value']), 'work item state malformed')


def v_comments(value):
    need(isinstance(value, dict) and isinstance(value.get('comments'), list), 'comments missing')
    need(all(isinstance(c, dict) and type(c.get('id')) is int and isinstance(c.get('text'), str)
             and isinstance(c.get('createdBy'), dict) for c in value['comments']), 'comment fields missing')


def v_identity(value):
    user = value.get('authenticatedUser') if isinstance(value, dict) else None
    need(isinstance(user, dict) and isinstance(user.get('id'), str) and user['id'], 'authenticated user missing')


def v_settings(value):
    need(isinstance(value, dict) and isinstance(value.get('bugsBehavior'), str), 'team settings missing bugsBehavior')


# ---- board planning (pure) ----------------------------------------------------------
def map_states(typ, allowed, states):
    """The factory's seven state mappings for one work item type, or ForgeError(65)."""
    inc = (allowed.get('Incoming') or {}).get(typ) or []
    out = (allowed.get('Outgoing') or {}).get(typ) or []
    prog = (allowed.get('InProgress') or {}).get(typ) or []
    if not (inc and out and prog):
        raise forge.ForgeError(f'{typ}: the board allows no incoming, in-progress or outgoing state for it; '
                               'cannot map the factory columns without guessing', EXIT_REFUSED)
    category = dict(states)
    backlog = next((s for s in inc if category.get(s) == 'Proposed'), inc[0])
    done = next((s for s in out if category.get(s) == 'Completed'), out[0])
    in_progress = [name for name, cat in states if cat == 'InProgress' and name in prog]
    active = in_progress[0] if in_progress else prog[0]
    ready = backlog if backlog in prog else active
    return {'Backlog': backlog, 'Ready': ready, 'Building': active, 'QA': active,
            'Review': active, 'Blocked': active, 'Done': done}


def desired_columns(types, mappings):
    """mappings: {type: map_states(...)} -> the seven column specs in order."""
    out = []
    for name in COLUMNS:
        kind = 'incoming' if name == COLUMNS[0] else 'outgoing' if name == COLUMNS[-1] else 'inProgress'
        out.append({'name': name, 'columnType': kind, 'stateMappings': {t: mappings[t][name] for t in types}})
    return out


def plan_columns(existing, desired):
    """Pair each desired column with an existing one (or None). Returns (pairs, leftovers).
    Order of preference: same name and type; the incoming/outgoing ends; same state mapping;
    then REUSE_ORDER. Leftover existing columns would be deleted by the PUT."""
    taken = set()
    pair = [None] * len(desired)

    def comparable(e, d):
        return all(e.get('stateMappings', {}).get(t) == s for t, s in d['stateMappings'].items())

    for i, d in enumerate(desired):
        for j, e in enumerate(existing):
            if j not in taken and e['name'].casefold() == d['name'].casefold() and e.get('columnType') == d['columnType']:
                pair[i] = j; taken.add(j); break
    for i, d in enumerate(desired):
        if pair[i] is None and d['columnType'] != 'inProgress':
            for j, e in enumerate(existing):
                if j not in taken and e.get('columnType') == d['columnType']:
                    pair[i] = j; taken.add(j); break
    middle = [i for i, d in enumerate(desired) if d['columnType'] == 'inProgress']
    pending = []
    for j, e in enumerate(existing):
        if j in taken or e.get('columnType') != 'inProgress':
            continue
        slot = next((i for i in middle if pair[i] is None and comparable(e, desired[i])), None)
        if slot is None:
            pending.append(j)
        else:
            pair[slot] = j; taken.add(j)
    for j in pending:
        slot = next((i for n in REUSE_ORDER for i in middle if desired[i]['name'] == n and pair[i] is None), None)
        if slot is not None:
            pair[slot] = j; taken.add(j)
    leftovers = [e for j, e in enumerate(existing) if j not in taken]
    return [(d, existing[pair[i]] if pair[i] is not None else None) for i, d in enumerate(desired)], leftovers


def column_changes(pairs, leftovers, existing):
    lines = []
    for d, e in pairs:
        if e is None:
            lines.append(f'columns: create "{d["name"]}"')
            continue
        if e['name'] != d['name']:
            lines.append(f'columns: rename "{e["name"]}" -> "{d["name"]}"')
        for t, state in d['stateMappings'].items():
            old = (e.get('stateMappings') or {}).get(t)
            if old != state:
                lines.append(f'columns: "{d["name"]}" maps {t} {old} -> {state}')
        if e.get('itemLimit', 0) != 0:
            lines.append(f'columns: "{d["name"]}" itemLimit {e.get("itemLimit")} -> 0')
    matched = [e['id'] for _, e in pairs if e is not None]
    if matched != [e['id'] for e in existing if e['id'] in matched]:
        lines.append('columns: reorder')
    lines += [f'columns: remove "{e["name"]}"' for e in leftovers]
    return lines


# ---- the verbs --------------------------------------------------------------------------
class AdoVerbs:
    name = 'azure-devops'

    def __init__(self, config, repo):
        super().__init__(config, repo)
        self._auth = None
        self._me = None
        self._me_unique = ''
        self._cache = None
        repo_path = (config.get('repo') or {}).get('path')
        if repo_path:
            os.environ.setdefault('SB_REPO_PATH', repo_path)

    # ---- config -------------------------------------------------------------------------
    @property
    def ado(self):
        value = self.config.get('ado')
        if not isinstance(value, dict) or not value.get('project'):
            raise forge.ForgeError('config "ado" block with "project" is required for provider azure-devops', forge.EXIT_CONFIG)
        return value

    @property
    def base(self):
        url = os.environ.get('SB_ADO_BASE_URL') or self.ado.get('organization') or ''
        if not url:
            raise forge.ForgeError('config ado.organization is required (or set SB_ADO_BASE_URL)', forge.EXIT_CONFIG)
        return url.rstrip('/')

    @property
    def web_org(self):
        return (self.ado.get('organization') or self.base).rstrip('/')

    @property
    def project(self):
        return self.ado['project']

    @property
    def team(self):
        return self.ado.get('team') or f'{self.project} Team'

    @property
    def board_name(self):
        return self.ado.get('board') or 'Stories'

    @property
    def repository(self):
        return self.ado.get('repository') or self.project

    @property
    def proj_api(self):
        return f'{self.base}/{q(self.project)}/_apis'

    @property
    def team_api(self):
        return f'{self.base}/{q(self.project)}/{q(self.team)}/_apis'

    def web_url(self, n):
        return f'{self.web_org}/{q(self.project)}/_workitems/edit/{n}'

    def type_map(self):
        merged = dict(DEFAULT_TYPES)
        merged.update({k.lower(): v for k, v in (self.ado.get('work_item_types') or {}).items()})
        return merged

    def body_field(self, typ):
        return self.configured_body_fields().get(typ) or DEFAULT_BODY_FIELD

    def body_fields(self):
        return sorted({DEFAULT_BODY_FIELD, *self.configured_body_fields().values()})

    def configured_body_fields(self):
        """An explicit `body_fields` (even {}) wins; absent means the built-in default."""
        value = self.ado.get('body_fields')
        return DEFAULT_BODY_FIELDS if value is None else value

    def resolve_repo(self):
        return f'{org_name(self.web_org)}/{self.project}/{self.repository}'

    explicit_repo = resolve_repo

    # ---- halt file (shared with the GitHub path; wording says Azure DevOps) ----------------
    def check_halt(self):
        if forge.github_read.halt_path().exists():
            raise forge.github_read.ReadHalted('Azure DevOps reads are halted; inspect the saved reason and '
                                               'explicitly restart the run')

    def stop(self, reason, attempts):
        forge.github_read.halt(reason, attempts)

    def check(self, argv):
        self.check_halt()

    def halt(self, argv):
        p = forge.parser('halt <reason>')
        p.add_argument('reason')
        self.stop(p.parse_args(argv).reason, 1)

    # ---- auth ---------------------------------------------------------------------------------
    def pat_from_env_file(self):
        path = Path((self.config.get('repo') or {}).get('path') or os.environ.get('SB_REPO_PATH') or '.') / '.env'
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            return ''
        for line in text.splitlines():
            m = re.match(rf'\s*(?:export\s+)?{PAT_VAR}\s*=\s*(.*?)\s*$', line)
            if m:
                value = m.group(1)
                if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                    value = value[1:-1]
                if value:
                    return value
        return ''

    @staticmethod
    def az_token():
        try:
            # On Windows the Azure CLI is az.cmd, which only resolves through PATHEXT.
            az = shutil.which('az')
            if not az:
                return ''
            r = subprocess.run([az, 'account', 'get-access-token', '--resource', ARM_RESOURCE,
                                '--query', 'accessToken', '-o', 'tsv'], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ''
        return r.stdout.strip() if r.returncode == 0 else ''

    def credentials(self):
        if self._auth:
            return self._auth
        pat = os.environ.get(PAT_VAR, '').strip() or self.pat_from_env_file()
        if pat:
            self._auth = 'Basic ' + base64.b64encode(f':{pat}'.encode()).decode()
        else:
            token = self.az_token()
            if token:
                self._auth = 'Bearer ' + token
        if not self._auth:
            raise forge.ForgeError(f'no Azure DevOps credentials: set {PAT_VAR} in the environment, or put a line '
                                   f'{PAT_VAR}=... in <repo>/.env, or run `az login` so `az account get-access-token` works',
                                   forge.EXIT_UNAVAILABLE)
        return self._auth

    # ---- transport ------------------------------------------------------------------------------
    def request(self, method, url, body=None, content_type='application/json'):
        headers = {'Authorization': self.credentials(), 'Accept': 'application/json', 'User-Agent': 'super-board-forge'}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = content_type
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = _OPENER.open(req, timeout=30)
            status, hdrs, raw = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as error:
            status, hdrs, raw = error.code, error.headers, error.read()
        self.note_quota(hdrs)
        if status == 429:
            self.note_throttle(hdrs)
        return status, hdrs, raw

    def quota_path(self):
        return forge.github_read.halt_path().with_name('ado-quota.json')

    def throttle_path(self):
        return forge.github_read.halt_path().with_name('ado-throttle.json')

    def note_throttle(self, hdrs):
        """A 429 is the service's "you went over the limit": the throttle step-down reads it as the
        honest signal that a wave hit the limit (there is no up-front quota to read). Best effort."""
        try:
            path = self.throttle_path()
            try:
                before = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                before = {}
            record = {'count': int(before.get('count') or 0) + 1, 'last_at': int(time.time()),
                      'retry_after': number(hdrs.get('Retry-After'))}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f'.{os.getpid()}')
            tmp.write_text(json.dumps(record), encoding='utf-8')
            os.replace(tmp, path)
        except OSError:
            pass

    def note_quota(self, hdrs):
        remaining, cost = hdrs.get('X-RateLimit-Remaining'), hdrs.get('X-RateLimit-Cost')
        if remaining is None and cost is None:
            return
        record = {'remaining': number(remaining), 'limit': number(hdrs.get('X-RateLimit-Limit')),
                  'reset': number(hdrs.get('X-RateLimit-Reset')), 'at': int(time.time())}
        try:
            path = self.quota_path()
            if cost is not None:  # the service may send only the cost of each call
                try:
                    before = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    before = {}
                record['cost'] = number(cost)
                record['total_cost'] = (before.get('total_cost') or 0) + (number(cost) or 0)
                if remaining is None:
                    record.update({k: before.get(k) for k in ('remaining', 'limit', 'reset')})
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f'.{os.getpid()}')
            tmp.write_text(json.dumps(record), encoding='utf-8')
            os.replace(tmp, path)
        except OSError:
            pass

    @staticmethod
    def pause(seconds):
        time.sleep(seconds * float(os.environ.get('SB_ADO_SLEEP_SCALE', '1')))

    def read(self, method, url, body=None, validate=None, fixed=True, what='read'):
        """Three attempts; permanent errors and exhausted attempts halt (79)."""
        self.check_halt()
        delay = float(os.environ.get('SB_GITHUB_RETRY_DELAY', '1'))
        last = 'Azure DevOps required read failed'
        for attempt in (1, 2, 3):
            self.check_halt()
            wait = None
            try:
                status, hdrs, raw = self.request(method, url, body)
            except OSError:
                last = 'Azure DevOps required read timed out or could not connect'
            else:
                if status in (203, 302, 400, 401, 403) or (status == 404 and fixed):
                    self.stop(f'Azure DevOps rejected a required read ({what}: HTTP {status}); fix credentials, '
                              'permissions or config before restarting', attempt)
                if status == 404:
                    raise forge.ForgeError(f'{what}: not found (HTTP 404)', EXIT_NOT_FOUND)
                if status == 429:
                    try:
                        wait = min(float(hdrs.get('Retry-After', '1')), 60.0)
                    except ValueError:
                        wait = 1.0
                    last = 'Azure DevOps throttled a required read (HTTP 429)'
                elif 200 <= status < 300:
                    try:
                        value = json.loads(raw.decode('utf-8'))
                        if validate:
                            validate(value)
                        self.check_halt()
                        return value
                    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
                        last = f'Azure DevOps required read returned incomplete evidence: {error}'
                else:
                    last = f'Azure DevOps required read failed (HTTP {status})'
            if attempt < 3:
                note = f'waiting {wait:g}s; ' if wait is not None else ''
                print(f'[azure-devops] {last}; {note}retry {attempt + 1}/3', file=sys.stderr)
                if wait is not None:
                    self.pause(wait)
                else:
                    time.sleep(max(0, delay) * attempt)
        self.stop(f'{last}; stopped after 3 failed attempts', 3)

    def write(self, method, url, body, what, content_type='application/json'):
        """One attempt, never retried. Returns (status, parsed json or None, text)."""
        self.check_halt()
        try:
            status, _, raw = self.request(method, url, body, content_type)
        except OSError as error:
            raise forge.ForgeError(f'{what}: Azure DevOps write failed ({type(error).__name__}); not retried, '
                                   'outcome unknown', EXIT_FAILED)
        text = raw.decode('utf-8', 'replace')
        try:
            return status, json.loads(text), text
        except ValueError:
            return status, None, text

    def written(self, status, parsed, text, what):
        if 200 <= status < 300 and isinstance(parsed, dict):
            return parsed
        message = (parsed or {}).get('message') if isinstance(parsed, dict) else None
        raise forge.ForgeError(f'{what} failed (HTTP {status}): {(message or text)[:300]}', EXIT_FAILED)

    # ---- cache (same directory rule as super-board-card.sh) ------------------------------------
    def cache_dir(self):
        if os.environ.get('SB_CARD_CACHE_DIR'):
            return Path(os.environ['SB_CARD_CACHE_DIR'])
        cwd = Path(os.environ.get('SB_REPO_PATH') or Path.cwd())
        r = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                           capture_output=True, text=True, timeout=5, cwd=cwd)
        if r.returncode != 0:
            return cwd / '.claude/super-board/cache'
        common = Path(r.stdout.strip())
        return (common.parent / '.claude/super-board/cache') if common.name == '.git' else common / 'super-board/cache'

    def cache_file(self):
        slug = re.sub(r'[^A-Za-z0-9._-]+', '_', f'{self.project}-{self.team}-{self.board_name}')
        return self.cache_dir() / f'board-ado-{slug}.json'

    def cache_load(self):
        if self._cache is None:
            try:
                value = json.loads(self.cache_file().read_text(encoding='utf-8'))
                self._cache = value if isinstance(value, dict) else {}
            except (OSError, ValueError):
                self._cache = {}
        return self._cache

    def cache_save(self):
        try:
            directory = self.cache_dir()
            directory.mkdir(parents=True, exist_ok=True)
            gitignore = directory / '.gitignore'
            if not gitignore.exists():
                gitignore.write_text('*\n', encoding='utf-8')
            tmp = self.cache_file().with_name(f'.board.{os.getpid()}')
            tmp.write_text(json.dumps(self._cache), encoding='utf-8')
            os.replace(tmp, self.cache_file())
        except OSError:
            pass

    def cache_drop_board(self):
        cache = self.cache_load()
        for key in ('columnField', 'columns', 'types'):
            cache.pop(key, None)
        self.cache_save()

    # ---- board, states, identity ------------------------------------------------------------------
    def read_board(self):
        return self.read('GET', f'{self.team_api}/work/boards/{q(self.board_name)}?api-version={API}',
                         validate=v_board, what='board')

    def load_board(self, refresh=False):
        cache = self.cache_load()
        if refresh or not (cache.get('columnField') and cache.get('columns')):
            board = self.read_board()
            cache.update(columnField=board['fields']['columnField']['referenceName'],
                         columns=[c['name'] for c in board['columns']],
                         types=sorted({t for c in board['columns'] for t in (c.get('stateMappings') or {})}))
            self.cache_save()
        return cache

    def states(self, typ):
        cache = self.cache_load()
        known = cache.setdefault('states', {})
        if typ not in known:
            value = self.read('GET', f'{self.proj_api}/wit/workitemtypes/{q(typ)}/states?api-version={API}',
                              validate=v_states, what=f'states of {typ}')
            known[typ] = [[s['name'], s['category']] for s in value['value']]
            self.cache_save()
        return known[typ]

    def category(self, typ, state):
        return dict(self.states(typ)).get(state)

    def whoami(self):
        if self._me is None:
            value = self.read('GET', f'{self.base}/_apis/connectionData?api-version={API_IDENTITY}',
                              validate=v_identity, what='identity')
            user = value['authenticatedUser']
            account = ((user.get('properties') or {}).get('Account') or {}).get('$value')
            self._me = {'login': account or user.get('providerDisplayName') or user['id'], 'id': user['id']}
            self._me_unique = account if isinstance(account, str) else ''
        return self._me

    def whoami_unique(self):
        """The token's unique name (connectionData Account), or '' when the service gave none:
        never the display name, which approval must not trust."""
        self.whoami()
        return self._me_unique

    # ---- human approval ------------------------------------------------------------------------
    def approvers(self):
        """config ado.approvers: unique names allowed to approve a held merge by commenting `done`,
        compared case-insensitively. Absent or empty: nobody."""
        value = self.ado.get('approvers')
        if value is None:
            return set()
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise forge.ForgeError('config ado.approvers must be a list of unique names (e.g. "user@example.com")',
                                   forge.EXIT_CONFIG)
        return {v.strip().casefold() for v in value}

    def may_approve(self, login):
        return isinstance(login, str) and bool(login.strip()) and login.strip().casefold() in self.approvers()

    def permission_verb(self, argv):
        p = forge.parser('permission <login>'); p.add_argument('login')
        login = p.parse_args(argv).login
        print(json.dumps({'permission': 'approve' if self.may_approve(login) else 'none'}))

    # ---- work items --------------------------------------------------------------------------------
    def get_item(self, n, fields):
        url = f'{self.proj_api}/wit/workitems/{n}?fields={urllib.parse.quote(",".join(fields), safe=",")}&api-version={API}'
        return self.read('GET', url, validate=v_item, fixed=False, what=f'work item #{n}')

    def wiql(self, types, order='ASC', where=''):
        listing = ', '.join("'" + t.replace("'", "''") + "'" for t in types)
        query = ('SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project '
                 f'AND [System.WorkItemType] IN ({listing}){where} ORDER BY [System.Id] {order}')
        value = self.read('POST', f'{self.proj_api}/wit/wiql?api-version={API}', {'query': query},
                          validate=v_wiql, what='WIQL')
        return [w['id'] for w in value['workItems']]

    def fetch(self, ids, fields):
        out = []
        for i in range(0, len(ids), 200):
            value = self.read('POST', f'{self.proj_api}/wit/workitemsbatch?api-version={API}',
                              {'ids': ids[i:i + 200], 'fields': fields, 'errorPolicy': 'omit'},
                              validate=v_batch, what='work item batch')
            out += [w for w in value['value'] if w]
        return out

    def board_types(self):
        types = self.load_board().get('types') or []
        if not types:
            raise forge.ForgeError('the board has no work item types in its state mappings', EXIT_FAILED)
        return types

    def ticket_fields(self):
        return ['System.Title', 'System.State', 'System.Tags', 'System.WorkItemType', *self.body_fields()]

    def ticket(self, wi):
        f = wi['fields']
        typ = f.get('System.WorkItemType') or ''
        tags = tags_of(f.get('System.Tags'))
        cat = self.category(typ, f.get('System.State')) if typ else None
        closed = cat in ('Completed', 'Removed')
        reason = None
        if closed:
            reason = 'NOT_PLANNED' if cat == 'Removed' or has_tag(tags, TAG_NOT_PLANNED) else 'COMPLETED'
        return {'number': wi['id'], 'title': f.get('System.Title') or '',
                'body': plain(f.get(self.body_field(typ)) or f.get(DEFAULT_BODY_FIELD) or ''),
                'state': 'CLOSED' if closed else 'OPEN', 'stateReason': reason,
                'labels': [{'name': t} for t in visible_tags(tags)],
                'assignees': [{'login': self.whoami()['login']}] if claim_tags(tags) else [],
                'url': self.web_url(wi['id'])}

    def update_item(self, n, fields, build, what):
        """Read, build a JSON-Patch (a /rev test is prepended unless the ops are only a tag merge,
        which the service applies atomically), apply. A stale-rev rejection did not apply, so
        re-read and retry (max 3). build() returns ops, or [] for nothing to do."""
        for attempt in (1, 2, 3):
            wi = self.get_item(n, fields)
            ops = build(wi)
            if not ops:
                return wi
            status, parsed, text = self.write(
                'PATCH', f'{self.proj_api}/wit/workitems/{n}?api-version={API}',
                ([] if is_merge_only(ops) else [{'op': 'test', 'path': '/rev', 'value': wi['rev']}]) + list(ops),
                what, 'application/json-patch+json')
            if 200 <= status < 300:
                return parsed
            if is_stale_rev(status, text) and attempt < 3:
                continue
            self.fail_patch(status, parsed, text, what, stale=is_stale_rev(status, text))
        raise forge.ForgeError(f'{what}: lost the revision race three times', EXIT_FAILED)

    def fail_patch(self, status, parsed, text, what, stale=False):
        if 'TF401320' in text:
            raise ColumnRejected(text)
        message = (parsed or {}).get('message') if isinstance(parsed, dict) else None
        raise forge.ForgeError(f'{what} failed (HTTP {status}): {(message or text)[:300]}', EXIT_FAILED)

    # ---- board verbs ----------------------------------------------------------------------------------
    def board(self, argv):
        if not argv:
            raise forge.Usage('usage: board ids|items|status <N>|item-id <N>|move <N> <Status>|ensure [--check]')
        verb, rest = argv[0], argv[1:]
        table = {'ids': self.board_ids, 'items': self.board_items, 'status': self.board_status,
                 'item-id': self.board_item_id, 'move': self.board_move, 'ensure': self.board_ensure}
        if verb not in table:
            raise forge.Usage(f'unknown board verb: {verb}')
        table[verb](rest)

    def board_ids(self, argv):
        forge.parser('board ids').parse_args(argv)
        cache = self.load_board()
        print(json.dumps({'board': self.board_name, 'columnField': cache['columnField'], 'columns': cache['columns']}))

    def board_items(self, argv):
        forge.parser('board items').parse_args(argv)
        cache = self.load_board()
        field = cache['columnField']
        ids = self.wiql(self.board_types())
        items = []
        repository = f'{org_name(self.web_org)}/{self.project}'
        for wi in self.fetch(ids, ['System.Title', 'System.Tags', 'System.WorkItemType', field]):
            column = wi['fields'].get(field)
            if not column:
                continue
            tags = tags_of(wi['fields'].get('System.Tags'))
            title = wi['fields'].get('System.Title') or ''
            items.append({'id': str(wi['id']), 'title': title, 'status': column,
                          'labels': visible_tags(tags),
                          'content': {'type': 'Issue', 'number': wi['id'], 'title': title,
                                      'url': self.web_url(wi['id']), 'repository': repository,
                                      'assignees': [{'login': self.whoami()['login']}] if claim_tags(tags) else []}})
        items.sort(key=lambda i: i['content']['number'])
        print(json.dumps({'items': items, 'totalCount': len(items)}))

    def board_status(self, argv):
        p = forge.parser('board status <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        field = self.load_board()['columnField']
        column = self.get_item(n, [field])['fields'].get(field)
        if not column:
            raise forge.ForgeError(f'#{n} is not on board {self.board_name}', EXIT_NOT_FOUND)
        print(column)

    def board_item_id(self, argv):
        p = forge.parser('board item-id <N>'); p.add_argument('number', type=forge.positive)
        print(p.parse_args(argv).number)

    def move(self, n, column):
        cache = self.load_board()
        if column not in cache['columns']:
            cache = self.load_board(refresh=True)
            if column not in cache['columns']:
                raise forge.ForgeError(f'column "{column}" is not on board {self.board_name}', EXIT_NOT_FOUND)
        field = cache['columnField']

        def build(wi):
            if wi['fields'].get(field) == column:
                return []
            return [{'op': 'add', 'path': f'/fields/{field}', 'value': column}]
        try:
            self.update_item(n, [field], build, f'move #{n}')
        except ColumnRejected:
            cache = self.load_board(refresh=True)
            if column not in cache['columns']:
                raise forge.ForgeError(f'column "{column}" is not on board {self.board_name}', EXIT_NOT_FOUND)
            raise forge.ForgeError(f'move #{n}: the server rejected column "{column}" although the board lists it', EXIT_FAILED)

    def board_move(self, argv):
        p = forge.parser('board move <N> <Status>'); p.add_argument('number', type=forge.positive); p.add_argument('status')
        a = p.parse_args(argv)
        self.move(a.number, a.status)
        print(f'moved #{a.number} → {a.status}')

    # ---- board ensure -------------------------------------------------------------------------------------
    def board_ensure(self, argv):
        p = forge.parser('board ensure [--check]'); p.add_argument('--check', action='store_true')
        check = p.parse_args(argv).check
        changes = []

        def change(text):
            changes.append(text)
            print(('needed: ' if check else 'changed: ') + text)

        settings = self.read('GET', f'{self.team_api}/work/teamsettings?api-version={API}', validate=v_settings,
                             what='team settings')
        if settings['bugsBehavior'] != 'asRequirements':
            change(f'bugsBehavior {settings["bugsBehavior"]} -> asRequirements')
            if not check:
                status, parsed, text = self.write('PATCH', f'{self.team_api}/work/teamsettings?api-version={API}',
                                                  {'bugsBehavior': 'asRequirements'}, 'team settings update')
                self.written(status, parsed, text, 'team settings update')
        board = self.read_board()
        allowed = board.get('allowedMappings') or {}
        types = sorted({t for c in board['columns'] for t in (c.get('stateMappings') or {})}
                       | {t for m in allowed.values() for t in m})
        mappings = {t: map_states(t, allowed, self.states_fresh(t)) for t in types}
        desired = desired_columns(types, mappings)
        existing = [dict(c, id=c.get('id')) for c in board['columns']]
        pairs, leftovers = plan_columns(existing, desired)
        field = board['fields']['columnField']['referenceName']
        for e in leftovers:
            count = self.column_count(field, e['name'])
            if count != 0:
                raise forge.ForgeError(f'refusing to remove column "{e["name"]}": ' +
                                       ('it holds cards; move them to another column first' if count else
                                        'could not verify that it is empty') +
                                       f'. Not changed: columns of board {self.board_name}.', EXIT_REFUSED)
        lines = column_changes(pairs, leftovers, existing)
        for line in lines:
            change(line)
        if lines and not check:
            body = []
            for d, e in pairs:
                entry = {'name': d['name'], 'columnType': d['columnType'], 'itemLimit': 0,
                         'stateMappings': d['stateMappings']}
                if d['columnType'] == 'inProgress':
                    entry['isSplit'] = False
                if e is not None:
                    entry['id'] = e['id']
                body.append(entry)
            status, parsed, text = self.write('PUT', f'{self.team_api}/work/boards/{q(self.board_name)}/columns?api-version={API}',
                                              body, 'board column update')
            if not (200 <= status < 300):
                self.written(status, parsed, text, 'board column update')
        if not check:
            self.cache_load().pop('states', None)
            self.cache_drop_board()
        if not changes:
            print('board ok: seven columns, bugs on the board')
        if check and changes:
            raise forge.ForgeError('', 1)

    def states_fresh(self, typ):
        self.cache_load().get('states', {}).pop(typ, None)
        return [tuple(s) for s in self.states(typ)]

    def column_count(self, field, column):
        """Cards in a column; None-like -1 when the server cannot say."""
        query = ('SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project '
                 f'AND [{field}] = \'{column.replace(chr(39), chr(39) * 2)}\'')
        try:
            status, _, raw = self.request('POST', f'{self.proj_api}/wit/wiql?api-version={API}', {'query': query})
            if not 200 <= status < 300:
                return -1
            return len(json.loads(raw.decode('utf-8'))['workItems'])
        except (OSError, ValueError, KeyError, TypeError):
            return -1

    # ---- ticket reads ----------------------------------------------------------------------------------------
    def ticket_view(self, argv):
        p = forge.parser('ticket view <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        print(json.dumps(self.ticket(self.get_item(n, self.ticket_fields()))))

    def ticket_list(self, argv):
        p = forge.parser('ticket list --state open|closed|all --limit N')
        p.add_argument('--state', default='open', choices=['open', 'closed', 'all'])
        p.add_argument('--limit', default='30', type=forge.positive)
        a = p.parse_args(argv)
        ids = sorted(self.wiql(self.board_types()), reverse=True)
        out = []
        for i in range(0, len(ids), 200):
            for wi in sorted(self.fetch(ids[i:i + 200], self.ticket_fields()), key=lambda w: -w['id']):
                t = self.ticket(wi)
                if a.state == 'all' or t['state'].lower() == a.state:
                    out.append(t)
            if len(out) >= int(a.limit):
                break
        print(json.dumps(out[:int(a.limit)]))

    def ticket_graph(self, argv):
        """One comments call per open ticket: the cost driver of this verb."""
        forge.parser('ticket graph').parse_args(argv)
        out = []
        for wi in self.fetch(self.wiql(self.board_types()), self.ticket_fields()):
            t = self.ticket(wi)
            if t['state'] != 'OPEN':
                continue
            comments = self.list_comments(t['number'])[-8:]
            out.append({'number': t['number'], 'title': t['title'], 'body': t['body'], 'state': 'OPEN',
                        'labels': {'nodes': t['labels']},
                        'comments': {'nodes': [{'body': plain(c['text'])} for c in comments]}})
        out.sort(key=lambda t: t['number'])
        print(json.dumps(out))

    def ticket_history(self, argv):
        forge.parser('ticket history').parse_args(argv)
        out = []
        for wi in self.fetch(self.wiql(self.board_types()), self.ticket_fields()):
            t = self.ticket(wi)
            out.append({'number': t['number'], 'state': t['state'], 'body': t['body']})
        out.sort(key=lambda t: t['number'])
        print(json.dumps(out))

    # ---- ticket writes -----------------------------------------------------------------------------------------
    def type_for(self, labels):
        types = self.type_map()
        for label in labels:
            if label.lower() in types:
                return types[label.lower()]
        return types['feature']

    def ticket_create(self, argv):
        p = forge.parser('ticket create --title T --body-file F [--label L]... [--column C]')
        p.add_argument('--title', required=True); p.add_argument('--body-file', required=True)
        p.add_argument('--label', action='append', default=[]); p.add_argument('--column')
        a = p.parse_args(argv)
        body = Path(a.body_file).read_text(encoding='utf-8')
        if a.column and a.column not in self.load_board()['columns'] \
                and a.column not in self.load_board(refresh=True)['columns']:
            raise forge.ForgeError(f'column "{a.column}" is not on board {self.board_name}', EXIT_NOT_FOUND)
        typ = self.type_for(a.label)
        field = self.body_field(typ)
        ops = [{'op': 'add', 'path': '/fields/System.Title', 'value': a.title},
               {'op': 'add', 'path': f'/fields/{field}', 'value': body}]
        labels = list(dict.fromkeys(a.label))
        if labels:
            ops.append({'op': 'add', 'path': '/fields/System.Tags', 'value': '; '.join(labels)})
        ops.append({'op': 'add', 'path': f'/multilineFieldsFormat/{field}', 'value': 'Markdown'})
        status, parsed, text = self.write('POST', f'{self.proj_api}/wit/workitems/${q(typ)}?api-version={API}', ops,
                                          'ticket create', 'application/json-patch+json')
        created = self.written(status, parsed, text, 'ticket create')
        n = created.get('id')
        if type(n) is not int:
            raise forge.ForgeError('ticket create: the response carried no work item id', EXIT_FAILED)
        if a.column:
            try:
                self.move(n, a.column)
            except forge.ForgeError as error:
                # The ticket exists: hand its number to the caller (stdout, last line) so a filer can
                # report "filed, not placed" instead of losing it, then fail.
                print(f'created #{n} but could not place it in "{a.column}": {error}', file=sys.stderr)
                print(self.web_url(n))
                print(n)
                raise
        print(self.web_url(n))
        print(n)

    def ticket_edit(self, argv):
        p = forge.parser('ticket edit <N> [--title T] [--body-file F] [--add-label L]... [--remove-label L]...')
        p.add_argument('number', type=forge.positive); p.add_argument('--title'); p.add_argument('--body-file')
        p.add_argument('--add-label', action='append', default=[]); p.add_argument('--remove-label', action='append', default=[])
        a = p.parse_args(argv)
        body = Path(a.body_file).read_text(encoding='utf-8') if a.body_file else None

        def build(wi):
            f = wi['fields']
            ops = []
            if a.title is not None:
                ops.append({'op': 'add', 'path': '/fields/System.Title', 'value': a.title})
            if body is not None:
                field = self.body_field(f.get('System.WorkItemType') or '')
                ops += [{'op': 'add', 'path': f'/fields/{field}', 'value': body},
                        {'op': 'add', 'path': f'/multilineFieldsFormat/{field}', 'value': 'Markdown'}]
            if a.add_label or a.remove_label:
                ops += tag_ops(tags_of(f.get('System.Tags')), a.add_label, a.remove_label)
            return ops
        self.update_item(a.number, ['System.Tags', 'System.WorkItemType'], build, f'edit #{a.number}')

    def add_tag(self, n, tag):
        def build(wi):
            return tag_ops(tags_of(wi['fields'].get('System.Tags')), [tag])
        self.update_item(n, ['System.Tags'], build, f'tag #{n}')

    def ticket_close(self, argv):
        p = forge.parser('ticket close <N> --reason completed|not-planned'); p.add_argument('number', type=forge.positive)
        p.add_argument('--reason', required=True, choices=['completed', 'not-planned'])
        a = p.parse_args(argv)
        if a.reason == 'not-planned':
            self.add_tag(a.number, TAG_NOT_PLANNED)
        self.move(a.number, COLUMNS[-1])
        print(f'closed #{a.number} ({a.reason})')

    def main_checkout(self):
        cwd = Path(os.environ.get('SB_REPO_PATH') or Path.cwd())
        r = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                           capture_output=True, text=True, timeout=5, cwd=cwd)
        if r.returncode != 0:
            return str(cwd.resolve())
        common = Path(r.stdout.strip())
        return str(common.parent if common.name == '.git' else common)

    def holder(self):
        """Stable 6-hex id of this machine + user + main checkout (env SB_CLAIM_HOLDER overrides)."""
        given = os.environ.get('SB_CLAIM_HOLDER', '').strip().lower()
        if given:
            return given
        who = os.environ.get('USER') or os.environ.get('LOGNAME') or ''
        raw = f'{socket.gethostname()}\n{who}\n{self.main_checkout()}'
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:6]

    def my_claim_tag(self):
        return f'{TAG_CLAIM}-{self.holder()}'

    def ticket_claim(self, argv):
        """Add our holder-specific claim tag, then verify. A merge-`add` of different values can
        both land, so after adding, the lexicographically smallest claim tag wins and the loser
        takes its own tag back. Two claimers with the SAME holder id are not separated by the tag."""
        p = forge.parser('ticket claim <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        mine = self.my_claim_tag()

        def build(wi):
            claims = claim_tags(tags_of(wi['fields'].get('System.Tags')))
            if any(c.lower() != mine.lower() for c in claims):
                raise forge.ForgeError(f'#{n} is already claimed', EXIT_CLAIMED)
            return tag_ops(claims, [mine])
        self.update_item(n, ['System.Tags'], build, f'claim #{n}')
        claims = claim_tags(tags_of(self.get_item(n, ['System.Tags'])['fields'].get('System.Tags')))
        if any(c.lower() != mine.lower() for c in claims) and min(c.lower() for c in claims) != mine.lower():
            self.update_item(n, ['System.Tags'], lambda wi: tag_ops(tags_of(wi['fields'].get('System.Tags')), (), [mine]),
                             f'claim #{n} (withdraw)')
            raise forge.ForgeError(f'#{n} was claimed by another holder at the same moment', EXIT_CLAIMED)
        print(f'claimed #{n}')

    def ticket_release(self, argv):
        p = forge.parser('ticket release <N> [--force]'); p.add_argument('number', type=forge.positive)
        p.add_argument('--force', action='store_true')
        a = p.parse_args(argv)
        mine = self.my_claim_tag()
        released = []

        def build(wi):
            tags = tags_of(wi['fields'].get('System.Tags'))
            gone = claim_tags(tags) if a.force else [t for t in tags if t.lower() == mine.lower()]
            released[:] = gone
            return tag_ops(tags, (), gone) if gone else []
        self.update_item(a.number, ['System.Tags'], build, f'release #{a.number}')
        print(f'released #{a.number}' + (f' ({", ".join(released)})' if released else ' (nothing held)'))

    # ---- comments --------------------------------------------------------------------------------------------------
    def comments_url(self, n, comment=None, extra=''):
        tail = f'/{comment}' if comment else ''
        return f'{self.proj_api}/wit/workItems/{n}/comments{tail}?{extra}api-version={API_COMMENTS}'

    def list_comments(self, n, strict=False):
        """Every comment, oldest first. strict (human approval): a continuation token that repeats
        means the listing is incomplete, so raise rather than return the pages read so far."""
        out, token, seen = [], None, set()
        while True:
            extra = '$top=200&order=asc&' + (f'continuationToken={q(token)}&' if token else '')
            value = self.read('GET', self.comments_url(n, extra=extra), validate=v_comments, fixed=False,
                              what=f'comments of #{n}')
            out += value['comments']
            token = value.get('continuationToken')
            if token and token in seen and strict:
                raise ValueError(f'comments of #{n}: the continuation token repeated; the listing is incomplete')
            if not token or token in seen:
                return out
            seen.add(token)

    def ticket_comment_rows(self, n, strict=False):
        """The `ticket comments` rows plus what approval needs: `uniqueName` (the author's unique
        name, never the display name) and `version` (1 = never edited)."""
        rows = []
        for c in self.list_comments(n, strict):
            by = c['createdBy']
            rows.append({'id': c['id'], 'body': plain(c['text']),
                         'user': {'login': by.get('uniqueName') or by.get('displayName') or '', 'type': 'User'},
                         'created_at': c.get('createdDate') or '',
                         'updated_at': c.get('modifiedDate') or c.get('createdDate') or '',
                         'version': c.get('version'), 'uniqueName': by.get('uniqueName') or ''})
        return rows

    def ticket_comment(self, argv):
        p = forge.parser('ticket comment <N> --body-file F'); p.add_argument('number', type=forge.positive)
        p.add_argument('--body-file', required=True)
        a = p.parse_args(argv)
        text = Path(a.body_file).read_text(encoding='utf-8')
        status, parsed, raw = self.write('POST', self.comments_url(a.number, extra='format=markdown&'), {'text': text},
                                         f'comment on #{a.number}')
        created = self.written(status, parsed, raw, f'comment on #{a.number}')
        print(created.get('id'))

    def ticket_comments(self, argv):
        p = forge.parser('ticket comments <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        page = [{k: v for k, v in row.items() if k != 'uniqueName'} for row in self.ticket_comment_rows(n)]
        print(json.dumps([page]))

    def ticket_comment_edit(self, argv):
        p = forge.parser('ticket comment-edit <N> <ID> --body-file F')
        p.add_argument('number', type=forge.positive); p.add_argument('comment', type=forge.positive)
        p.add_argument('--body-file', required=True)
        a = p.parse_args(argv)
        text = Path(a.body_file).read_text(encoding='utf-8')
        status, parsed, raw = self.write('PATCH', self.comments_url(a.number, a.comment, 'format=markdown&'),
                                         {'text': text}, f'edit comment {a.comment} on #{a.number}')
        self.written(status, parsed, raw, f'edit comment {a.comment} on #{a.number}')
        print(a.comment)

    # ---- identity, quota ----------------------------------------------------------------------------------------------
    def whoami_verb(self, argv):
        forge.parser('whoami').parse_args(argv)
        print(json.dumps(self.whoami()))

    def quota_verb(self, argv):
        """The saved X-RateLimit-* record plus `seen`. `--throttle` prints instead the 429s the
        transport has recorded since the last take, `{throttled, throttled_at}`; `--take-throttle`
        does the same and then resets the count, so a caller asking once per wave sees only that
        wave's. (Separate from the plain record so its shape never changes.)"""
        p = forge.parser('quota [--throttle | --take-throttle]')
        p.add_argument('--throttle', action='store_true'); p.add_argument('--take-throttle', action='store_true')
        a = p.parse_args(argv)
        if a.throttle or a.take_throttle:
            try:
                hit = json.loads(self.throttle_path().read_text(encoding='utf-8'))
            except (OSError, ValueError):
                hit = {}
            print(json.dumps({'throttled': int(hit.get('count') or 0), 'throttled_at': hit.get('last_at')}))
            if a.take_throttle and hit:
                try:
                    self.throttle_path().unlink()
                except OSError:
                    pass
            return
        try:
            record = json.loads(self.quota_path().read_text(encoding='utf-8'))
            record['seen'] = True
        except (OSError, ValueError):
            record = {'remaining': None, 'limit': None, 'reset': None, 'at': None, 'seen': False}
        print(json.dumps(record))


class ColumnRejected(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a sign-in redirect is an auth failure, not a page to follow


_OPENER = urllib.request.build_opener(_NoRedirect)
