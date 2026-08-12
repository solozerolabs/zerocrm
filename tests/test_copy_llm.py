"""Slice 2 LLM copy step: the copy seam, single-call subject+icebreaker, the
write-time lint/repair, and fail-safe to the floor."""

from zerocrm.copy_llm import build_icebreaker_fn

_PROFILE = "Sid's voice: short, flat, factual."
_GH = {"company_enrichment": {"github": {"found": True, "public_repos": 9,
                                         "top_languages": ["Go"]}}}
_OK = "SUBJECT: your go repos\nICEBREAKER: saw acme ships 9 go repos before most agencies open source anything"


def _fn(reply, capture=None):
    def complete(system, user):
        if capture is not None:
            capture["system"], capture["user"] = system, user
        return reply
    return build_icebreaker_fn(_PROFILE, ["never say leverage"], complete=complete)


def test_off_without_profile_or_key():
    assert build_icebreaker_fn(None, [], complete=lambda s, u: "x") is None
    assert build_icebreaker_fn(_PROFILE, [], api_key="") is None


def test_clean_output_returns_subject_and_icebreaker():
    out = _fn(_OK)(None, "Jane", "Acme", _GH)
    assert out == {"subject": "your go repos",
                   "icebreaker": "saw acme ships 9 go repos before most agencies open source anything"}


def test_name_leak_is_repaired_not_rejected():
    out = _fn("SUBJECT: your go repos\nICEBREAKER: Jane, saw acme ships 9 go repos")(
        None, "Jane", "Acme", _GH)
    assert out["icebreaker"] == "saw acme ships 9 go repos"  # 'Jane, ' stripped


def test_slop_output_falls_back():
    assert _fn("SUBJECT: x\nICEBREAKER: let's leverage your seamless synergy")(
        None, "Jane", "Acme", _GH) is None


def test_condescension_tell_falls_back():
    assert _fn("SUBJECT: your stack\nICEBREAKER: still running plain apache with no cdn in 2025")(
        None, "Jane", "Acme", _GH) is None


def test_empty_subject_falls_back():
    assert _fn("ICEBREAKER: saw acme ships go")(None, "Jane", "Acme", _GH) is None


def test_error_falls_back():
    def boom(system, user):
        raise RuntimeError("api down")
    assert build_icebreaker_fn(_PROFILE, [], complete=boom)(None, "J", "Acme", _GH) is None


def test_no_evidence_returns_none_without_calling():
    calls = []
    def complete(system, user):
        calls.append(user)
        return _OK
    fn = build_icebreaker_fn(_PROFILE, [], complete=complete)
    assert fn(None, "J", "Acme", None) is None
    assert calls == []


def test_prompt_carries_profile_rules_and_evidence():
    cap: dict = {}
    _fn(_OK, cap)({"text": "shipped a Rust SDK last week"}, "Jane", "Acme", _GH)
    assert _PROFILE in cap["system"]
    assert "never say leverage" in cap["system"]
    assert "shipped a Rust SDK last week" in cap["user"]
    assert "9 public repos" in cap["user"]
    assert "Jane" in cap["user"] and "Acme" in cap["user"]
