# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole flow on local fixture repositories: clone, sync, merge start."""

import pytest

from conftest import CHANGELOG_V1, CONTROL_V1, NEW_ENTRY, RULES_V1, git, new_version
from git_ppa import gitutil, merge, workspace

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


def test_build_system_file_change_is_high_risk(prof, tmp_path, world):
    pkg, repo = prepare(prof, tmp_path, world, {"configure.ac": "AC_INIT([hello],[1.1])\n"})
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert r.risk == "high" and [h.kind for h in r.hits] == ["build-system"]


def test_small_conflict_outside_risk_regions_is_ai_assisted_candidate(prof, tmp_path, world):
    pkg, repo = prepare(prof, tmp_path, world, {"debian/control": CONTROL_V1.replace("ubuntu-devel-discuss", "other-list")})
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert not r.merged and r.risk == "low" and r.tier == "ai-assisted"
    assert [c.path for c in r.conflicts] == ["debian/control"]


def test_conflict_in_the_toolchain_block_is_high_risk_and_over_the_cap(prof, tmp_path, world):
    rules = RULES_V1.replace("hardening=+all", "hardening=+all,-pie")
    pkg, repo = prepare(prof, tmp_path, world, {"debian/rules": rules})
    r = merge.merge_start(prof, pkg, "resolute", repo, V2)
    assert not r.merged and r.risk == "high"
    assert [c.path for c in r.conflicts] == ["debian/rules"]
    assert r.tier == "re-port"


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
