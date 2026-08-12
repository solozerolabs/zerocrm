"""Cold-email copy: 4-touch cadence, anti-slop, per-lead icebreaker, preview
render, and the Smartlead sequence payload shape."""

from zerocrm.copy import (
    default_sequence, icebreaker_from_hook, render_preview, slop_hits,
    subject_hook, to_smartlead,
)


def test_sequence_shape_and_cadence():
    steps = default_sequence()
    assert [s["seq_number"] for s in steps] == [1, 2, 3, 4]
    assert [s["delay_days"] for s in steps] == [0, 3, 5, 10]      # widening cadence
    assert steps[0]["subject"] and steps[1]["subject"] == ""       # follow-ups thread
    assert "{{icebreaker}}" in steps[0]["body"]                    # per-lead slot
    assert "{{subject_hook}}" in steps[0]["subject"]               # per-lead subject
    assert all("{{unsubscribe_link}}" in s["body"] for s in steps) # opt-out every send


def test_copy_is_slop_free():
    for s in default_sequence():
        assert slop_hits(s["subject"] + " " + s["body"]) == []     # no em dash / banned words


def test_sender_address_placeholder_is_loud_when_unset():
    assert "[POSTAL ADDRESS REQUIRED]" in default_sequence()[0]["body"]
    assert "[POSTAL ADDRESS REQUIRED]" not in default_sequence(
        {"sender_address": "1 Main St, SF"})[0]["body"]


def test_icebreaker_ladder_hook_then_enrichment_then_floor():
    # 1. research hook wins
    assert icebreaker_from_hook({"text": "hiring 3 senior eng"}, "Jane", "Acme") == \
        "Saw Acme is hiring 3 senior eng."
    # 2. no hook -> held enrichment (github > tech > keywords)
    gh = {"company_enrichment": {"github": {"found": True, "public_repos": 9,
                                            "top_languages": ["Go"]}}}
    assert "9 public repos" in icebreaker_from_hook(None, "Jane", "Acme", gh)
    tech = {"company_enrichment": {"technologies": ["Next.js", "AWS"]}}
    assert "builds on Next.js" in icebreaker_from_hook(None, "Jane", "Acme", tech)
    kw = {"company_enrichment": {"keywords": ["web development"]}}
    assert "does web development for clients" in icebreaker_from_hook(None, "Jane", "Acme", kw)
    # 3. nothing held -> floor, still company-specific, never generic praise
    assert "Acme" in icebreaker_from_hook(None, "Jane", "Acme")


def test_clean_company_trims_marketing_suffixes():
    from zerocrm.copy import clean_company
    assert clean_company("Cappital - Custom App Development Firm") == "Cappital"
    assert clean_company("Dream Beyond | Custom Software") == "Dream Beyond"
    assert clean_company("Acme") == "Acme" and clean_company(None) == ""


def test_keyword_floor_skips_industry_taxonomy_strings():
    kw = {"company_enrichment": {"keywords": ["technology, information & internet",
                                              "web development"]}}
    assert "web development" in icebreaker_from_hook(None, "J", "Acme", kw)
    assert subject_hook(kw, "Acme") == "your web development work"


def test_subject_hook_their_thing_never_empty():
    tech = {"company_enrichment": {"technologies": ["Next.js"]}}
    assert subject_hook(tech, "Acme") == "your next.js builds"
    kw = {"company_enrichment": {"keywords": ["App Development"]}}
    assert subject_hook(kw, "Acme") == "your app development work"
    assert subject_hook({}, "Acme") == "acme + agents"             # non-empty floor


def test_render_preview_fills_merges_and_collapses_spintax():
    p = render_preview(default_sequence()[0],
                       {"first_name": "Jane", "company_name": "Acme",
                        "icebreaker": "Saw Acme ships fast.",
                        "subject_hook": "your next.js builds"})
    assert "Saw Acme ships fast." in p["body"]
    assert p["subject"] == "your next.js builds"
    assert "{{" not in p["body"] and "{{" not in p["subject"]      # merges filled
    assert "|" not in p["subject"]                                 # spintax collapsed
    assert "unsubscribe" in p["body"] and "{{unsubscribe_link}}" not in p["body"]


def test_warm_reply_windows_booking_and_no_slop():
    from zerocrm.copy import warm_reply
    r = warm_reply("Jane", {"booking_url": "https://cal/x", "site_url": "syndai.ai"})
    assert r["subject"] == "" and "Jane" in r["body"]          # same-thread reply
    assert "https://cal/x" in r["body"] and "syndai.ai" in r["body"]  # link OK in warm reply
    assert "Thursday or Friday" in r["body"]                   # two plain-text windows
    assert slop_hits(r["body"]) == []
    assert "[BOOKING LINK REQUIRED]" in warm_reply("J", {})["body"]  # loud when unset


def test_to_smartlead_payload_shape():
    # flat single-variant shape, verified live 2026-08-09 (API rejects `variants`)
    seqs = to_smartlead(default_sequence())["sequences"]
    assert len(seqs) == 4
    assert seqs[0]["seq_number"] == 1 and seqs[0]["seq_delay_details"]["delay_in_days"] == 0
    assert seqs[1]["seq_delay_details"]["delay_in_days"] == 3
    assert "email_body" in seqs[0] and "subject" in seqs[0]
    assert "variants" not in seqs[0]
    assert seqs[1]["subject"] == ""              # follow-up threads


def test_lint_copy_flags_each_violation_class():
    from zerocrm.copy import lint_copy
    assert lint_copy("your go repos", "saw acme ships go") == []          # clean
    assert "empty icebreaker" in lint_copy("subj", "")
    assert "empty subject" in lint_copy("", "saw acme ships go")
    assert any("name" in v for v in lint_copy("subj", "Jane, saw acme", "Jane"))
    assert any("slop" in v for v in lint_copy("subj", "let's leverage synergy"))
    assert any("condescension" in v for v in lint_copy("subj", "feels backwards to me"))
    assert any("link" in v for v in lint_copy("subj", "check https://x.com"))
    assert any("too long" in v for v in lint_copy("subj", "x " * 200))


def test_default_sequence_and_floor_are_lint_clean():
    from zerocrm.copy import lint_copy, icebreaker_from_hook, subject_hook
    # the deterministic floor (the safe fallback) must never itself fail lint
    cand = {"company_enrichment": {"technologies": ["Next.js"]}}
    ice = icebreaker_from_hook(None, "Jane", "Acme", cand)
    subj = subject_hook(cand, "Acme")
    assert lint_copy(subj, ice, "Jane") == []


def test_strip_leading_name():
    from zerocrm.copy import strip_leading_name
    assert strip_leading_name("Edgar, saw acme", "Edgar") == "saw acme"
    assert strip_leading_name("edgar: saw acme", "Edgar") == "saw acme"
    assert strip_leading_name("saw acme", "Edgar") == "saw acme"   # no leak, untouched
