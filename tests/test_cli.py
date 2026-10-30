# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from conftest import CONTROL_V1, CHANGELOG_V1, NEW_ENTRY, new_version
from git_ppa import cli, workspace


def test_global_options_work_before_and_after_the_subcommand():
    p = cli.build_parser()
    assert p.parse_args(["--profile", "a.toml", "--json", "status"]).profile == "a.toml"
    assert p.parse_args(["status", "--profile", "b.toml"]).profile == "b.toml"
    assert p.parse_args(["--profile", "a.toml", "status", "--json"]).profile == "a.toml"
    a = p.parse_args(["--lpuser", "me", "lint", "-C", "x"])
    assert (a.lpuser, a.directory, a.json) == ("me", "x", False)


def test_usage_errors_do_not_use_exit_code_2(capsys):
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(["bogus"])
    assert e.value.code == 1


def test_missing_profile_is_a_clear_error(monkeypatch, capsys):
    monkeypatch.delenv("GIT_PPA_PROFILE", raising=False)
    assert cli.main(["status"]) == 1
    assert "no profile" in capsys.readouterr().err


def test_merge_in_progress_says_how_to_recover(prof, tmp_path, world, monkeypatch, capsys):
    new_version(world, {"debian/changelog": NEW_ENTRY + CHANGELOG_V1, "debian/control": CONTROL_V1.replace("ubuntu-devel-discuss", "x")})
    pkg = prof.package("hello-acme")
    repo = workspace.clone(prof, pkg, "resolute", "lp", tmp_path / "ws")
    workspace.sync_local(prof, pkg, "resolute", repo, "1.0-1ubuntu1.1")
    from git_ppa import merge
    merge.merge_start(prof, pkg, "resolute", repo, "1.0-1ubuntu1.1")
    with pytest.raises(merge.Refused, match="--abort"):
        merge.merge_start(prof, pkg, "resolute", repo, "1.0-1ubuntu1.1")


def test_login_keeps_one_token_per_package_and_logout_removes_them(tmp_path, monkeypatch, capsys):
    import io
    from git_ppa import credentials
    prof = tmp_path / "p.toml"
    prof.write_text(
        '[project]\ntag="x"\nseries=["s"]\n[repo]\nurl="https://git.launchpad.net/~t/p/+git/{name}"\nupstream_url="u"\n'
        '[branches]\nmirror="m"\ndownstream="d"\nstaging="s"\n'
        '[[package]]\nname="a"\narchive="a"\n[[package]]\nname="b"\narchive="b"\n'
    )
    monkeypatch.setenv("GIT_PPA_CREDENTIALS", str(tmp_path / "credentials"))
    monkeypatch.setenv("GIT_PPA_PROFILE", str(prof))
    monkeypatch.delenv("GIT_PPA_TOKEN", raising=False)
    assert cli.main(["login", "--lpuser", "me", "--no-verify"]) == 1
    assert "name the packages" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("tok-a\n"))
    assert cli.main(["login", "--lpuser", "me", "--no-verify", "a"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("tok-all\n"))
    assert cli.main(["login", "--lpuser", "me", "--no-verify", "--all"]) == 0
    assert credentials.stored() == ["~t/p/+git/a", "~t/p/+git/b"]
    assert cli.main(["logout", "a"]) == 0
    assert credentials.stored() == ["~t/p/+git/b"]
    assert cli.main(["logout"]) == 0
    assert credentials.stored() == []


def _login_profile(tmp_path, monkeypatch):
    prof = tmp_path / "p.toml"
    prof.write_text(
        '[project]\ntag="x"\nseries=["s"]\n[repo]\nurl="https://git.launchpad.net/~t/p/+git/{name}"\nupstream_url="u"\n'
        '[branches]\nmirror="m"\ndownstream="d"\nstaging="s"\n'
        '[[package]]\nname="a"\narchive="src-a"\n[[package]]\nname="b"\narchive="b"\n'
    )
    monkeypatch.setenv("GIT_PPA_CREDENTIALS", str(tmp_path / "credentials"))
    monkeypatch.setenv("GIT_PPA_PROFILE", str(prof))
    monkeypatch.delenv("GIT_PPA_TOKEN", raising=False)


def test_login_from_a_json_file_stores_one_token_per_package(tmp_path, monkeypatch, capsys):
    from git_ppa import credentials
    _login_profile(tmp_path, monkeypatch)
    f = tmp_path / "tokens.json"
    f.write_text('{"a": "tok-a", "b": "tok-b"}')
    f.chmod(0o600)
    assert cli.main(["login", "--lpuser", "me", "--no-verify", "--from-file", str(f)]) == 0
    assert credentials.stored() == ["~t/p/+git/a", "~t/p/+git/b"]
    out = capsys.readouterr()
    assert "tok-a" not in out.out + out.err
    text = (tmp_path / "credentials").read_text()
    assert "me:tok-a@" in text and "me:tok-b@" in text


def test_login_from_standard_input_and_the_star_token(tmp_path, monkeypatch):
    import io
    from git_ppa import credentials
    _login_profile(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"*": "project", "b": "only-b"}\n'))
    assert cli.main(["login", "--lpuser", "me", "--no-verify", "--from-file", "-"]) == 0
    text = (tmp_path / "credentials").read_text()
    assert "me:project@git.launchpad.net/~t/p/+git/a" in text
    assert "me:only-b@git.launchpad.net/~t/p/+git/b" in text and "project@git.launchpad.net/~t/p/+git/b" not in text


def test_login_from_a_file_refuses_a_readable_or_malformed_file(tmp_path, monkeypatch, capsys):
    from git_ppa import credentials
    _login_profile(tmp_path, monkeypatch)
    f = tmp_path / "tokens.json"
    f.write_text('{"a": "tok-a"}')
    f.chmod(0o644)
    assert cli.main(["login", "--lpuser", "me", "--no-verify", "--from-file", str(f)]) == 1
    assert "chmod 600" in capsys.readouterr().err
    f.chmod(0o600)
    bad_inputs = (
        ("a tok-a", "not a JSON object"),
        ("[]", "must be a JSON object"),
        ("{}", "at least one entry"),
        ('{"a": "t1", "a": "t2"}', "appears twice"),
        ('{"a": "t1", "src-a": "t2"}', "appears twice"),
        ('{"nope": "t"}', "unknown package"),
        ('{"a": 5}', "non-empty string"),
        ('{"a": "two words"}', "without spaces"),
        ('{"a": ""}', "non-empty string"),
    )
    for bad, why in bad_inputs:
        f.write_text(bad)
        assert cli.main(["login", "--lpuser", "me", "--no-verify", "--from-file", str(f)]) == 1
        assert why in capsys.readouterr().err, bad
    assert credentials.stored() == []


def test_a_refused_token_keeps_the_token_that_was_there(tmp_path, monkeypatch, capsys):
    from git_ppa import credentials, gitutil
    import subprocess
    _login_profile(tmp_path, monkeypatch)
    credentials.save("me", "good", "~t/p/+git/a")
    before = (tmp_path / "credentials").read_text()
    refused = subprocess.CompletedProcess([], 128, "", "fatal: Authentication failed")
    monkeypatch.setattr(gitutil, "run", lambda *a, **k: refused)
    f = tmp_path / "tokens"
    f.write_text('{"a": "bad", "b": "bad"}')
    f.chmod(0o600)
    assert cli.main(["login", "--lpuser", "me", "--from-file", str(f)]) == 1
    assert (tmp_path / "credentials").read_text() == before
    assert capsys.readouterr().err.count("token refused") == 2
