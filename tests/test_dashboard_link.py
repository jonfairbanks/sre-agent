import pytest
from slack_notifier import dashboard_link


@pytest.mark.parametrize("url", ["", "javascript:alert(1)", "ftp://example.com", "https://", "https://user:password@example.com", "https://example.com:bad", "https://bad host/path", "https://[invalid/path", "https://example.com/\nattack", "x" * 2049])
def test_unset_or_invalid_dashboard_url_leaves_plain_text(monkeypatch, url):
    monkeypatch.setenv("DASHBOARD_URL", url)
    assert dashboard_link() == "dashboard"


def test_dashboard_url_links_exact_configured_path_query_and_fragment(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://example.com/sre/?view=health&scope=all#checks")
    assert dashboard_link() == "<https://example.com/sre/?view=health&amp;scope=all#checks|dashboard>"


def test_local_http_dashboard_supported_and_whitespace_trimmed(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "  http://localhost:8080/  ")
    assert dashboard_link() == "<http://localhost:8080/|dashboard>"


def test_url_cannot_inject_slack_link_delimiters(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://example.com/a|b><@everyone>")
    link = dashboard_link()
    assert link == "<https://example.com/a%7Cb%3E%3C@everyone%3E|dashboard>"
    assert link.count("|") == 1


def test_missing_dashboard_url_leaves_plain_text(monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    assert dashboard_link() == "dashboard"
