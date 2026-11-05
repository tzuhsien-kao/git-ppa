# SPDX-License-Identifier: GPL-3.0-or-later
"""update-package: the steps in order, resuming, the review gate, and the spec options it refuses."""

import contextlib
import json
from dataclasses import dataclass

import pytest

from conftest import CHANGELOG_V1, CONTROL_V1, NEW_ENTRY, git, new_version
from git_ppa import build as build_mod, cli, lp, merge, signing, update, upload as upload_mod

V2 = "1.0-1ubuntu1.1"
STAGING = "~acme/ubuntu/acme-staging"


@dataclass
class FakeBuilt:
    changes: str

    def to_dict(self):
        return {"changes": self.changes}


@pytest.fixture
def env(prof, tmp_path, monkeypatch):
    """The fixture world with Launchpad, the signing, the build and the upload replaced by recorders."""
    prof.changelog_name, prof.changelog_email = "Acme Bot", "bot@example.com"
    prof.ppa_owner = "acme"
    prof.allow_ppas = [STAGING]
    pkg = prof.package("hello-acme")
    object.__setattr__(pkg, "ppa", "acme-proposed")
    calls = {"ppa_versions": [], "build": 0, "upload": [], "wait": []}
    used: dict[str, set] = {}
    states = []

    def ppa_versions(ref, name):
        calls["ppa_versions"].append(ref)
        return set(used.get(ref, set()))

    def fake_build(prof_, pkg_, series, repo, key=None):
        calls["build"] += 1
        assert key == "KEY"
        return FakeBuilt(str(repo.parent / "x.changes"))

    def fake_wait(ppa, source, version, seconds, log=print):
        calls["wait"].append((ppa, version))
        return states.pop(0) if states else {"status": "Published", "builds": [{"arch": "amd64", "state": "Successfully built", "link": "l"}]}

    monkeypatch.setattr(update, "_newest_target", lambda *a: V2)
    monkeypatch.setattr(lp, "ppa_versions", ppa_versions)
    monkeypatch.setattr(signing, "read_secret", lambda path: b"secret:" + path.encode())
    monkeypatch.setattr(signing, "Keyring", lambda secret, pw: contextlib.nullcontext("KEY"))
    monkeypatch.setattr(build_mod, "build", fake_build)
    monkeypatch.setattr(upload_mod, "load_private_key", lambda data, pw: "SSH")
    monkeypatch.setattr(upload_mod, "upload", lambda changes, ppa, user, key: calls["upload"].append((changes.name, ppa, user, key)))
    monkeypatch.setattr(upload_mod, "wait", fake_wait)
    opts = update.Options(pkg_name="hello-acme", ppa_name="acme-staging", sign_key_file="sign", ssh_key_file="ssh")
    return pkg, opts, calls, used, states


def go(prof, pkg, opts, workdir, resume=False):
    return update.run(prof, pkg, "resolute", opts, workdir, resume, "bot", log=lambda m: None)


def test_a_mechanical_update_runs_every_step_and_stops_for_review(prof, tmp_path, world, env):
    pkg, opts, calls, used, _ = env
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1})
    used[STAGING] = {"1.0-1ubuntu1.1+acme1"}
    r = go(prof, pkg, opts, tmp_path / "ws" / "hello-acme")
    assert r.code == 0 and r.done == ["clone", "merge", "finish", "build", "upload", "wait"]
    assert r.merge == {"tier": "mechanical", "risk": "low", "conflicts": 0}
    # the versions of the target and of the staging PPA both count, acme1 is taken in staging
    assert r.version == "1.0-1ubuntu1.1+acme2"
    assert "~acme/ubuntu/acme-proposed" in calls["ppa_versions"] and STAGING in calls["ppa_versions"]
    assert calls["upload"] == [("hello-acme_1.0-1ubuntu1.1+acme2_source.changes", STAGING, "bot", "SSH")]
    assert "waiting for a review before promote to ~acme/ubuntu/acme-proposed" in r.stopped
    state = json.loads((tmp_path / "ws/hello-acme/.git/git-ppa-update.json").read_text())
    assert state["done"][-1] == "wait" and state["version"] == r.version


def test_without_manual_review_it_says_promote_is_missing(prof, tmp_path, world, env):
    pkg, opts, *_ = env
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1})
    opts.manual_review = False
    r = go(prof, pkg, opts, tmp_path / "w")
    assert r.code == 0 and "promote to ~acme/ubuntu/acme-proposed is not implemented yet" in r.stopped


def test_a_build_still_running_stops_with_5_and_continue_does_not_upload_again(prof, tmp_path, world, env):
    pkg, opts, calls, used, states = env
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1})
    states.append({"status": "Pending", "builds": [{"arch": "amd64", "state": "Uploading build", "link": "l"}]})
    w = tmp_path / "w"
    r = go(prof, pkg, opts, w)
    assert r.code == update.BUILDING and r.done[-1] == "upload" and "--continue" in r.stopped
    r = go(prof, pkg, opts, w, resume=True)
    assert r.code == 0 and r.done[-1] == "wait"
    assert len(calls["upload"]) == 1 and calls["build"] == 1 and len(calls["wait"]) == 2


def test_a_failed_build_in_the_staging_ppa_is_an_error_with_the_link(prof, tmp_path, world, env):
    pkg, opts, _, _, states = env
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1})
    states.append({"status": "Published", "builds": [{"arch": "amd64", "state": "Failed to build", "link": "https://b/1"}]})
    r = go(prof, pkg, opts, tmp_path / "w")
    assert r.code == 1 and "amd64 Failed to build https://b/1" in r.stopped


def test_an_upload_already_in_the_staging_ppa_is_not_sent_again(prof, tmp_path, world, env, monkeypatch):
    pkg, opts, calls, used, _ = env
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1})
    w = tmp_path / "w"
    monkeypatch.setattr(upload_mod, "upload", lambda *a: (_ for _ in ()).throw(upload_mod.UploadError("network gone")))
    with pytest.raises(upload_mod.UploadError):
        go(prof, pkg, opts, w)
    # the upload reached Launchpad before the connection broke
    used[STAGING] = {"1.0-1ubuntu1.1+acme1"}
    r = go(prof, pkg, opts, w, resume=True)
    assert r.code == 0 and r.done[-2:] == ["upload", "wait"] and r.version == "1.0-1ubuntu1.1+acme1"


def test_conflicts_stop_with_3_and_continue_after_the_commit(prof, tmp_path, world, env):
    pkg, opts, calls, *_ = env
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1,
                        "debian/control": CONTROL_V1.replace("ubuntu-devel-discuss", "other-list")})
    w = tmp_path / "w"
    r = go(prof, pkg, opts, w)
    assert r.code == 3 and r.merge["tier"] == "ai-assisted" and r.done == ["clone"]
    r = go(prof, pkg, opts, w, resume=True)
    assert r.code == 3 and "resolve and commit" in r.stopped
    # keep our control, the archive one has the unrenamed package and lint would stop finish
    git(w, "checkout", "--ours", "debian/control")
    git(w, "add", "debian/control")
    git(w, "commit", "-q", "--no-edit")
    r = go(prof, pkg, opts, w, resume=True)
    assert r.code == 0 and r.done[-1] == "wait" and calls["build"] == 1


def test_a_fresh_run_refuses_an_existing_directory_and_continue_checks_the_run(prof, tmp_path, world, env):
    pkg, opts, *_ = env
    (tmp_path / "w").mkdir()
    with pytest.raises(update.UpdateError, match="--continue"):
        go(prof, pkg, opts, tmp_path / "w")
    git(tmp_path / "w", "init", "-q")
    with pytest.raises(update.UpdateError, match="not started by update-package"):
        go(prof, pkg, opts, tmp_path / "w", resume=True)


def test_the_staging_ppa_must_be_allowed_and_not_the_target(prof, tmp_path, env):
    pkg, opts, *_ = env
    opts.ppa_name = "other"
    with pytest.raises(merge.Refused, match="allow_ppas"):
        go(prof, pkg, opts, tmp_path / "w")
    prof.allow_ppas.append("~acme/ubuntu/acme-proposed")
    opts.ppa_name = "acme-proposed"
    with pytest.raises(merge.Refused, match="target PPA"):
        go(prof, pkg, opts, tmp_path / "w")


@pytest.mark.parametrize("change, message", [
    ({"ai_model": "m", "review_model": "m"}, "must not be the same"),
    ({"ai_model": "m"}, "--ai-model is not supported"),
    ({"review_model": "m"}, "--review-model is not supported"),
    ({"budget": "10"}, "--budget is not supported"),
    ({"bug": "lp:1"}, "--bug is not supported"),
    ({"ppa_dependencies": "x"}, "--ppa-dependencies is not supported"),
    ({"base": "salsa"}, "--base salsa"),
    ({"source": "watch"}, "--from watch"),
    ({"build": ["local", "ppa"]}, "--build local,ppa"),
    ({"ppa_name": ""}, "--ppa-name is needed"),
    ({"sign_key_file": "-", "ssh_key_file": "-"}, "only one of"),
])
def test_spec_options_git_ppa_does_not_have_are_refused(change, message):
    o = update.Options(pkg_name="p", ppa_name="s", **{k: v for k, v in change.items() if k != "ppa_name"})
    if "ppa_name" in change:
        o.ppa_name = change["ppa_name"]
    with pytest.raises(update.UpdateError, match=message):
        update.check(o)


def test_config_yaml_uses_the_option_names(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("pkg-name: hello-acme\nppa_name: s\nfrom: archive\nbuild: ppa\nmanual-review: false\nskillset: p.toml\ncontinue: w\n")
    assert update.load_config(f) == {"pkg_name": "hello-acme", "ppa_name": "s", "source": "archive", "build": ["ppa"],
                                     "manual_review": False, "profile": "p.toml", "workdir": "w"}
    f.write_text("pkg-nmae: x\n")
    with pytest.raises(update.UpdateError, match="unknown option 'pkg-nmae'"):
        update.load_config(f)


def test_the_cli_reads_config_yaml_and_the_command_line_wins(tmp_path, monkeypatch, capsys):
    prof_file = tmp_path / "p.toml"
    prof_file.write_text('[project]\ntag = "acme"\nseries = ["resolute"]\n[repo]\nurl = "x/{name}"\nupstream_url = "y/{archive}"\n[branches]\nmirror = "ubuntu/{series}"\ndownstream = "{tag}/{series}"\nstaging = "{tag}-proposed/{series}"\n'
                         '[ppa]\nowner = "acme"\n[safety]\nallow_ppas = ["~acme/ubuntu/s2"]\n[[package]]\nname = "hello-acme"\narchive = "hello"\n')
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"pkg-name: hello-acme\nppa-name: s1\nskillset: {prof_file}\nmanual-review: false\nlp-user: bot\nwait: 10\n")
    seen = {}

    def fake_run(prof, pkg, series, o, workdir, resume, lpuser, log):
        seen.update(o=o, workdir=workdir, resume=resume, lpuser=lpuser, series=series)
        return update.Result(pkg.name, str(workdir), stopped="ok")

    monkeypatch.setattr(update, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["update-package", str(cfg), "--ppa-name", "s2"]) == 0
    o = seen["o"]
    assert (o.ppa_name, o.manual_review, o.wait, seen["lpuser"], seen["resume"]) == ("s2", False, 10, "bot", False)
    assert seen["workdir"] == tmp_path / "hello-acme" and seen["series"] == "resolute"
    assert cli.main(["update-package", str(cfg), "--ai-model", "x"]) == 1
    assert "--ai-model is not supported" in capsys.readouterr().err


def test_skillset_by_name_is_a_profile_in_the_skillsets_directory(tmp_path, monkeypatch, capsys):
    d = tmp_path / "skillsets"
    d.mkdir()
    (d / "acme.toml").write_text('[project]\ntag = "acme"\nseries = ["resolute"]\n[repo]\nurl = "x/{name}"\nupstream_url = "y"\n[branches]\nmirror = "ubuntu/{series}"\ndownstream = "{tag}/{series}"\nstaging = "{tag}-proposed/{series}"\n'
                                 '[[package]]\nname = "hello-acme"\narchive = "hello"\n')
    monkeypatch.setenv("GIT_PPA_SKILLSETS", str(d))
    monkeypatch.setattr(update, "run", lambda prof, pkg, *a, **k: update.Result(pkg.name, "w", stopped=prof.tag))
    assert cli.main(["update-package", "--skillset", "acme", "--pkg-name", "hello-acme", "--ppa-name", "~a/ubuntu/s", "--lp-user", "u"]) == 0
    assert "acme" in capsys.readouterr().out


def test_a_clone_that_fails_leaves_no_directory_so_the_run_can_start_again(prof, tmp_path, world, env, monkeypatch):
    from git_ppa import gitutil, workspace
    pkg, opts, *_ = env
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1})
    real = workspace.clone

    def broken(prof_, pkg_, series, user, dest):
        dest.mkdir(parents=True)
        raise gitutil.GitError(["fetch", "pkg"], 128, "HTTP 504")

    monkeypatch.setattr(workspace, "clone", broken)
    w = tmp_path / "w"
    with pytest.raises(gitutil.GitError):
        go(prof, pkg, opts, w)
    assert not w.exists()
    monkeypatch.setattr(workspace, "clone", real)
    assert go(prof, pkg, opts, w).code == 0
