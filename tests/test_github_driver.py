"""Unit tests for the GitHub buildability probe (zerocrm/drivers/github.py)."""

from zerocrm.drivers import github as gh_driver


def test_domain_root_strips_www_and_tld():
    assert gh_driver._domain_root("www.Example.com") == "example"
    assert gh_driver._domain_root("acme.io") == "acme"
    assert gh_driver._domain_root("") == ""


def test_blog_matches_requires_domain_root_in_blog():
    assert gh_driver._blog_matches("https://acme.io/blog", "acme.io") is True
    assert gh_driver._blog_matches("https://unrelated.com", "acme.io") is False
    assert gh_driver._blog_matches("", "acme.io") is False


def test_requote_collapses_whitespace_to_plus():
    assert gh_driver.requote("  Acme   Inc  ") == "Acme+Inc"


def test_github_probe_high_confidence_when_blog_matches_domain(monkeypatch):
    def fake_gh(path):
        if path == "users/acme":
            return {"blog": "https://acme.io", "public_repos": 12}
        if path.startswith("users/acme/repos"):
            return [
                {"language": "Python", "pushed_at": "2026-08-01T00:00:00Z"},
                {"language": "Go", "pushed_at": "2026-07-01T00:00:00Z"},
            ]
        return None

    monkeypatch.setattr(gh_driver, "_gh", fake_gh)

    result = gh_driver.github_probe("Acme Inc", "acme.io")

    assert result == {
        "found": True,
        "login": "acme",
        "public_repos": 12,
        "top_languages": ["Python", "Go"],
        "last_push": "2026-08-01T00:00:00Z",
        "confidence": "high",
    }


def test_github_probe_skips_zero_repo_login_collision(monkeypatch):
    def fake_gh(path):
        if path == "users/acme":
            return {"blog": "", "public_repos": 0}
        if path.startswith("search/users"):
            return {"items": []}
        return None

    monkeypatch.setattr(gh_driver, "_gh", fake_gh)

    assert gh_driver.github_probe("Acme Inc", "acme.io") == {"found": False}
