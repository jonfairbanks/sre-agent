"""Read Argo state and prepare public-GitHub diffs without publishing."""
import base64
import difflib
import json
import os
import re
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import yaml
from config import TOOL_OUTPUT_MAX_CHARS
from langchain.tools import tool
from .k8s_client import custom_objects

_MAX_BYTES = 32768


def _result(payload):
    result = json.dumps(payload)
    if len(result) > max(512, TOOL_OUTPUT_MAX_CHARS - 128):
        return json.dumps({'error': 'Evidence exceeds the tool output limit; select a smaller source or narrower Application. No changes made.'})
    return result


def _repo(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.query or parsed.fragment:
        raise ValueError('Only HTTPS github.com repository sources are supported')
    path = parsed.path.removesuffix('.git').strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', path) or any(p in ('.', '..') for p in path.split('/')):
        raise ValueError('Invalid GitHub repository path')
    return path


def _path(path):
    if not isinstance(path, str) or not path or '\\' in path or '%' in path:
        raise ValueError('Invalid source path')
    if path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/')):
        raise ValueError('Source path must be relative without traversal')
    if re.search(r'(^|/)(secrets?|credentials?|\.env[^/]*|.*\.pem|.*\.key)(/|$)', path, re.I):
        raise ValueError('Sensitive source paths cannot be read')
    return path


def _allowed():
    return {v.strip() for v in os.getenv('GITOPS_ALLOWED_REPOSITORIES', '').split(',') if v.strip()}


def _sources(application):
    spec = application.get('spec', {})
    sources = spec.get('sources') or ([spec['source']] if spec.get('source') else [])
    refs = {s['ref']: s for s in sources if s.get('ref')}
    result = []
    for source in sources[:20]:
        entry = {k: source[k] for k in ('path', 'chart', 'targetRevision', 'ref') if k in source}
        source_url = urlsplit(source.get('repoURL', ''))
        entry['repoURL'] = source.get('repoURL') if source_url.scheme == 'https' and not (source_url.username or source_url.password or source_url.query or source_url.fragment) else '[UNAVAILABLE]'
        try:
            entry['repository'] = _repo(source.get('repoURL', ''))
        except ValueError:
            entry['repository'] = None
        entry['value_files'] = []
        for filename in source.get('helm', {}).get('valueFiles', [])[:50]:
            if filename.startswith('$'):
                ref, _, path = filename[1:].partition('/')
                target = refs.get(ref)
                entry['value_files'].append({'reference': filename, 'repository': _repo(target['repoURL']) if target else None,
                                             'path': _path(path), 'targetRevision': target.get('targetRevision') if target else None})
            else:
                _path(filename)
                entry['value_files'].append({'reference': filename, 'repository': entry['repository'],
                                             'path': _path(str(PurePosixPath(source.get('path', '')) / filename)),
                                             'targetRevision': source.get('targetRevision')})
        result.append(entry)
    return result


def _application(name, namespace):
    for value in (name, namespace):
        if not re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?', value):
            raise ValueError('Invalid Application name or namespace')
    return custom_objects().get_namespaced_custom_object('argoproj.io', 'v1alpha1', namespace, 'applications', name, _request_timeout=10)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('GitHub redirects are not permitted')


def _github(repository, endpoint):
    if repository not in _allowed() or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Repository is not in GITOPS_ALLOWED_REPOSITORIES')
    url = f'https://api.github.com/repos/{repository}/{endpoint}'
    request = Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'sre-agent-readonly'})
    with build_opener(_NoRedirect).open(request, timeout=10) as response:
        payload = response.read(_MAX_BYTES * 4 + 1)
    if len(payload) > _MAX_BYTES * 4:
        raise ValueError('GitHub response exceeds size limit')
    return json.loads(payload)


def _safe_content(content):
    if len(content.encode()) > _MAX_BYTES:
        raise ValueError('Source exceeds size limit')
    # SafeLoader preserves alias identity. Visit containers once to avoid
    # exponential work on repeated aliases, and cap ordinary nesting as well.
    visited = set()
    node_count = 0

    def inspect(value, depth=0):
        nonlocal node_count
        node_count += 1
        if depth > 64 or node_count > 10000:
            raise ValueError('Source structure exceeds inspection limits')
        if isinstance(value, (dict, list)):
            identity = id(value)
            if identity in visited:
                return
            visited.add(identity)
        if isinstance(value, str):
            if (re.search(r'(?i)\b(?:bearer|basic)\s+[^\s\"\']+', value)
                    or re.search(r'(?i)\b(?:password|token|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+', value)
                    or re.search(r'https?://[^\s/@]+:[^\s/@]+@', value, re.I)
                    or re.search(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{15,}|AKIA[A-Z0-9]{16})\b', value)
                    or '-----BEGIN ' in value):
                raise ValueError('Sensitive content cannot be read or proposed')
        if isinstance(value, dict):
            if value.get('kind') == 'Secret':
                raise ValueError('Secret manifests cannot be read or proposed')
            for key, item in value.items():
                if re.search(r'password|token|credential|private.?key|secret|api.?key', str(key), re.I):
                    raise ValueError('Sensitive content cannot be read or proposed')
                inspect(item, depth + 1)
            if re.search(r'password|token|credential|private.?key|secret|api.?key', str(value.get('name', '')), re.I) and 'value' in value:
                raise ValueError('Sensitive environment values cannot be read or proposed')
        elif isinstance(value, list):
            for item in value:
                inspect(item, depth + 1)
    for document in yaml.safe_load_all(content):
        visited.clear()
        inspect(document)
    if re.search(r'(?im)^\s*[\w.-]*(?:password|token|credential|private.?key|secret)[\w.-]*\s*[:=]', content) or '-----BEGIN ' in content:
        raise ValueError('Sensitive content cannot be read or proposed')


@tool
def get_argocd_application(name: str, namespace: str = 'argocd') -> str:
    """Read Argo sources, revisions, sync, health and rollout phase. Sync/health do not prove a real request succeeded."""
    try:
        app = _application(name, namespace)
        status = app.get('status', {})
        operation = status.get('operationState', {})
        return _result({'application': name, 'namespace': namespace, 'sources': _sources(app),
                           'destination': app.get('spec', {}).get('destination', {}),
                           'sync': {k: status.get('sync', {}).get(k) for k in ('status', 'revision', 'revisions')},
                           'health': status.get('health', {}).get('status', 'Unknown'),
                           'rollout': {k: operation.get(k) for k in ('phase', 'startedAt', 'finishedAt')},
                           'resources': [{k: r.get(k) for k in ('group', 'kind', 'namespace', 'name', 'status')} | {'health': r.get('health', {}).get('status')} for r in status.get('resources', [])[:100]],
                           'runtime_request_proven': False, 'merged': 'unverified', 'released': 'unverified'})
    except ValueError as exc:
        return _result({'error': str(exc)})
    except Exception:
        return _result({'error': 'Argo Application evidence unavailable', 'health': 'unknown'})


def _mapped_sources(application, repository, path):
    candidates = []
    for source in _sources(application):
        if source.get('repository') == repository and 'path' in source:
            root = source['path']
            if root in ('', '.') or path == _path(root) or path.startswith(_path(root) + '/'):
                candidates.append(source)
        candidates.extend(v for v in source['value_files'] if v['repository'] == repository and v['path'] == path)
    if not candidates:
        raise ValueError('Path is not mapped by this Argo Application')
    return candidates


def _resolved_commit(candidates, repository):
    revisions = {s.get('targetRevision') or 'HEAD' for s in candidates}
    if any(not isinstance(r, str) or len(r) > 256 or any(ord(c) < 32 for c in r) for r in revisions):
        raise ValueError('Invalid source revision')
    resolved = {_github(repository, 'commits/' + quote(revision, safe=''))['sha'] for revision in revisions}
    if len(resolved) != 1:
        raise ValueError('Ambiguous source revisions')
    commit = resolved.pop()
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid resolved commit')
    return commit


def _read_source(repository, path, commit):
    blob = _github(repository, 'contents/' + quote(path, safe='/') + '?ref=' + commit)
    if blob.get('type') != 'file' or blob.get('encoding') != 'base64':
        raise ValueError('Expected a bounded GitHub file blob')
    content = base64.b64decode(blob['content'], validate=False).decode('utf-8')
    _safe_content(content)
    return content


@tool
def get_gitops_source(application: str, repository: str, path: str, namespace: str = 'argocd') -> str:
    """Read one allowed public GitHub YAML source mapped by Argo, returning its exact commit for a reviewable proposal. Refuses sensitive content."""
    try:
        path = _path(path)
        candidates = _mapped_sources(_application(application, namespace), repository, path)
        commit = _resolved_commit(candidates, repository)
        return _result({'repository': repository, 'path': path, 'base_commit': commit,
                           'content': _read_source(repository, path, commit)})
    except ValueError as exc:
        return _result({'error': str(exc)})
    except Exception:
        return _result({'error': 'GitOps source evidence unavailable'})


@tool
def propose_gitops_change(application: str, repository: str, path: str, base_commit: str,
                          old_text: str, new_text: str, reasoning: str, evidence: str,
                          namespace: str = 'argocd') -> str:
    """Prepare a unified diff against an exact public GitHub commit mapped by Argo. Replaces one unique match; never commits, opens a PR, or writes to the cluster."""
    try:
        path = _path(path)
        if not re.fullmatch(r'[0-9a-f]{40}', base_commit):
            raise ValueError('Base commit must be an exact 40-character SHA')
        if not reasoning.strip() or not evidence.strip():
            raise ValueError('Reasoning and evidence are required')
        candidates = _mapped_sources(_application(application, namespace), repository, path)
        if _resolved_commit(candidates, repository) != base_commit:
            raise ValueError('Base commit does not match the current Application source revision; refresh evidence')
        before = _read_source(repository, path, base_commit)
        if not old_text or before.count(old_text) != 1 or old_text == new_text:
            raise ValueError('Replacement must identify one unique match and change it')
        after = before.replace(old_text, new_text, 1)
        _safe_content(after)
        lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile='a/' + path, tofile='b/' + path)
        diff = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines)
        try:
            ownership = json.loads(os.getenv('GITOPS_REPOSITORY_OWNERSHIP', '{}')).get(repository, 'unverified')
        except (ValueError, AttributeError):
            ownership = 'unverified'
        return _result({'repository': repository, 'path': path, 'base_commit': base_commit,
                           'ownership': ownership, 'before': before, 'after': after, 'diff': diff,
                           'reasoning': reasoning[:2000], 'evidence': evidence[:4000], 'published': False,
                           'next_step': 'Review this diff before any external write. Publish through repository CI/CD after approval.',
                           'runtime_request_proven': False})
    except ValueError as exc:
        return _result({'error': str(exc)})
    except Exception:
        return _result({'error': 'GitOps proposal evidence unavailable; no changes made'})
