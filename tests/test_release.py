# SPDX-License-Identifier: GPL-3.0-or-later
"""release: the human step. Nothing is signed or uploaded for a tree that was not approved."""

import contextlib

import pytest

from conftest import git
from git_ppa import build as build_mod, cli, gitutil, lp, merge, release, signing, upload as upload_mod, workspace

PPA = "~acme/ubuntu/acme-proposed"


class Built:
    def __init__(self, changes):
        self.changes = changes


@pytest.fixture
def env(prof, tmp_path, world, monkeypatch):
    """A downstream checkout whose top entry is ours, Launchpad, signing, build and upload replaced by recorders."""
    prof.ppa_owner = "acme"
    prof.allow_ppas = [PPA]
    pkg = prof.package("hello-acme")
    repo = workspace.clone(prof, pkg, "resolute", "lp", tmp_path / "ws" / "hello-acme")
    calls = {"keys": [], "build": [], "upload": [], "wait": 0}
    used: set[str] = set()
    states = []

    def read_secret(path):
        calls["keys"].append(path)
        return b"secret:" + path.encode()

    def fake_wait(ppa, source, version, seconds, log=print):
        calls["wait"] += 1
        return states.pop(0) if states else {"status": "Published", "builds": [{"arch": "amd64", "state": "Successfully built", "link": "l"}]}

    monkeypatch.setattr(lp, "ppa_versions", lambda ref, name: set(used))
    monkeypatch.setattr(signing, "read_secret", read_secret)
    monkeypatch.setattr(signing, "Keyring", lambda secret, pw: contextlib.nullcontext(secret.decode()))
    monkeypatch.setattr(build_mod, "build", lambda p, k, s, r, key=None: calls["build"].append(key) or Built("x"))
    monkeypatch.setattr(upload_mod, "load_private_key", lambda data, pw: "SSH")
    monkeypatch.setattr(upload_mod, "upload", lambda changes, ppa, user, key: calls["upload"].append((changes.name, ppa, user, key)))
    monkeypatch.setattr(upload_mod, "wait", fake_wait)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    return pkg, repo, tree, calls, used, states


def go(prof, env, approve="tree", sign="sign.key", ssh="ssh.key", ppa=PPA, wait=60):
    pkg, repo, tree, *_ = env
    return release.release(prof, pkg, "resolute", repo, ppa, tree[:12] if approve == "tree" else approve, sign, ssh, wait, "bot", log=lambda m: None)


def test_release_signs_the_approved_tree_uploads_it_and_waits(prof, env):
    _, repo, tree, calls, *_ = env
    r = go(prof, env)
    assert (r.code, r.uploaded, r.tree, r.version) == (0, True, tree, "1.0-1ubuntu1+acme1")
    assert calls["keys"] == ["sign.key", "ssh.key"] and calls["build"] == ["secret:sign.key"]
    assert calls["upload"] == [("hello-acme_1.0-1ubuntu1+acme1_source.changes", PPA, "bot", "SSH")]
    assert r.message == f"1.0-1ubuntu1+acme1 is built in {PPA}"


@pytest.mark.parametrize("approve", [None, "", "short", "0" * 12])
def test_nothing_is_signed_for_a_tree_that_was_not_approved_and_no_key_is_read(prof, env, approve):
    calls = env[3]
    with pytest.raises(merge.Refused, match="--approve"):
        go(prof, env, approve=approve)
    assert calls["keys"] == [] and calls["build"] == [] and calls["upload"] == []


def test_the_approval_is_for_the_committed_tree_so_a_changed_checkout_is_refused(prof, env):
    pkg, repo, tree, calls, *_ = env
    (repo / "debian/rules").write_text((repo / "debian/rules").read_text() + "\n# slipped in\n")
    with pytest.raises(merge.Refused, match="not committed"):
        go(prof, env)
    git(repo, "commit", "-qam", "slipped in")
    with pytest.raises(merge.Refused, match="--approve"):
        go(prof, env)
    assert calls["keys"] == [] and calls["upload"] == []


def test_the_ppa_must_be_in_the_allow_list(prof, env):
    prof.allow_ppas = []
    with pytest.raises(merge.Refused, match="allow_ppas"):
        go(prof, env)
    assert env[3]["keys"] == []


def test_a_tree_that_changes_while_it_is_built_is_not_uploaded(prof, env, monkeypatch):
    repo, calls = env[1], env[3]

    def sneaky(p, k, s, r, key=None):
        (r / "debian/rules").write_text("changed\n")
        git(r, "commit", "-qam", "changed")
        return Built("x")

    monkeypatch.setattr(build_mod, "build", sneaky)
    with pytest.raises(merge.Refused, match="changed while it was being built"):
        go(prof, env)
    assert calls["upload"] == []


def test_both_keys_are_needed_and_only_one_may_come_from_standard_input(prof, env):
    with pytest.raises(release.ReleaseError, match="--sign-key-file"):
        go(prof, env, sign=None)
    with pytest.raises(release.ReleaseError, match="--ssh-key-file"):
        go(prof, env, ssh=None)
    with pytest.raises(release.ReleaseError, match="only one of"):
        go(prof, env, sign="-", ssh="-")
    assert env[3]["keys"] == []


def test_a_version_already_in_the_ppa_only_waits_and_needs_no_key(prof, env):
    calls, used = env[3], env[4]
    used.add("1.0-1ubuntu1+acme1")
    r = go(prof, env, sign=None, ssh=None)
    assert (r.code, r.uploaded) == (0, False)
    assert calls["keys"] == [] and calls["build"] == [] and calls["upload"] == [] and calls["wait"] == 1


def test_a_build_still_running_is_exit_5_and_the_same_command_goes_on(prof, env):
    calls, used, states = env[3], env[4], env[5]
    states.append({"status": "Pending", "builds": [{"arch": "amd64", "state": "Uploading build", "link": "l"}]})
    r = go(prof, env)
    assert (r.code, r.uploaded) == (release.BUILDING, True) and "no key is needed any more" in r.message
    used.add(r.version)
    r = go(prof, env, sign=None, ssh=None)
    assert r.code == 0 and len(calls["upload"]) == 1 and calls["wait"] == 2


def test_a_failed_build_is_exit_1_with_the_link(prof, env):
    env[5].append({"status": "Published", "builds": [{"arch": "amd64", "state": "Failed to build", "link": "https://b/1"}]})
    r = go(prof, env)
    assert r.code == 1 and "amd64 Failed to build https://b/1" in r.message


def test_a_checkout_without_an_entry_of_ours_is_refused(prof, env):
    pkg, repo, *_ = env
    object.__setattr__(pkg, "name", "other")
    with pytest.raises(release.ReleaseError, match="run merge finish first"):
        go(prof, env)


def test_the_cli_passes_the_options_and_takes_the_ppa_of_the_package(prof, env, monkeypatch):
    pkg, repo, tree, *_ = env
    object.__setattr__(pkg, "ppa", "acme-proposed")
    seen = {}

    def fake(prof_, pkg_, series, repo_, ppa, approve, sign, ssh, wait, lpuser, log):
        seen.update(pkg=pkg_.name, ppa=ppa, approve=approve, sign=sign, ssh=ssh, wait=wait, lpuser=lpuser)
        return release.Released(pkg_.name, "v", ppa, tree, code=5, message="m")

    monkeypatch.setattr(cli, "_profile", lambda args: prof)
    monkeypatch.setattr(release, "release", fake)
    argv = ["release", "-C", str(repo), "--approve", tree[:12], "--sign-key-file", "-", "--ssh-key-file", "k", "--lp-user", "bot"]
    assert cli.main(argv) == 5
    assert seen == {"pkg": "hello-acme", "ppa": PPA, "approve": tree[:12], "sign": "-", "ssh": "k", "wait": 0, "lpuser": "bot"}
    assert cli.main(argv + ["--ppa", "other"]) == 5 and seen["ppa"] == "~acme/ubuntu/other"


def test_by_default_nothing_waits_for_the_build_after_the_upload(prof, env):
    calls = env[3]
    r = go(prof, env, wait=0)
    assert (r.code, r.uploaded) == (0, True) and calls["wait"] == 0
    assert "the build is not awaited" in r.message and "https://launchpad.net/~acme/+archive/ubuntu/acme-proposed" in r.message


def test_without_waiting_a_version_already_there_is_looked_at_once(prof, env):
    calls, used, states = env[3], env[4], env[5]
    used.add("1.0-1ubuntu1+acme1")
    states.append({"status": "Pending", "builds": [{"arch": "amd64", "state": "Currently building", "link": "l"}]})
    r = go(prof, env, sign=None, ssh=None, wait=0)
    assert (r.code, r.uploaded, calls["wait"]) == (0, False, 1) and "follow it at" in r.message
    states.append({"status": "Published", "builds": [{"arch": "amd64", "state": "Failed to build", "link": "https://b/1"}]})
    assert go(prof, env, sign=None, ssh=None, wait=0).code == 1


def test_a_private_ppa_that_cannot_be_read_is_uploaded_to_as_if_the_version_were_new(prof, env, monkeypatch):
    calls = env[3]

    def unreadable(ref, name):
        raise lp.LaunchpadError("HTTP 404")

    monkeypatch.setattr(lp, "ppa_versions", unreadable)
    r = go(prof, env, wait=0)
    assert (r.code, r.uploaded) == (0, True) and len(calls["upload"]) == 1
