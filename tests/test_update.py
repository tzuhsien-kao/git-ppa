# SPDX-License-Identifier: GPL-3.0-or-later
"""update-package (prepare, no keys) and release (the only place a key is read), to staging and to production."""

import contextlib
import json
from dataclasses import dataclass

import pytest

from conftest import CHANGELOG_V1, CONTROL_V1, NEW_ENTRY, git, new_version
from git_ppa import build as build_mod, cli, gitutil, lp, merge, signing, update, upload as upload_mod, workspace

V2 = "1.0-1ubuntu1.1"
STAGING = "~acme/ubuntu/acme-staging"
PRODUCTION = "~acme/ubuntu/acme-proposed"


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
    prof.allow_ppas = [STAGING, PRODUCTION]
    prof.production_ppas = [PRODUCTION]
    pkg = prof.package("hello-acme")
    object.__setattr__(pkg, "ppa", "acme-proposed")
    calls = {"ppa_versions": [], "build": [], "upload": [], "wait": [], "read_secret": []}
    used: dict[str, set] = {}
    states = []

    def ppa_versions(ref, name):
        calls["ppa_versions"].append(ref)
        return set(used.get(ref, set()))

    def fake_build(prof_, pkg_, series, repo, key=None):
        calls["build"].append(key)
        return FakeBuilt(str(repo.parent / "x.changes"))

    def fake_wait(ppa, source, version, seconds, log=print):
        calls["wait"].append((ppa, version))
        return states.pop(0) if states else {"status": "Published", "builds": [{"arch": "amd64", "state": "Successfully built", "link": "l"}]}

    def read_secret(path):
        calls["read_secret"].append(path)
        return b"secret:" + path.encode()

    monkeypatch.setattr(update, "_newest_target", lambda *a: V2)
    monkeypatch.setattr(lp, "ppa_versions", ppa_versions)
    monkeypatch.setattr(signing, "read_secret", read_secret)
    monkeypatch.setattr(signing, "Keyring", lambda secret, pw: contextlib.nullcontext(secret.decode()))
    monkeypatch.setattr(build_mod, "build", fake_build)
    monkeypatch.setattr(upload_mod, "load_private_key", lambda data, pw: "SSH")
    monkeypatch.setattr(upload_mod, "upload", lambda changes, ppa, user, key: calls["upload"].append((changes.name, ppa, user, key)))
    monkeypatch.setattr(upload_mod, "wait", fake_wait)
    opts = update.Options(pkg_name="hello-acme", ppa_name="acme-staging")
    return pkg, opts, calls, used, states


def prep(prof, pkg, opts, workdir, resume=False):
    return update.prepare(prof, pkg, "resolute", opts, workdir, resume, "bot", log=lambda m: None)


def rel(prof, pkg, workdir, to="staging", approve=None, sign="staging-key", ssh="ssh", wait=60):
    return update.release(prof, pkg, "resolute", workdir, to, approve, sign, ssh, wait, "bot", log=lambda m: None)


def prepared(prof, tmp_path, world, env, name="w"):
    pkg, opts, calls, used, states = env
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1})
    w = tmp_path / name
    r = prep(prof, pkg, opts, w)
    assert r.code == 0 and r.tree
    return w, r


def staged(prof, tmp_path, world, env):
    pkg, *_ = env
    w, p = prepared(prof, tmp_path, world, env)
    assert rel(prof, pkg, w).code == 0
    return w, p


def test_prepare_runs_the_steps_up_to_an_unsigned_package_and_never_reads_a_key(prof, tmp_path, world, env, monkeypatch):
    pkg, opts, calls, used, _ = env
    monkeypatch.setattr(signing, "read_secret", lambda p: pytest.fail("prepare must not read a key"))
    monkeypatch.setattr(signing, "Keyring", lambda *a: pytest.fail("prepare must not open a keyring"))
    monkeypatch.setattr(upload_mod, "upload", lambda *a: pytest.fail("prepare must not upload"))
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1})
    used[STAGING] = {"1.0-1ubuntu1.1+acme1"}
    w = tmp_path / "ws" / "hello-acme"
    r = prep(prof, pkg, opts, w)
    assert r.code == 0 and r.done == ["clone", "merge", "finish", "prepare"]
    assert r.merge == {"tier": "mechanical", "risk": "low", "conflicts": 0}
    # the versions of the production and of the staging PPA both count, acme1 is taken in staging
    assert r.version == "1.0-1ubuntu1.1+acme2"
    assert PRODUCTION in calls["ppa_versions"] and STAGING in calls["ppa_versions"]
    assert calls["build"] == [None]
    assert r.tree == git(w, "rev-parse", "HEAD^{tree}")
    assert "--to staging" in r.stopped and "git ppa release" in r.stopped
    state = json.loads((w / ".git/git-ppa-update.json").read_text())
    assert state["done"][-1] == "prepare" and state["tree"] == r.tree and state["manual_review"] is True


def test_release_to_staging_signs_uploads_and_waits_with_no_approval(prof, tmp_path, world, env):
    pkg, opts, calls, *_ = env
    w, p = prepared(prof, tmp_path, world, env)
    r = rel(prof, pkg, w)
    assert r.code == 0 and r.done == ["clone", "merge", "finish", "prepare", "sign", "upload", "wait"]
    assert calls["build"] == [None, "secret:staging-key"]
    assert calls["upload"] == [("hello-acme_1.0-1ubuntu1.1+acme1_source.changes", STAGING, "bot", "SSH")]
    assert f"--to production --approve {p.tree[:12]}" in r.stopped


def test_release_to_production_signs_again_with_the_production_key_and_uploads_to_the_production_ppa(prof, tmp_path, world, env):
    pkg, opts, calls, *_ = env
    w, p = staged(prof, tmp_path, world, env)
    r = rel(prof, pkg, w, to="production", approve=p.tree[:12], sign="production-key")
    assert r.code == 0 and r.done[-3:] == ["prod-sign", "prod-upload", "prod-wait"]
    assert calls["build"] == [None, "secret:staging-key", "secret:production-key"]
    assert [(u[1], u[3]) for u in calls["upload"]] == [(STAGING, "SSH"), (PRODUCTION, "SSH")]
    assert calls["wait"] == [(STAGING, r.version), (PRODUCTION, r.version)]
    assert r.stopped == f"released: {r.version} is built in {PRODUCTION}"


def test_production_needs_the_approval_and_reads_no_key_before_it(prof, tmp_path, world, env):
    pkg, _, calls, *_ = env
    w, p = staged(prof, tmp_path, world, env)
    reads = len(calls["read_secret"])
    for approve in (None, "", p.tree[:11], "0" * 12, p.tree[:11] + ("0" if p.tree[11] != "0" else "1")):
        with pytest.raises(merge.Refused, match="--approve"):
            rel(prof, pkg, w, to="production", approve=approve)
    assert len(calls["read_secret"]) == reads and len(calls["upload"]) == 1


def test_production_needs_a_successful_staging_build_and_a_listed_production_ppa(prof, tmp_path, world, env):
    pkg, _, calls, _, states = env
    w, p = prepared(prof, tmp_path, world, env)
    with pytest.raises(update.UpdateError, match="release --to staging first"):
        rel(prof, pkg, w, to="production", approve=p.tree[:12])
    states.append({"status": "Published", "builds": [{"arch": "amd64", "state": "Failed to build", "link": "https://b/1"}]})
    assert rel(prof, pkg, w).code == 1
    with pytest.raises(update.UpdateError, match="release --to staging first"):
        rel(prof, pkg, w, to="production", approve=p.tree[:12])
    assert rel(prof, pkg, w).code == 0
    prof.production_ppas = []
    with pytest.raises(merge.Refused, match="production_ppas"):
        rel(prof, pkg, w, to="production", approve=p.tree[:12])


def test_the_staging_key_can_not_release_to_production_without_the_listed_ppa_and_vice_versa(prof, tmp_path, world, env):
    pkg, *_ = env
    w, p = prepared(prof, tmp_path, world, env)
    prof.allow_ppas = [PRODUCTION]
    with pytest.raises(merge.Refused, match="allow_ppas"):
        rel(prof, pkg, w)


def test_release_refuses_a_tree_that_changed_after_the_review(prof, tmp_path, world, env):
    pkg, _, calls, *_ = env
    w, p = prepared(prof, tmp_path, world, env)
    (w / "debian/rules").write_text((w / "debian/rules").read_text() + "\n# slipped in\n")
    with pytest.raises(merge.Refused, match="not the tree that was prepared"):
        rel(prof, pkg, w)
    git(w, "commit", "-qam", "slipped in")
    with pytest.raises(merge.Refused, match="not the tree that was prepared"):
        rel(prof, pkg, w)
    assert calls["read_secret"] == [] and calls["upload"] == []


def test_a_tree_that_changes_while_it_is_built_is_not_uploaded(prof, tmp_path, world, env, monkeypatch):
    pkg, _, calls, *_ = env
    w, p = prepared(prof, tmp_path, world, env)

    def sneaky(prof_, pkg_, series, repo, key=None):
        (repo / "debian/rules").write_text("changed\n")
        git(repo, "commit", "-qam", "changed")
        return FakeBuilt("x")

    monkeypatch.setattr(build_mod, "build", sneaky)
    with pytest.raises(merge.Refused, match="changed while it was being built"):
        rel(prof, pkg, w)
    assert calls["upload"] == []


def test_without_manual_review_production_needs_no_approval_but_still_checks_the_tree(prof, tmp_path, world, env):
    pkg, opts, calls, *_ = env
    opts.manual_review = False
    w, p = prepared(prof, tmp_path, world, env)
    assert rel(prof, pkg, w).code == 0
    assert rel(prof, pkg, w, to="production", sign="production-key").code == 0
    assert len(calls["upload"]) == 2


def test_release_needs_both_keys_and_one_of_them_at_most_from_standard_input(prof, tmp_path, world, env):
    pkg, *_ = env
    w, p = prepared(prof, tmp_path, world, env)
    with pytest.raises(update.UpdateError, match="--sign-key-file"):
        rel(prof, pkg, w, sign=None)
    with pytest.raises(update.UpdateError, match="--ssh-key-file"):
        rel(prof, pkg, w, ssh=None)
    with pytest.raises(update.UpdateError, match="only one of"):
        rel(prof, pkg, w, sign="-", ssh="-")


def test_a_build_still_running_stops_with_5_and_release_again_does_not_sign_or_upload_again(prof, tmp_path, world, env):
    pkg, opts, calls, used, states = env
    w, p = prepared(prof, tmp_path, world, env)
    states.append({"status": "Pending", "builds": [{"arch": "amd64", "state": "Uploading build", "link": "l"}]})
    r = rel(prof, pkg, w)
    assert r.code == update.BUILDING and r.done[-1] == "upload" and "release --to staging again" in r.stopped
    # no key is needed any more, the signed package is uploaded and only the wait is left
    r = rel(prof, pkg, w, sign=None, ssh=None)
    assert r.code == 0 and r.done[-1] == "wait"
    assert len(calls["upload"]) == 1 and calls["build"] == [None, "secret:staging-key"] and len(calls["wait"]) == 2


def test_a_failed_build_in_the_staging_ppa_is_an_error_with_the_link(prof, tmp_path, world, env):
    pkg, _, _, _, states = env
    w, p = prepared(prof, tmp_path, world, env)
    states.append({"status": "Published", "builds": [{"arch": "amd64", "state": "Failed to build", "link": "https://b/1"}]})
    r = rel(prof, pkg, w)
    assert r.code == 1 and "amd64 Failed to build https://b/1" in r.stopped


def test_an_upload_already_in_the_ppa_is_not_sent_again(prof, tmp_path, world, env, monkeypatch):
    pkg, opts, calls, used, _ = env
    w, p = prepared(prof, tmp_path, world, env)
    monkeypatch.setattr(upload_mod, "upload", lambda *a: (_ for _ in ()).throw(upload_mod.UploadError("network gone")))
    with pytest.raises(upload_mod.UploadError):
        rel(prof, pkg, w)
    # the upload reached Launchpad before the connection broke
    used[STAGING] = {p.version or "1.0-1ubuntu1.1+acme1"}
    r = rel(prof, pkg, w, sign=None)
    assert r.code == 0 and r.done[-2:] == ["upload", "wait"]


def test_conflicts_stop_with_3_and_continue_after_the_commit(prof, tmp_path, world, env):
    pkg, opts, calls, *_ = env
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1,
                        "debian/control": CONTROL_V1.replace("ubuntu-devel-discuss", "other-list")})
    w = tmp_path / "w"
    r = prep(prof, pkg, opts, w)
    assert r.code == 3 and r.merge["tier"] == "ai-assisted" and r.done == ["clone"]
    r = prep(prof, pkg, opts, w, resume=True)
    assert r.code == 3 and "resolve and commit" in r.stopped
    # keep our control, the archive one has the unrenamed package and lint would stop finish
    git(w, "checkout", "--ours", "debian/control")
    git(w, "add", "debian/control")
    git(w, "commit", "-q", "--no-edit")
    r = prep(prof, pkg, opts, w, resume=True)
    assert r.code == 0 and r.done[-1] == "prepare" and calls["build"] == [None]


def test_a_fresh_run_refuses_an_existing_directory_and_continue_checks_the_run(prof, tmp_path, world, env):
    pkg, opts, *_ = env
    (tmp_path / "w").mkdir()
    with pytest.raises(update.UpdateError, match="--continue"):
        prep(prof, pkg, opts, tmp_path / "w")
    git(tmp_path / "w", "init", "-q")
    with pytest.raises(update.UpdateError, match="not started by update-package"):
        prep(prof, pkg, opts, tmp_path / "w", resume=True)
    with pytest.raises(update.UpdateError, match="not started by update-package"):
        rel(prof, pkg, tmp_path / "w")


def test_release_refuses_a_run_that_is_not_prepared(prof, tmp_path, world, env):
    pkg, opts, *_ = env
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1,
                        "debian/control": CONTROL_V1.replace("ubuntu-devel-discuss", "other-list")})
    w = tmp_path / "w"
    assert prep(prof, pkg, opts, w).code == 3
    with pytest.raises(update.UpdateError, match="not prepared yet"):
        rel(prof, pkg, w)
    with pytest.raises(update.UpdateError, match="--to is staging or production"):
        rel(prof, pkg, w, to="elsewhere")


def test_the_staging_ppa_must_be_allowed_and_not_the_production_ppa(prof, tmp_path, world, env):
    pkg, opts, *_ = env
    opts.ppa_name = "other"
    with pytest.raises(merge.Refused, match="allow_ppas"):
        prep(prof, pkg, opts, tmp_path / "w")
    opts.ppa_name = "acme-proposed"
    with pytest.raises(merge.Refused, match="target PPA"):
        prep(prof, pkg, opts, tmp_path / "w")


def test_release_checks_the_allow_list_again_with_the_profile_it_runs_with(prof, tmp_path, world, env):
    pkg, *_ = env
    w, p = prepared(prof, tmp_path, world, env)
    prof.allow_ppas = []
    with pytest.raises(merge.Refused, match="allow_ppas"):
        rel(prof, pkg, w)


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
])
def test_spec_options_git_ppa_does_not_have_are_refused(change, message):
    o = update.Options(pkg_name="p", ppa_name="s")
    for k, v in change.items():
        setattr(o, k, v)
    with pytest.raises(update.UpdateError, match=message):
        update.check(o)


def test_config_yaml_uses_the_option_names_and_never_holds_a_key_option(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("pkg-name: hello-acme\nppa_name: s\nfrom: archive\nbuild: ppa\nmanual-review: false\nskillset: p.toml\ncontinue: w\n")
    assert update.load_config(f) == {"pkg_name": "hello-acme", "ppa_name": "s", "source": "archive", "build": ["ppa"],
                                     "manual_review": False, "profile": "p.toml", "workdir": "w"}
    f.write_text("pkg-nmae: x\n")
    with pytest.raises(update.UpdateError, match="unknown option 'pkg-nmae'"):
        update.load_config(f)
    for key in ("sign-key-file", "ssh_key_file"):
        f.write_text(f"{key}: /home/me/key\n")
        with pytest.raises(update.UpdateError, match="keys are given to `git ppa release` only"):
            update.load_config(f)


def test_update_package_has_no_key_options_on_the_command_line(capsys):
    for opt in ("--sign-key-file", "--ssh-key-file"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["update-package", opt, "-"])
        assert "unrecognized arguments" in capsys.readouterr().err
    a = cli.build_parser().parse_args(["release", "dir", "--sign-key-file", "-"])
    assert a.sign_key_file == "-" and a.to == "staging"


def test_the_cli_reads_config_yaml_and_the_command_line_wins(tmp_path, monkeypatch, capsys):
    prof_file = tmp_path / "p.toml"
    prof_file.write_text('[project]\ntag = "acme"\nseries = ["resolute"]\n[repo]\nurl = "x/{name}"\nupstream_url = "y/{archive}"\n'
                         '[branches]\nmirror = "ubuntu/{series}"\ndownstream = "{tag}/{series}"\nstaging = "{tag}-proposed/{series}"\n'
                         '[ppa]\nowner = "acme"\n[safety]\nallow_ppas = ["~acme/ubuntu/s2"]\n[[package]]\nname = "hello-acme"\narchive = "hello"\n')
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"pkg-name: hello-acme\nppa-name: s1\nskillset: {prof_file}\nmanual-review: false\nlp-user: bot\nwait: 10\n")
    seen = {}

    def fake_prepare(prof, pkg, series, o, workdir, resume, lpuser, log):
        seen.update(o=o, workdir=workdir, resume=resume, lpuser=lpuser, series=series)
        return update.Result(pkg.name, str(workdir), stopped="ok")

    monkeypatch.setattr(update, "prepare", fake_prepare)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["update-package", str(cfg), "--ppa-name", "s2"]) == 0
    o = seen["o"]
    assert (o.ppa_name, o.manual_review, o.wait, seen["lpuser"], seen["resume"]) == ("s2", False, 10, "bot", False)
    assert seen["workdir"] == tmp_path / "hello-acme" and seen["series"] == "resolute"
    assert cli.main(["update-package", str(cfg), "--ai-model", "x"]) == 1
    assert "--ai-model is not supported" in capsys.readouterr().err


def test_the_release_cli_takes_the_package_and_series_of_the_run(tmp_path, monkeypatch, world, env, prof):
    w, p = prepared(prof, tmp_path, world, env)
    seen = {}

    def fake_release(prof_, pkg_, series, workdir, to, approve, sign, ssh, wait, lpuser, log):
        seen.update(pkg=pkg_.name, series=series, to=to, approve=approve, sign=sign, ssh=ssh, wait=wait, lpuser=lpuser)
        return update.Result(pkg_.name, str(workdir), stopped="ok")

    monkeypatch.setattr(cli, "_profile", lambda args: prof)
    monkeypatch.setattr(update, "release", fake_release)
    assert cli.main(["release", str(w), "--to", "production", "--approve", p.tree[:12], "--sign-key-file", "-", "--ssh-key-file", "k", "--lp-user", "bot"]) == 0
    assert seen == {"pkg": "hello-acme", "series": "resolute", "to": "production", "approve": p.tree[:12], "sign": "-", "ssh": "k", "wait": 3600, "lpuser": "bot"}


def test_the_skillset_by_name_is_a_profile_in_the_skillsets_directory(tmp_path, monkeypatch, capsys):
    d = tmp_path / "skillsets"
    d.mkdir()
    (d / "acme.toml").write_text('[project]\ntag = "acme"\nseries = ["resolute"]\n[repo]\nurl = "x/{name}"\nupstream_url = "y"\n'
                                 '[branches]\nmirror = "ubuntu/{series}"\ndownstream = "{tag}/{series}"\nstaging = "{tag}-proposed/{series}"\n'
                                 '[[package]]\nname = "hello-acme"\narchive = "hello"\n')
    monkeypatch.setenv("GIT_PPA_SKILLSETS", str(d))
    monkeypatch.setattr(update, "prepare", lambda prof, pkg, *a, **k: update.Result(pkg.name, "w", stopped=prof.tag))
    assert cli.main(["update-package", "--skillset", "acme", "--pkg-name", "hello-acme", "--ppa-name", "~a/ubuntu/s", "--lp-user", "u"]) == 0
    assert "acme" in capsys.readouterr().out


def test_a_clone_that_fails_leaves_no_directory_so_the_run_can_start_again(prof, tmp_path, world, env, monkeypatch):
    pkg, opts, *_ = env
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1})
    real = workspace.clone

    def broken(prof_, pkg_, series, user, dest):
        dest.mkdir(parents=True)
        raise gitutil.GitError(["fetch", "pkg"], 128, "HTTP 504")

    monkeypatch.setattr(workspace, "clone", broken)
    w = tmp_path / "w"
    with pytest.raises(gitutil.GitError):
        prep(prof, pkg, opts, w)
    assert not w.exists()
    monkeypatch.setattr(workspace, "clone", real)
    assert prep(prof, pkg, opts, w).code == 0
