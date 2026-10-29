# SPDX-License-Identifier: GPL-3.0-or-later
"""orig without the network: Launchpad calls are replaced by local files."""

import hashlib
from pathlib import Path

import pytest

from conftest import CHANGELOG_V1, DOWN_ENTRY
from git_ppa import cli, credentials, gitutil, lp, orig


def serve(tmp_path: Path, monkeypatch, tarball: bytes, size_lie: int | None = None, drop_orig: bool = False) -> None:
    src = tmp_path / "srv"
    src.mkdir()
    (src / "hello_1.0.orig.tar.gz").write_bytes(tarball)
    (src / "hello_1.0-1ubuntu1.debian.tar.xz").write_bytes(b"debian")
    sha = hashlib.sha256(tarball).hexdigest()
    dsc = (
        "Format: 3.0 (quilt)\nSource: hello\nVersion: 1.0-1ubuntu1\n"
        "Checksums-Sha256:\n"
        f" {sha} {size_lie if size_lie is not None else len(tarball)} hello_1.0.orig.tar.gz\n"
        f" {hashlib.sha256(b'debian').hexdigest()} 6 hello_1.0-1ubuntu1.debian.tar.xz\n"
    )
    (src / "hello_1.0-1ubuntu1.dsc").write_text(dsc)
    names = ["hello_1.0-1ubuntu1.dsc", "hello_1.0-1ubuntu1.debian.tar.xz"] + ([] if drop_orig else ["hello_1.0.orig.tar.gz"])
    monkeypatch.setattr(lp, "published", lambda *a, **k: [lp.Pub("1.0-1ubuntu1", "Release", "", "x")])
    monkeypatch.setattr(lp, "source_file_urls", lambda pub: [(src / n).as_uri() for n in names])


def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    (r / "debian").mkdir(parents=True)
    (r / "debian/changelog").write_text(DOWN_ENTRY + CHANGELOG_V1)
    return r


def test_orig_is_downloaded_verified_and_renamed(prof, tmp_path, monkeypatch):
    serve(tmp_path, monkeypatch, b"tarball bytes")
    files = orig.fetch(prof, prof.package("hello-acme"), "resolute", repo(tmp_path), tmp_path / "out")
    assert [f.name for f in files] == ["hello-acme_1.0.orig.tar.gz"]
    assert files[0].read_bytes() == b"tarball bytes"


def test_orig_checksum_mismatch_is_an_error(prof, tmp_path, monkeypatch):
    serve(tmp_path, monkeypatch, b"tarball bytes", size_lie=5)
    with pytest.raises(orig.OrigError, match="checksum mismatch"):
        orig.fetch(prof, prof.package("hello-acme"), "resolute", repo(tmp_path), tmp_path / "out")


def test_orig_listed_but_not_offered(prof, tmp_path, monkeypatch):
    serve(tmp_path, monkeypatch, b"x", drop_orig=True)
    with pytest.raises(orig.OrigError, match="not offered"):
        orig.fetch(prof, prof.package("hello-acme"), "resolute", repo(tmp_path), tmp_path / "out")


def test_orig_for_an_unpublished_version(prof, tmp_path, monkeypatch):
    monkeypatch.setattr(lp, "published", lambda *a, **k: [])
    with pytest.raises(orig.OrigError, match="not published"):
        orig.fetch(prof, prof.package("hello-acme"), "resolute", repo(tmp_path), tmp_path / "out")


def test_missing_host_tool_is_reported_with_a_hint(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(gitutil.ToolMissing, match="dpkg-dev"):
        gitutil.require("dpkg-mergechangelogs")


def test_missing_git_is_a_clean_error_not_a_traceback(prof, tmp_path, monkeypatch, capsys):
    (tmp_path / "r/.git").mkdir(parents=True)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("GIT_PPA_LPUSER", "lp")
    with pytest.raises(gitutil.ToolMissing, match="install git"):
        gitutil.run("status")
    p = tmp_path / "p.toml"
    p.write_text('[project]\ntag="x"\nseries=["s"]\n[repo]\nurl="u"\nupstream_url="u"\n[branches]\nmirror="m"\ndownstream="d"\nstaging="s"\n')
    assert cli.main(["--profile", str(p), "merge", "start", "-C", str(tmp_path / "r")]) == 1
    assert "git not found" in capsys.readouterr().err


def fake_snap(tmp_path):
    snap = tmp_path / "snap"
    (snap / "usr/bin").mkdir(parents=True)
    exe = snap / "usr/bin/git"
    exe.write_text('#!/bin/sh\necho "staged git|$GIT_EXEC_PATH|$GIT_TEMPLATE_DIR|$*"\n')
    exe.chmod(0o755)
    return snap


def test_the_snaps_own_git_is_used_with_its_helpers_and_templates(tmp_path, monkeypatch):
    snap = fake_snap(tmp_path)
    monkeypatch.setenv("SNAP", str(snap))
    monkeypatch.setenv("PATH", "/nonexistent")
    gitutil.require("git")
    out = gitutil.out("version")
    assert out == f"staged git|{snap}/usr/lib/git-core|{snap}/usr/share/git-core/templates|-c commit.gpgsign=false version"


def test_without_a_snap_the_host_git_is_used(monkeypatch):
    monkeypatch.delenv("SNAP", raising=False)
    monkeypatch.delenv("GIT_EXEC_PATH", raising=False)
    assert gitutil.snap_git() is None
    assert gitutil.out("--version").startswith("git version")


def test_a_snap_without_git_falls_back_to_the_host(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAP", str(tmp_path))
    assert gitutil.snap_git() is None
    assert gitutil.out("--version").startswith("git version")


def test_merge_commits_are_never_signed(tmp_path, monkeypatch):
    snap = fake_snap(tmp_path)
    monkeypatch.setenv("SNAP", str(snap))
    assert "commit.gpgsign=false" in gitutil.out("version")


def test_a_confined_snap_gets_an_identity_and_keeps_a_users_own_config(tmp_path, monkeypatch):
    common, home = tmp_path / "common", tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SNAP", str(tmp_path / "snap"))
    monkeypatch.setenv("SNAP_USER_COMMON", str(common))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    env = gitutil._env()
    assert env["GIT_CONFIG_GLOBAL"] == str(common / "gitconfig")
    assert "email = git-ppa@localhost" in (common / "gitconfig").read_text()
    (home / ".gitconfig").write_text("[user]\n\tname = me\n")
    assert "GIT_CONFIG_GLOBAL" not in gitutil._env()


def test_the_changelog_driver_runs_the_snaps_perl_script_with_its_modules(tmp_path, monkeypatch):
    snap = fake_snap(tmp_path)
    (snap / "usr/bin/perl").write_text("")
    (snap / "usr/bin/dpkg-mergechangelogs").write_text("")
    (snap / "usr/share/perl5").mkdir(parents=True)
    monkeypatch.setenv("SNAP", str(snap))
    monkeypatch.setenv("SNAP_NAME", "x")
    cmd = gitutil.changelog_driver()
    assert cmd == (f"DPKG_ORIGINS_DIR=/snap/x/current/usr/share/dpkg-origins "
                   f"PERL5LIB=/snap/x/current/usr/share/perl5 /snap/x/current/usr/bin/perl "
                   f"/snap/x/current/usr/bin/dpkg-mergechangelogs -m %O %A %B %A")
    monkeypatch.delenv("SNAP")
    assert gitutil.changelog_driver() == "dpkg-mergechangelogs -m %O %A %B %A"


def test_the_cache_lives_in_the_snaps_common_dir(tmp_path, monkeypatch):
    from git_ppa import status
    monkeypatch.delenv("GIT_PPA_CACHE", raising=False)
    monkeypatch.setenv("SNAP_USER_COMMON", str(tmp_path))
    assert status.cache_dir() == tmp_path / "cache"
    monkeypatch.setenv("GIT_PPA_CACHE", "/elsewhere")
    assert str(status.cache_dir()) == "/elsewhere"


def test_a_missing_token_is_explained(monkeypatch):
    assert "git ppa login" in gitutil._auth_hint("fatal: could not read Username for 'https://git.launchpad.net': terminal prompts disabled")
    assert "git ppa login" in gitutil._auth_hint("remote: Authentication failed")
    assert "git ppa login" in gitutil._auth_hint("fatal: unable to get password from user")
    assert gitutil._auth_hint("fatal: repository not found") == ""


REPO_A, REPO_B = "~t/p/+git/a", "~t/p/+git/b"


def fill(host_path):
    host, _, path = host_path.partition("/")
    return gitutil.run("credential", "fill", check=False, input_=f"protocol=https\nhost={host}\npath={path}\n\n").stdout


def test_the_token_file_has_one_private_line_per_repository(tmp_path, monkeypatch):
    f = tmp_path / "cfg" / "credentials"
    monkeypatch.setenv("GIT_PPA_CREDENTIALS", str(f))
    credentials.save("me", "tok/en:1@", REPO_A)
    assert f.read_text() == "https://me:tok%2Fen%3A1%40@git.launchpad.net/~t/p/+git/a\n"
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    credentials.save("me", "two", REPO_B)
    credentials.save("me", "new", REPO_A)
    assert credentials.stored() == [REPO_A, REPO_B]
    assert f.read_text().count("\n") == 2 and "new" in f.read_text()
    assert credentials.remove([REPO_A, "~t/p/+git/zzz"]) == 1
    assert credentials.stored() == [REPO_B]
    assert credentials.remove() == 1
    assert not f.exists()


def test_the_token_file_is_in_the_snaps_common_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_PPA_CREDENTIALS", raising=False)
    monkeypatch.setenv("SNAP_USER_COMMON", str(tmp_path))
    assert credentials.path() == tmp_path / "credentials"


def test_git_gets_the_token_of_the_repository_it_asks_for_and_only_on_launchpad(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_PPA_CREDENTIALS", str(tmp_path / "credentials"))
    monkeypatch.delenv("GIT_PPA_TOKEN", raising=False)
    credentials.save("me", "token-a", REPO_A)
    credentials.save("me", "token-b", REPO_B)
    assert "password=token-a" in fill(f"git.launchpad.net/{REPO_A}")
    assert "password=token-b" in fill(f"git.launchpad.net/{REPO_B}")
    assert "token" not in fill("git.launchpad.net/~t/p/+git/c")
    assert "token" not in fill(f"example.com/{REPO_A}")


def test_a_refused_token_is_not_erased(tmp_path, monkeypatch):
    f = tmp_path / "credentials"
    monkeypatch.setenv("GIT_PPA_CREDENTIALS", str(f))
    credentials.save("me", "token-a", REPO_A)
    before = f.read_text()
    gitutil.run("credential", "reject", check=False,
                input_=f"protocol=https\nhost=git.launchpad.net\npath={REPO_A}\nusername=me\npassword=token-a\n\n")
    assert f.read_text() == before


def test_a_token_from_the_environment_is_not_in_the_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_PPA_CREDENTIALS", str(tmp_path / "none"))
    monkeypatch.setenv("GIT_PPA_LPUSER", "bot")
    monkeypatch.setenv("GIT_PPA_TOKEN", "ci-token")
    assert all("ci-token" not in a for a in gitutil._credential_args())
    # one token for the whole project serves every repository, and nothing else
    for repo in (REPO_A, REPO_B):
        out = fill(f"git.launchpad.net/{repo}")
        assert "username=bot" in out and "password=ci-token" in out
    assert "ci-token" not in fill(f"example.com/{REPO_A}")
