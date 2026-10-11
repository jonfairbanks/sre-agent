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


@pytest.mark.parametrize('url', [
    'https://github.com/team/state.git',
    'git@github.com:team/state.git',
    'ssh://git@github.com/team/state.git',
])
def test_canonical_repository_transports(url):
    assert gitops._repo(url) == 'team/state'
    app = {'spec': {'source': {'repoURL': url, 'path': 'applications/demo'}}}
    source = gitops._sources(app)[0]
    assert source['repository'] == 'team/state'
    assert source['repoURL'] == 'https://github.com/team/state'


@pytest.mark.parametrize('url', [
    'git@github.com.evil:team/state.git',
    'git@github.com:team/../state.git',
    'git@github.com:team/state.git?token=hidden',
    'git@github.com:team/state.git#hidden',
    'ssh://git:hidden@github.com/team/state.git',
    'ssh://other@github.com/team/state.git',
    'ssh://git@github.com:22/team/state.git',
    'ssh://git@github.com.evil/team/state.git',
    'ssh://git@github.com/team/state.git?token=hidden',
    'ssh://git@github.com/team/state.git#hidden',
    'https://github.com:443/team/state.git',
    'https://github.com/team/state.git?',
    'ssh://git@github.com/team/state.git#',
    'https://github.com//team/state.git',
    'https://github.com/team/%2e%2e/state.git',
])
def test_noncanonical_repository_transports_refused(url):
    with pytest.raises(ValueError):
        gitops._repo(url)


@pytest.mark.parametrize('url', ['git@github.com:team/state.git', 'ssh://git@github.com/team/state.git'])
def test_mixed_chart_and_ssh_value_source_mapping(url):
    app = {'spec': {'sources': [
        {'repoURL': 'https://charts.example', 'chart': 'demo', 'helm': {'valueFiles': ['$values/apps/demo.yaml']}},
        {'repoURL': url, 'targetRevision': 'main', 'ref': 'values', 'path': 'unrelated'},
    ]}}
    mapping = gitops._mapped_sources(app, 'team/state', 'apps/demo.yaml')
    assert mapping == [{'reference': '$values/apps/demo.yaml', 'repository': 'team/state', 'path': 'apps/demo.yaml', 'targetRevision': 'main'}]
    sources = gitops._sources(app)
    assert sources[0]['repoURL'] == 'https://charts.example'
    assert sources[1]['repoURL'] == 'https://github.com/team/state'


def _http_response(payload):
    response = Mock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


@pytest.mark.parametrize('token', ['', 'example-private-read-token'])
def test_github_optional_auth_only_on_fixed_get(monkeypatch, token):
    monkeypatch.setenv('GITOPS_ALLOWED_REPOSITORIES', 'team/state')
    monkeypatch.setenv('GITOPS_GITHUB_TOKEN', token)
    with patch.object(gitops, 'build_opener') as factory:
        factory.return_value.open.return_value = _http_response({'sha': SHA})
        assert gitops._github('team/state', 'commits/main') == {'sha': SHA}
    request = factory.return_value.open.call_args.args[0]
    assert request.full_url == 'https://api.github.com/repos/team/state/commits/main'
    assert request.get_method() == 'GET'
    assert request.get_header('Authorization') == ('Bearer ' + token if token else None)
    assert factory.return_value.open.call_args.kwargs['timeout'] == 10
    factory.assert_called_once_with(gitops._NoRedirect)


def test_private_token_not_used_for_unallowlisted_repository(monkeypatch):
    monkeypatch.setenv('GITOPS_ALLOWED_REPOSITORIES', 'team/state')
    monkeypatch.setenv('GITOPS_GITHUB_TOKEN', 'example-private-read-token')
    with patch.object(gitops, 'build_opener') as factory:
        with pytest.raises(ValueError, match='GITOPS_ALLOWED_REPOSITORIES'):
            gitops._github('other/state', 'commits/main')
    factory.assert_not_called()


def test_private_auth_redirect_is_refused_without_followup(monkeypatch):
    monkeypatch.setenv('GITOPS_ALLOWED_REPOSITORIES', 'team/state')
    monkeypatch.setenv('GITOPS_GITHUB_TOKEN', 'example-private-read-token')
    def redirect(request, **kwargs):
        return gitops._NoRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://evil.example/blob')
    with patch.object(gitops, 'build_opener') as factory:
        factory.return_value.open.side_effect = redirect
        with pytest.raises(ValueError, match='redirects are not permitted') as caught:
            gitops._github('team/state', 'commits/main')
    assert 'example-private-read-token' not in str(caught.value)
    assert factory.return_value.open.call_count == 1


def test_authenticated_private_source_and_proposal_do_not_echo_token(monkeypatch):
    token = 'example-private-read-token'
    monkeypatch.setenv('GITOPS_ALLOWED_REPOSITORIES', 'team/state')
    monkeypatch.setenv('GITOPS_GITHUB_TOKEN', token)
    before = 'kind: Deployment\nspec:\n  replicas: 1\n'
    blob = {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(before.encode()).decode()}
    app = {'spec': {'source': dict(APP['spec']['source'], repoURL='git@github.com:team/state.git')}}
    with patch.object(gitops, '_application', return_value=app), patch.object(gitops, 'build_opener') as factory:
        factory.return_value.open.side_effect = [_http_response({'sha': SHA}), _http_response(blob), _http_response({'sha': SHA}), _http_response(blob)]
        source = gitops.get_gitops_source.invoke({'application': 'demo', 'repository': 'team/state', 'path': 'applications/demo/deployment.yaml'})
        proposal = _proposal()
    assert json.loads(source)['base_commit'] == SHA
    assert proposal['published'] is False
    assert token not in source + json.dumps(proposal)
    for call in factory.return_value.open.call_args_list:
        request = call.args[0]
        assert request.get_header('Authorization') == 'Bearer ' + token
        assert request.full_url.startswith('https://api.github.com/repos/team/state/')
        assert request.get_method() == 'GET'


def test_authenticated_errors_do_not_echo_transport_details(monkeypatch):
    token = 'example-private-read-token'
    monkeypatch.setenv('GITOPS_ALLOWED_REPOSITORIES', 'team/state')
    monkeypatch.setenv('GITOPS_GITHUB_TOKEN', token)
    with patch.object(gitops, '_application', return_value=APP), patch.object(gitops, 'build_opener') as factory:
        factory.return_value.open.side_effect = RuntimeError('Authorization: Bearer ' + token)
        result = _proposal()
    assert token not in json.dumps(result)
    assert 'unavailable' in result['error']


@pytest.mark.parametrize('url', ['git@github.com:team/state.git', 'ssh://git@github.com/team/state.git'])
def test_ssh_stale_base_refused_before_blob_read(url):
    app = {'spec': {'source': dict(APP['spec']['source'], repoURL=url)}}
    with patch.object(gitops, '_application', return_value=app), patch.object(gitops, '_github', return_value={'sha': 'b' * 40}) as github:
        assert 'Base commit does not match' in _proposal()['error']
    assert github.call_count == 1


@pytest.mark.parametrize('control', ['\r', '\n', '\x7f'])
def test_private_token_control_characters_refused_before_request(monkeypatch, control):
    token = 'example-private-read-token' + control
    monkeypatch.setenv('GITOPS_ALLOWED_REPOSITORIES', 'team/state')
    monkeypatch.setenv('GITOPS_GITHUB_TOKEN', token)
    with patch.object(gitops, 'build_opener') as factory:
        with pytest.raises(ValueError, match='authentication configuration') as caught:
            gitops._github('team/state', 'commits/main')
    factory.assert_not_called()
    assert token not in str(caught.value)
