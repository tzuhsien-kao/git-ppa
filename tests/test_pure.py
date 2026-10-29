# SPDX-License-Identifier: GPL-3.0-or-later
from git_ppa import merge, profile as profile_mod, versions
from git_ppa.profile import Caps, Risk


def test_tag_encoding():
    assert versions.enc_tag("5:8.0.5-1ubuntu0.1") == "5%8.0.5-1ubuntu0.1"
    assert versions.enc_tag("1.0~rc1-1") == "1.0_rc1-1"


def test_suffix_handling():
    assert versions.strip_suffix("1.28.3-2ubuntu1.4+acme7", "acme") == "1.28.3-2ubuntu1.4"
    assert versions.strip_suffix("26.01-0ubuntu1~acme7", "acme") == "26.01-0ubuntu1"
    assert versions.strip_suffix("1.0-1", "acme") == "1.0-1"
    assert versions.has_suffix("1.0-1+acme2", "acme")
    assert not versions.has_suffix("1.0-1+acme2", "other")


def test_next_version_sorts_above_both():
    new = versions.next_version("1.28.3-2ubuntu1.12", "acme")
    assert new == "1.28.3-2ubuntu1.12+acme1"
    assert versions.gt(new, "1.28.3-2ubuntu1.12")
    assert versions.gt(new, "1.28.3-2ubuntu1.4+acme7")


def test_tilde_suffix_sorts_below_base():
    assert not versions.gt(versions.next_version("26.01-0ubuntu1", "acme", in_archive=False), "26.01-0ubuntu1")


def test_profile_expand_reports_missing_variable():
    prof = profile_mod.from_dict({
        "project": {"tag": "x", "series": ["s"]},
        "repo": {"url": "{nope}", "upstream_url": "u"},
        "branches": {"mirror": "m", "downstream": "d", "staging": "s"},
    })
    try:
        prof.expand("{nope}")
    except profile_mod.ProfileError as e:
        assert "nope" in str(e)
    else:
        raise AssertionError("expected ProfileError")


def test_parse_hunks_numbers_lines():
    diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -3,2 +3,1 @@\n-old a\n-old b\n+new\n@@ -10,0 +10,1 @@\n+added\n"
    h = merge.parse_hunks(diff)
    assert h[0].removed == [(3, "old a"), (4, "old b")] and h[0].added == [(3, "new")]
    assert h[1].removed == [] and h[1].added == [(10, "added")]


def test_parse_hunks_keeps_lines_that_look_like_headers():
    diff = "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n--- not a header\n+++ also not\n"
    h = merge.parse_hunks(diff)
    assert h[0].removed == [(1, "-- not a header")] and h[0].added == [(1, "++ also not")]


def test_parse_conflicts_counts_lines_per_side():
    text = "a\n<<<<<<< HEAD\none\ntwo\n=======\nthree\n>>>>>>> tag\nb\n<<<<<<< HEAD\nx\n=======\ny\nz\n>>>>>>> tag\n"
    assert merge.parse_conflicts(text) == [(2, 1), (1, 2)]
    assert [n for n, _ in merge.conflict_lines(text)] == [3, 4, 6, 10, 12, 13]


def test_rules_spans_find_marker_blocks_only():
    import re
    pats = [re.compile(r"-mcpu")]
    text = "a\nifeq (x,y)\nfoo\nendif\nifeq ($(A),arm64)\nCFLAGS=-mcpu=custom\nendif\n"
    assert merge.rules_spans(text, pats) == [(5, 7)]


def test_control_field_follows_continuation_lines():
    lines = ["Package: a", "Provides: b,", " c,", " d", "Description: x"]
    assert merge.control_field(lines, 3) == "Provides"
    assert merge.control_field(lines, 5) == "Description"


def test_risk_hits_rules_and_control():
    risk = Risk(rules_markers=[r"-mcpu"], control_fields=["Provides"], files=[])
    old = "a\nCFLAGS=-mcpu=x\nb\n"
    hits = merge.risk_hits("debian/rules", old, old, [(2, "CFLAGS=-mcpu=x")], [], risk)
    assert [h.kind for h in hits] == ["rules-toolchain"]
    assert merge.risk_hits("debian/rules", old, old, [(1, "a")], [], risk) == []
    ctrl = "Package: a\nProvides: b,\n c\nDepends: d\n"
    assert merge.risk_hits("debian/control", ctrl, ctrl, [], [(3, " c")], risk)[0].kind == "control-Provides"
    assert merge.risk_hits("debian/control", ctrl, ctrl, [], [(4, "Depends: d")], risk) == []


def test_classify_caps():
    caps = Caps()
    one = [merge.Conflict("debian/control", [(1, 1)])]
    assert merge.classify([], "low", caps) == "mechanical"
    assert merge.classify(one, "low", caps) == "ai-assisted"
    assert merge.classify([merge.Conflict("debian/rules", [(8, 4)])], "low", caps) == "ai-assisted"
    assert merge.classify([merge.Conflict("debian/rules", [(8, 4)])], "high", caps) == "re-port"
    assert merge.classify(one * 3, "low", caps) == "re-port"
