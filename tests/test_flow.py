# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole flow on local fixture repositories: clone, sync, merge start."""

import pytest

from conftest import CHANGELOG_V1, CONTROL_V1, NEW_ENTRY, RULES_V1, commit, git, new_version, write
from git_ppa import gitutil, merge, versions, workspace

V2 = "1.0-1ubuntu1.1"


def prepare(prof, tmp_path, up, files):
    new_version(up, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1, **files})
    pkg = prof.package("hello-acme")
    repo = workspace.clone(prof, pkg, "resolute", "lp", tmp_path / "ws")
    workspace.sync_local(prof, pkg, "resolute", repo, V2)
    return pkg, repo


def test_clone_sets_up_remotes_branches_and_driver(prof, tmp_path, world):
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1})
    pkg = prof.package("hello-acme")
    repo = workspace.clone(prof, pkg, "resolute", "lp", tmp_path / "ws")
    assert gitutil.current_branch(repo) == "acme/resolute"
    assert gitutil.rev("pkg/import/1.0-1ubuntu1.1", repo)
    assert gitutil.rev("refs/heads/ubuntu/resolute", repo)
    assert "dpkg-mergechangelogs" in (repo / ".git/info/attributes").read_text()
    assert git(repo, "tag", "--list", "import/*") == ""
    assert git(repo, "tag", "--list", "pkg/*") == "pkg/import/1.0-1ubuntu1\npkg/import/1.0-1ubuntu1.1"
    assert git(repo, "branch", "-r", "--list", "pkg/*") == ""


def test_sync_is_fast_forward_only(prof, tmp_path, world):
    pkg, repo = prepare(prof, tmp_path, world, {})
    assert gitutil.rev("refs/heads/ubuntu/resolute", repo) == gitutil.rev("pkg/import/1.0-1ubuntu1.1", repo)
    again = workspace.sync_local(prof, pkg, "resolute", repo, V2)
    assert again[0] == again[1]
    git(repo, "branch", "-f", "ubuntu/resolute", "acme/resolute")
    with pytest.raises(workspace.WorkspaceError, match="not an ancestor"):
        workspace.sync_local(prof, pkg, "resolute", repo, V2)


def test_clean_merge_is_mechanical_and_low_risk(prof, tmp_path, world):
    pkg, repo = prepare(prof, tmp_path, world, {"debian/patches/CVE-1.patch": "p\n", "debian/patches/series": "CVE-1.patch\n"})
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert (r.merged, r.tier, r.risk, r.conflicts) == (True, "mechanical", "low", [])
    assert r.base == "1.0-1ubuntu1" and r.staging == "acme-proposed/resolute"
    assert "+CVE-1.patch" in r.series_changes
    assert {p for _, p in r.changed} == {"debian/changelog", "debian/patches/CVE-1.patch", "debian/patches/series"}
    top = [l for l in git(repo, "show", "HEAD:debian/changelog").splitlines() if " (" in l and "urgency" in l]
    assert [t.split()[1] for t in top] == ["(1.0-1ubuntu1.1)", "(1.0-1ubuntu1+acme1)", "(1.0-1ubuntu1)"]
    for t in ("old/acme", "old/ubuntu", "new/ubuntu"):
        assert gitutil.rev(t, repo)


def test_clean_merge_that_lands_in_a_risk_region_is_high_risk(prof, tmp_path, world):
    pkg, repo = prepare(prof, tmp_path, world, {"debian/rules": RULES_V1.replace("-O2", "-O3")})
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert (r.merged, r.tier, r.risk) == (True, "mechanical", "high")
    assert [h.kind for h in r.hits] == ["rules-toolchain", "rules-toolchain"]


def test_build_system_file_our_delta_does_not_touch_is_listed_but_not_a_risk(prof, tmp_path, world):
    pkg, repo = prepare(prof, tmp_path, world, {"configure.ac": "AC_INIT([hello],[1.1])\n"})
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert r.risk == "low" and r.hits == [] and r.tier == "mechanical"
    assert r.build_system_other == ["configure.ac"]
    assert "not counted as risk: configure.ac" in merge.render(r)


def test_build_system_file_a_delta_patch_modifies_is_high_risk(prof, tmp_path, world):
    work = tmp_path / "work"
    write(work, "debian/patches/acme.patch", "--- a/configure.ac\n+++ b/configure.ac\n@@ -1 +1 @@\n-AC_INIT([hello],[1.0])\n+AC_INIT([hello],[1.0-acme])\n")
    write(work, "debian/patches/series", "a.patch\nb.patch\nacme.patch\n")
    commit(work, "acme patch on configure.ac")
    git(work, "push", "-q", "origin", "acme/resolute")
    pkg, repo = prepare(prof, tmp_path, world, {"configure.ac": "AC_INIT([hello],[1.1])\n"})
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert r.risk == "high" and [(h.kind, h.path) for h in r.hits] == [("build-system", "configure.ac")]
    assert r.build_system_other == []
    delta = merge.delta_files(repo, "old/acme")
    assert {"configure.ac", "debian/rules", "debian/patches/acme.patch"} <= delta
    assert "a.patch" not in delta and "debian/patches/a.patch" not in delta


def test_without_the_previous_archive_version_every_build_system_file_counts(prof, tmp_path, world):
    pkg, repo = prepare(prof, tmp_path, world, {"configure.ac": "AC_INIT([hello],[1.1])\n"})
    assert merge.delta_files(repo, "acme/resolute") is None
    git(repo, "tag", "-d", "pkg/import/1.0-1ubuntu1")
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert r.risk == "high" and [h.kind for h in r.hits] == ["build-system"]
    assert any("old/ubuntu is not set" in w for w in r.warnings)


def test_small_conflict_outside_risk_regions_is_ai_assisted_candidate(prof, tmp_path, world):
    pkg, repo = prepare(prof, tmp_path, world, {"debian/control": CONTROL_V1.replace("ubuntu-devel-discuss", "other-list")})
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert not r.merged and r.risk == "low" and r.tier == "ai-assisted"
    assert [c.path for c in r.conflicts] == ["debian/control"]


def test_conflict_in_the_toolchain_block_is_high_risk_and_within_the_high_cap(prof, tmp_path, world):
    rules = RULES_V1.replace("hardening=+all", "hardening=+all,-pie")
    pkg, repo = prepare(prof, tmp_path, world, {"debian/rules": rules})
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert not r.merged and r.risk == "high"
    assert [c.path for c in r.conflicts] == ["debian/rules"]
    assert r.tier == "ai-assisted"


def test_abort_restores_the_downstream_branch(prof, tmp_path, world):
    pkg, repo = prepare(prof, tmp_path, world, {"debian/control": CONTROL_V1.replace("ubuntu-devel-discuss", "other-list")})
    merge.merge_start(prof, pkg, "resolute", repo, V2)
    done = merge.abort(prof, pkg, "resolute", repo)
    assert "merge aborted" in done and gitutil.current_branch(repo) == "acme/resolute"
    assert gitutil.rev("refs/heads/acme-proposed/resolute", repo) is None
    assert gitutil.rev("old/acme", repo) is None and gitutil.is_clean(repo)


def test_refusals(prof, tmp_path, world):
    pkg, repo = prepare(prof, tmp_path, world, {})
    (repo / "dirty").write_text("x")
    with pytest.raises(merge.Refused, match="not clean"):
        merge.merge_start(prof, pkg, "resolute", repo, V2)
    (repo / "dirty").unlink()
    git(repo, "branch", "-f", "ubuntu/resolute", "pkg/import/1.0-1ubuntu1")
    with pytest.raises(merge.Refused, match="git ppa sync"):
        merge.merge_start(prof, pkg, "resolute", repo, V2)
    workspace.sync_local(prof, pkg, "resolute", repo, V2)
    merge.merge_start(prof, pkg, "resolute", repo, V2)
    git(repo, "checkout", "-q", "acme/resolute")
    with pytest.raises(merge.Refused, match="--force"):
        merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert merge.merge_start(prof, pkg, "resolute", repo, V2, force=True).merged


def test_up_to_date_after_the_merge_is_in_the_downstream(prof, tmp_path, world):
    pkg, repo = prepare(prof, tmp_path, world, {})
    merge.merge_start(prof, pkg, "resolute", repo, V2)
    git(repo, "checkout", "-q", "acme/resolute")
    git(repo, "merge", "-q", "--ff-only", "acme-proposed/resolute")
    git(repo, "branch", "-q", "-D", "acme-proposed/resolute")
    with pytest.raises(merge.UpToDate):
        merge.merge_start(prof, pkg, "resolute", repo, V2)


def _series_conflict(prof, tmp_path, world, ours: str, theirs: str):
    work = tmp_path / "work"
    write(work, "debian/patches/series", ours)
    commit(work, "series on our side")
    git(work, "push", "-q", "origin", "acme/resolute")
    return prepare(prof, tmp_path, world, {"debian/patches/series": theirs})


def test_a_series_conflict_that_is_a_plain_list_merge_is_resolved_and_reported(prof, tmp_path, world):
    pkg, repo = _series_conflict(prof, tmp_path, world, "a.patch\nb.patch\nacme.patch\n", "b.patch\ncve-1.patch\n")
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert r.merged and r.conflicts == [] and r.tier == "mechanical" and r.risk == "low"
    assert r.merge_commit and len(git(repo, "log", "-1", "--format=%P").split()) == 2
    assert (repo / "debian/patches/series").read_text() == "b.patch\nacme.patch\ncve-1.patch\n"
    assert len(r.resolved) == 1 and "ours +1 -0, theirs +1 -1" in r.resolved[0]
    assert "resolved  debian/patches/series" in merge.render(r)
    assert gitutil.is_clean(repo)


def test_a_series_conflict_the_tool_cannot_judge_stays_a_conflict(prof, tmp_path, world):
    pkg, repo = _series_conflict(prof, tmp_path, world, "a2.patch\nb.patch\nacme.patch\n", "a3.patch\nb.patch\ncve-1.patch\n")
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert not r.merged and r.resolved == []
    assert [c.path for c in r.conflicts] == ["debian/patches/series"]
    assert "<<<<<<<" in (repo / "debian/patches/series").read_text()


def test_series_resolved_but_another_file_still_conflicts(prof, tmp_path, world):
    work = tmp_path / "work"
    write(work, "debian/patches/series", "a.patch\nb.patch\nacme.patch\n")
    commit(work, "series on our side")
    git(work, "push", "-q", "origin", "acme/resolute")
    pkg, repo = prepare(prof, tmp_path, world, {
        "debian/patches/series": "b.patch\ncve-1.patch\n",
        "debian/control": CONTROL_V1.replace("ubuntu-devel-discuss", "other-list"),
    })
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert not r.merged and len(r.resolved) == 1
    assert [c.path for c in r.conflicts] == ["debian/control"]
    assert (repo / "debian/patches/series").read_text() == "b.patch\nacme.patch\ncve-1.patch\n"


def _finish_profile(prof, monkeypatch, used=frozenset()):
    from git_ppa import lp
    prof.changelog_name, prof.changelog_email = "Acme Bot", "bot@example.com"
    prof.ppa_owner = "acme"
    pkg = prof.package("hello-acme")
    object.__setattr__(pkg, "ppa", "acme-proposed")
    calls = []
    monkeypatch.setattr(lp, "ppa_versions", lambda ref, name: calls.append((ref, name)) or set(used))
    return pkg, calls


def test_merge_finish_adds_the_entry_lints_and_commits(prof, tmp_path, world, monkeypatch):
    pkg, repo = prepare(prof, tmp_path, world, {})
    assert merge.merge_start(prof, pkg, "resolute", repo, V2).merged
    _, calls = _finish_profile(prof, monkeypatch)
    f = merge.merge_finish(prof, pkg, "resolute", repo)
    assert calls == [("~acme/ubuntu/acme-proposed", "hello-acme")]
    assert (f.target, f.previous, f.version, f.kept_base) == (V2, "1.0-1ubuntu1+acme1", "1.0-1ubuntu1.1+acme1", False)
    top = (repo / "debian/changelog").read_text().splitlines()
    assert top[0] == "hello-acme (1.0-1ubuntu1.1+acme1) resolute; urgency=medium"
    assert "  * Merge 1.0-1ubuntu1.1 from the Ubuntu archive." in top
    assert "  * It includes the fixes for CVE-2026-0001." in top
    assert git(repo, "log", "-1", "--format=%an <%ae>|%s") == "Acme Bot <bot@example.com>|hello-acme 1.0-1ubuntu1.1+acme1"
    assert gitutil.is_clean(repo) and f.commit
    from git_ppa import lint
    assert [x for x in lint.run(repo, prof, pkg) if x.severity == "error"] == []
    with pytest.raises(merge.Refused, match="already an entry 1.0-1ubuntu1.1\\+acme1 after"):
        merge.merge_finish(prof, pkg, "resolute", repo)


def test_merge_finish_keeps_the_base_when_the_archive_appended_letters(prof, tmp_path, world, monkeypatch):
    entry = NEW_ENTRY.replace("1.0-1ubuntu1.1", "1.0-1ubuntu1build1")
    new_version(world, {"debian/changelog": entry + CHANGELOG_V1}, "1.0-1ubuntu1build1")
    pkg = prof.package("hello-acme")
    repo = workspace.clone(prof, pkg, "resolute", "lp", tmp_path / "ws")
    workspace.sync_local(prof, pkg, "resolute", repo, "1.0-1ubuntu1build1")
    assert merge.merge_start(prof, pkg, "resolute", repo, "1.0-1ubuntu1build1").merged
    _finish_profile(prof, monkeypatch, used={"1.0-1ubuntu1+acme2"})
    f = merge.merge_finish(prof, pkg, "resolute", repo)
    assert (f.version, f.kept_base) == ("1.0-1ubuntu1+acme3", True)
    text = (repo / "debian/changelog").read_text()
    assert "keeps the base 1.0-1ubuntu1, 1.0-1ubuntu1build1+acme1 would sort below the previous version" in " ".join(text.split())
    # the base is read from the changelog, so status and the next merge see 1.0-1ubuntu1build1, not 1.0-1ubuntu1
    assert versions.archive_base(text, "hello", "acme") == "1.0-1ubuntu1build1"


def test_merge_finish_dry_run_writes_nothing(prof, tmp_path, world, monkeypatch):
    pkg, repo = prepare(prof, tmp_path, world, {})
    merge.merge_start(prof, pkg, "resolute", repo, V2)
    _finish_profile(prof, monkeypatch)
    head = gitutil.rev("HEAD", repo)
    f = merge.merge_finish(prof, pkg, "resolute", repo, dry_run=True)
    assert f.commit is None and f.entry.startswith("hello-acme (1.0-1ubuntu1.1+acme1)")
    assert gitutil.rev("HEAD", repo) == head and gitutil.is_clean(repo)
    assert "dry run   nothing written" in merge.render_finish(f)


def test_merge_finish_refuses_before_the_merge_is_committed_and_without_an_identity(prof, tmp_path, world, monkeypatch):
    pkg, repo = prepare(prof, tmp_path, world, {"debian/control": CONTROL_V1.replace("ubuntu-devel-discuss", "x")})
    assert not merge.merge_start(prof, pkg, "resolute", repo, V2).merged
    with pytest.raises(merge.Refused, match="not committed"):
        merge.merge_finish(prof, pkg, "resolute", repo)
    merge.abort(prof, pkg, "resolute", repo)
    with pytest.raises(merge.Refused, match="run merge finish on acme-proposed/resolute"):
        merge.merge_finish(prof, pkg, "resolute", repo)


def test_merge_finish_without_an_identity_needs_me(prof, tmp_path, world, monkeypatch):
    pkg, repo = prepare(prof, tmp_path, world, {})
    merge.merge_start(prof, pkg, "resolute", repo, V2)
    monkeypatch.delenv("DEBFULLNAME", raising=False)
    monkeypatch.delenv("DEBEMAIL", raising=False)
    with pytest.raises(merge.Refused, match="no \\[changelog\\] name and email"):
        merge.merge_finish(prof, pkg, "resolute", repo, skip_ppa_check=True)
    monkeypatch.setenv("DEBFULLNAME", "Me")
    monkeypatch.setenv("DEBEMAIL", "me@example.com")
    f = merge.merge_finish(prof, pkg, "resolute", repo, me=True, skip_ppa_check=True)
    assert "Me <me@example.com>" in f.entry
    assert any("no target PPA" in w for w in f.warnings)


def test_merge_finish_stops_on_a_lint_error_the_merge_brought_and_keeps_old_ones(prof, tmp_path, world, monkeypatch):
    # the archive adds a binary package, which our delta does not rename: a new binary-rename error
    control = CONTROL_V1 + "\nPackage: hello-extra\nArchitecture: all\nDescription: extra\n x\n"
    pkg, repo = prepare(prof, tmp_path, world, {"debian/control": control})
    assert merge.merge_start(prof, pkg, "resolute", repo, V2).merged
    _finish_profile(prof, monkeypatch)
    head = gitutil.rev("HEAD", repo)
    with pytest.raises(merge.LintFailed) as e:
        merge.merge_finish(prof, pkg, "resolute", repo)
    assert any("hello-extra" in x.message for x in e.value.findings)
    assert gitutil.rev("HEAD", repo) == head and gitutil.is_clean(repo)


def test_merge_finish_carries_lint_errors_that_were_there_before(prof, tmp_path, world, monkeypatch):
    work = tmp_path / "work"
    write(work, "debian/hello.install", "usr/bin/*\n")   # an old, already wrong debhelper file
    commit(work, "leftover file")
    git(work, "push", "-q", "origin", "acme/resolute")
    pkg, repo = prepare(prof, tmp_path, world, {})
    assert merge.merge_start(prof, pkg, "resolute", repo, V2).merged
    _finish_profile(prof, monkeypatch)
    f = merge.merge_finish(prof, pkg, "resolute", repo)
    assert f.commit and any("hello.install" in c for c in f.carried)
