"""Evidence bounds and GitOps proposal contracts without cluster/network writes."""
import base64
import json
from unittest.mock import Mock, patch
import pytest
from tools.incident_history import make_incident_history_tools
from tools import gitops

SHA = 'a' * 40
APP = {'spec': {'source': {'repoURL': 'https://github.com/team/state.git', 'path': 'applications/demo', 'targetRevision': 'main'}}}


def test_history_bounds_timezone_and_injected_db():
    db = Mock()
    db.recent_monitor_checks.return_value = [{'analysis_valid': False, 'coverage': [], 'report': {'token': 'hidden'}}]
    history = make_incident_history_tools(db)[0]
    for arguments in ({'limit': 51}, {'since': '2026-10-09'}, {'namespace': '../bad'}):
        assert 'error' in json.loads(history.invoke(arguments))
    db.recent_monitor_checks.assert_not_called()
    result = json.loads(history.invoke({'since': '2026-10-09T00:00:00Z', 'namespace': 'demo', 'limit': 2}))
    assert result['checks'][0]['report']['token'] == '[REDACTED]'
    assert 'unknown' in result['interpretation']
    assert db.recent_monitor_checks.call_args.kwargs['since'].tzinfo is not None


def test_finding_history_unavailable_is_unknown():
    db = Mock()
    db.finding_history.side_effect = RuntimeError('database password=hidden')
    result = make_incident_history_tools(db)[1].invoke({'fingerprint': 'abc'})
    assert 'hidden' not in result
    assert json.loads(result)['health'] == 'unknown'


@pytest.mark.parametrize('url', ['http://github.com/team/state', 'https://github.com.evil/team/state', 'https://user@github.com/team/state', 'https://github.com/team/state?x=y', 'https://github.com/team/../state'])
def test_bad_repositories(url):
    with pytest.raises(ValueError):
        gitops._repo(url)


@pytest.mark.parametrize('path', ['../values.yaml', '/values.yaml', 'a//values.yaml', 'a/./values.yaml', 'a/%2e%2e/values.yaml', 'a\\values.yaml', 'secrets/demo.yaml', '.env'])
def test_bad_paths(path):
    with pytest.raises(ValueError):
        gitops._path(path)


def test_multi_source_values_ref_uses_repository_root():
    app = {'spec': {'sources': [{'repoURL': 'https://charts.example', 'chart': 'demo', 'helm': {'valueFiles': ['$values/apps/demo.yaml']}}, {'repoURL': 'https://github.com/team/state', 'targetRevision': 'main', 'ref': 'values', 'path': 'unrelated'}]}}
    mapping = gitops._sources(app)[0]['value_files'][0]
    assert mapping == {'reference': '$values/apps/demo.yaml', 'repository': 'team/state', 'path': 'apps/demo.yaml', 'targetRevision': 'main'}


@pytest.mark.parametrize('content', ['kind: Secret\ndata: {}\n', 'kind: List\nitems:\n- kind: Secret\n', '{"nested": {"apiKey": "hidden"}}', 'env:\n- name: DB_PASSWORD\n  value: hidden\n'])
def test_sensitive_content_refused(content):
    with pytest.raises(ValueError):
        gitops._safe_content(content)


def _proposal(**overrides):
    args = dict(application='demo', repository='team/state', path='applications/demo/deployment.yaml', base_commit=SHA, old_text='replicas: 1', new_text='replicas: 2', reasoning='Keep two ready replicas', evidence='Observed one ready replica')
    args.update(overrides)
    return json.loads(gitops.propose_gitops_change.invoke(args))


def test_exact_proposal_diff_no_external_write():
    before = 'kind: Deployment\nspec:\n  replicas: 1\n'
    responses = [{'sha': SHA}, {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(before.encode()).decode()}]
    with patch.object(gitops, '_application', return_value=APP), patch.object(gitops, '_github', side_effect=responses) as github:
        result = _proposal()
    assert result['base_commit'] == SHA
    assert result['after'] == before.replace('replicas: 1', 'replicas: 2')
    assert '-  replicas: 1\n+  replicas: 2\n' in result['diff']
    assert result['published'] is False
    assert all(call.args[1].startswith(('commits/', 'contents/')) for call in github.call_args_list)


def test_stale_base_refused_before_blob_read():
    with patch.object(gitops, '_application', return_value=APP), patch.object(gitops, '_github', return_value={'sha': 'b' * 40}) as github:
        assert 'Base commit does not match' in _proposal()['error']
    assert github.call_count == 1


def test_unmapped_path_has_no_network_access():
    with patch.object(gitops, '_application', return_value=APP), patch.object(gitops, '_github') as github:
        assert 'not mapped' in _proposal(path='unrelated/file.yaml')['error']
    github.assert_not_called()


def test_allowlist_empty_blocks_http(monkeypatch):
    monkeypatch.delenv('GITOPS_ALLOWED_REPOSITORIES', raising=False)
    with patch.object(gitops, 'build_opener') as opener:
        with pytest.raises(ValueError, match='GITOPS_ALLOWED_REPOSITORIES'):
            gitops._github('team/state', 'commits/main')
    opener.assert_not_called()


def test_argo_projection_excludes_sensitive_parameters_and_message():
    app = {'spec': {'source': dict(APP['spec']['source'], helm={'parameters': [{'name': 'password', 'value': 'hidden'}]})}, 'status': {'sync': {'status': 'Synced', 'revision': SHA}, 'health': {'status': 'Healthy', 'message': 'hidden'}, 'operationState': {'phase': 'Succeeded', 'message': 'hidden'}}}
    with patch.object(gitops, '_application', return_value=app):
        result = gitops.get_argocd_application.invoke({'name': 'demo'})
    assert 'hidden' not in result
    assert json.loads(result)['runtime_request_proven'] is False


def test_proposal_without_final_newline_has_valid_marker():
    before = 'replicas: 1'
    responses = [{'sha': SHA}, {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(before.encode()).decode()}]
    with patch.object(gitops, '_application', return_value=APP), patch.object(gitops, '_github', side_effect=responses):
        diff = _proposal()['diff']
    assert '-replicas: 1\n\\ No newline at end of file\n+replicas: 2\n' in diff


def test_credential_source_url_not_exposed():
    app = {'spec': {'source': {'repoURL': 'https://user:hidden@github.com/team/state', 'path': 'app'}}}
    assert 'hidden' not in json.dumps(gitops._sources(app))


def test_history_output_remains_complete_bounded_json(monkeypatch):
    from tools import incident_history
    monkeypatch.setattr(incident_history, 'TOOL_OUTPUT_MAX_CHARS', 800)
    db = Mock()
    db.recent_monitor_checks.return_value = [{'detail': 'x' * 500} for _ in range(4)]
    result = make_incident_history_tools(db)[0].invoke({})
    payload = json.loads(result)
    assert len(result) < 800
    assert payload['omitted_records'] > 0


def test_gitops_large_output_is_not_a_partial_diff(monkeypatch):
    monkeypatch.setattr(gitops, 'TOOL_OUTPUT_MAX_CHARS', 800)
    result = json.loads(gitops._result({'diff': 'x' * 900}))
    assert 'diff' not in result
    assert 'limit' in result['error']


def test_source_read_returns_resolved_commit_and_content():
    before = 'kind: Deployment\n'
    responses = [{'sha': SHA}, {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(before.encode()).decode()}]
    with patch.object(gitops, '_application', return_value=APP), patch.object(gitops, '_github', side_effect=responses):
        payload = json.loads(gitops.get_gitops_source.invoke({'application': 'demo', 'repository': 'team/state', 'path': 'applications/demo/deployment.yaml'}))
    assert payload['base_commit'] == SHA
    assert payload['content'] == before


@pytest.mark.parametrize('scalar', [
    'curl -H "Authorization: Bearer example_test_credential" https://service.example',
    'Authorization: Basic ZXhhbXBsZTp0ZXN0',
    'https://user:example_test_credential@service.example',
    'password=example_test_credential',
    'ghp_abcdefghijklmnopqrstuvwxyz',
    '-----BEGIN PRIVATE KEY-----',
])
def test_sensitive_scalar_content_refused_without_echo(scalar):
    content = 'kind: ConfigMap\ndata:\n  script: ' + json.dumps(scalar) + '\n'
    with pytest.raises(ValueError, match='Sensitive content') as caught:
        gitops._safe_content(content)
    assert scalar not in str(caught.value)


def test_yaml_alias_dag_and_cycle_are_bounded():
    content = 'a0: &a0 [ok]\n' + ''.join(f'a{i}: &a{i} [*a{i-1}, *a{i-1}]\n' for i in range(1, 50))
    gitops._safe_content(content)
    gitops._safe_content('a: &a [*a]\n')


def test_yaml_nesting_is_bounded():
    with pytest.raises(ValueError, match='inspection limits'):
        gitops._safe_content('a: ' + '[' * 70 + 'ok' + ']' * 70)



def test_null_database_does_not_report_successful_empty_history():
    from persistence import NullDatabase
    history, finding = make_incident_history_tools(NullDatabase())
    assert json.loads(history.invoke({}))['health'] == 'unknown'
    assert json.loads(finding.invoke({'fingerprint': 'abc'}))['health'] == 'unknown'
