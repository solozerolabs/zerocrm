from zerocrm.drivers import github


def test_github_probe_high_confidence_when_blog_matches_domain(monkeypatch):
    responses = {
        "users/acme": {"blog": "https://acme.com", "public_repos": 12},
        "users/acme/repos?sort=pushed&per_page=10": [
            {"language": "Python"},
            {"language": "TypeScript"},
            {"language": "Python"},
        ],
        "users/acme/repos?sort=pushed&per_page=1": [{"pushed_at": "2026-08-01T00:00:00Z"}],
    }
    monkeypatch.setattr(github, "_gh", lambda path: responses.get(path))

    result = github.github_probe(name="", domain="acme.com")

    assert result == {
        "found": True,
        "login": "acme",
        "public_repos": 12,
        "top_languages": ["Python", "TypeScript"],
        "last_push": "2026-08-01T00:00:00Z",
        "confidence": "high",
    }


def test_github_probe_no_match_returns_found_false(monkeypatch):
    monkeypatch.setattr(github, "_gh", lambda path: None)

    assert github.github_probe(name="", domain="unknown.io") == {"found": False}
