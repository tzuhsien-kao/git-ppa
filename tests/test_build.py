# SPDX-License-Identifier: GPL-3.0-or-later
"""Signing, build and upload on a real tiny source package, a real gpg and an sftp server in the test."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import paramiko
import pytest

from git_ppa import build, cli, gitutil, lp, orig, signing, upload
from sftpd import SftpServer

needs_gpg = pytest.mark.skipif(not shutil.which("gpg"), reason="gpg is not installed")
needs_dpkg = pytest.mark.skipif(not shutil.which("dpkg-buildpackage"), reason="dpkg-dev is not installed")

CHANGELOG = """hello-acme (1.0-1+acme1) resolute; urgency=medium

  * Acme delta.

 -- Bot <bot@example.com>  Tue, 02 Jan 2026 00:00:00 +0000

hello (1.0-1) unstable; urgency=medium

  * Initial.

 -- Dev <dev@example.com>  Mon, 01 Jan 2026 00:00:00 +0000
"""

CONTROL = """Source: hello-acme
Section: misc
Priority: optional
Maintainer: Bot <bot@example.com>
Standards-Version: 4.7.0
Build-Depends: debhelper-compat (= 13)

Package: hello-acme
Architecture: any
Provides: hello (= ${binary:Version})
Conflicts: hello
Replaces: hello
Description: hello
 test package
"""


def _sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})


@pytest.fixture
def source(tmp_path, prof):
    """ws/hello-acme is the checkout, ws/hello-acme_1.0.orig.tar.gz is next to it."""
    ws = tmp_path / "ws"
    repo = ws / "hello-acme"
    (repo / "debian/source").mkdir(parents=True)
    (repo / "debian/changelog").write_text(CHANGELOG)
    (repo / "debian/control").write_text(CONTROL)
    (repo / "debian/rules").write_text("#!/usr/bin/make -f\n%:\n\tdh $@\n")
    (repo / "debian/rules").chmod(0o755)
    (repo / "debian/source/format").write_text("3.0 (quilt)\n")
    (repo / "hello.txt").write_text("hello\n")
    _sh(repo, "git", "init", "-q", "-b", "acme/resolute")
    _sh(repo, "git", "add", "-A")
    _sh(repo, "git", "commit", "-q", "-m", "x")
    make_orig(ws)
    return prof.package("hello-acme"), repo


def make_orig(ws: Path) -> Path:
    path = ws / "hello-acme_1.0.orig.tar.gz"
    with tarfile.open(path, "w:gz") as t:
        data = b"hello\n"
        info = tarfile.TarInfo("hello-1.0/hello.txt")
        info.size = len(data)
        info.mtime = 1
        t.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def secret_key(tmp_path):
    """Exported secret key of a throwaway identity, without and with a passphrase."""
    if not shutil.which("gpg"):
        pytest.skip("gpg is not installed")
    out = {}
    for name, pw in (("plain", ""), ("locked", "pw")):
        home = tmp_path / f"gnupg-{name}"
        home.mkdir(mode=0o700)
        env = {"GNUPGHOME": str(home), "PATH": "/usr/bin:/bin"}
        base = ["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", pw]
        subprocess.run(base + ["--quick-generate-key", f"Test {name} <{name}@example.com>", "rsa2048", "sign", "1d"], env=env, capture_output=True, check=True)
        exp = subprocess.run(base + ["--export-secret-keys"], env=env, capture_output=True, check=True).stdout
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=env, capture_output=True)
        out[name] = (exp, pw or None)
    return out


@needs_dpkg
def test_unsigned_build_makes_a_source_package_and_leaves_the_tree_clean(source, prof):
    pkg, repo = source
    b = build.build(prof, pkg, "resolute", repo)
    assert b.version == "1.0-1+acme1" and not b.signed and not b.orig_fetched
    ws = repo.parent
    assert b.changes == str(ws / "hello-acme_1.0-1+acme1_source.changes")
    assert set(b.files) == {n for n in b.files if (ws / n).is_file()} and len(b.files) >= 4
    assert any(n.endswith(".dsc") for n in b.files) and any(".orig." in n for n in b.files)
    assert gitutil.is_clean(repo) and not (repo / "debian/files").exists()


@needs_dpkg
def test_build_downloads_the_orig_when_it_is_missing(source, prof, monkeypatch):
    pkg, repo = source
    (repo.parent / "hello-acme_1.0.orig.tar.gz").unlink()
    calls = []
    monkeypatch.setattr(orig, "fetch", lambda *a, **k: calls.append(a) or make_orig(repo.parent))
    assert build.build(prof, pkg, "resolute", repo).orig_fetched is True
    assert len(calls) == 1


def test_build_refuses_a_checkout_whose_top_entry_is_not_ours(source, prof):
    pkg, repo = source
    text = (repo / "debian/changelog").read_text()
    entry = "hello (1.0-2) resolute; urgency=medium\n\n  * archive\n\n -- D <d@x.y>  Wed, 03 Jan 2026 00:00:00 +0000\n\n"
    (repo / "debian/changelog").write_text(entry + text)
    _sh(repo, "git", "commit", "-qam", "merge")
    with pytest.raises(build.BuildError, match="not an entry of hello-acme"):
        build.build(prof, pkg, "resolute", repo)


def test_build_refuses_a_dirty_tree_and_a_missing_suffix(source, prof):
    pkg, repo = source
    (repo / "hello.txt").write_text("changed\n")
    with pytest.raises(build.BuildError, match="not clean"):
        build.build(prof, pkg, "resolute", repo)
    (repo / "hello.txt").write_text("hello\n")
    (repo / "debian/changelog").write_text(CHANGELOG.replace("1.0-1+acme1", "1.0-2"))
    _sh(repo, "git", "commit", "-qam", "x")
    with pytest.raises(build.BuildError, match="no \\+acme<N>"):
        build.build(prof, pkg, "resolute", repo)


def test_changes_name_drops_the_epoch():
    assert build.changes_name("redis-x", "5:8.0.5-1+x6") == "redis-x_8.0.5-1+x6_source.changes"
    assert build.upstream_version("5:8.0.5-1+x6") == "8.0.5" and build.upstream_version("2.10-5+g2") == "2.10"


@needs_gpg
@needs_dpkg
@pytest.mark.parametrize("which", ["plain", "locked"])
def test_signed_build_verifies_and_leaves_no_keyring_behind(source, prof, secret_key, monkeypatch, tmp_path, which):
    pkg, repo = source
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("SNAP_USER_COMMON", raising=False)
    secret, pw = secret_key[which]
    with signing.Keyring(secret, pw) as key:
        home = key.home
        b = build.build(prof, pkg, "resolute", repo, key)
    assert b.signed and b.key and not home.exists()
    changes = Path(b.changes)
    assert changes.read_text().startswith("-----BEGIN PGP SIGNED MESSAGE-----")
    assert upload.read_changes(changes)[2][-1] == changes.name
    assert list((tmp_path / "run").iterdir()) == []


@needs_gpg
def test_keyring_refuses_a_wrong_or_missing_passphrase_and_junk(secret_key, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("SNAP_USER_COMMON", raising=False)
    secret, _ = secret_key["locked"]
    for bad in ("nope", None):
        with pytest.raises(signing.SigningError, match="wrong or missing passphrase"):
            signing.Keyring(secret, bad).__enter__()
    with pytest.raises(signing.SigningError, match="cannot import"):
        signing.Keyring(b"not a key\n").__enter__()
    with pytest.raises(signing.SigningError, match="empty"):
        signing.Keyring(b"  \n")
    assert list((tmp_path / "run").iterdir()) == []


def test_read_secret_refuses_a_file_others_can_read(tmp_path):
    f = tmp_path / "k"
    f.write_bytes(b"x")
    f.chmod(0o644)
    with pytest.raises(signing.SigningError, match="chmod 600"):
        signing.read_secret(str(f))
    f.chmod(0o600)
    assert signing.read_secret(str(f)) == b"x"
    with pytest.raises(signing.SigningError, match="not a file"):
        signing.read_secret(str(tmp_path / "missing"))


SIGNED = "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"


def _package(tmp_path, signed=True, dsc_signed=True):
    """A fake upload: files, a .changes with checksums, as dpkg-genchanges writes them."""
    d = tmp_path / "up"
    d.mkdir(parents=True)
    files = {"a_1.orig.tar.gz": b"orig", "a_1-1.debian.tar.xz": b"deb", "a_1-1.dsc": (SIGNED if dsc_signed else "") .encode() + b"dsc"}
    for n, data in files.items():
        (d / n).write_bytes(data)
    sums = "".join(f" {hashlib.sha256(v).hexdigest()} {len(v)} {n}\n" for n, v in files.items())
    body = f"Format: 1.8\nSource: a\nVersion: 1-1\nChecksums-Sha256:\n{sums}Files:\n"
    changes = d / "a_1-1_source.changes"
    text = body
    if signed:
        text = SIGNED + body + "\n-----BEGIN PGP SIGNATURE-----\n\nx\n-----END PGP SIGNATURE-----\n"
    changes.write_text(text)
    return changes


def test_read_changes_checks_signature_files_and_checksums(tmp_path):
    changes = _package(tmp_path)
    source, version, names = upload.read_changes(changes)
    assert (source, version) == ("a", "1-1")
    assert names[-1] == "a_1-1_source.changes" and set(names[:-1]) == {"a_1.orig.tar.gz", "a_1-1.debian.tar.xz", "a_1-1.dsc"}
    (changes.parent / "a_1-1.debian.tar.xz").write_bytes(b"tampered")
    with pytest.raises(upload.UploadError, match="does not match its checksum"):
        upload.read_changes(changes)
    (changes.parent / "a_1-1.debian.tar.xz").unlink()
    with pytest.raises(upload.UploadError, match="not next to it"):
        upload.read_changes(changes)
    with pytest.raises(upload.UploadError, match="is not signed"):
        upload.read_changes(_package(tmp_path / "u", signed=False))
    with pytest.raises(upload.UploadError, match="a_1-1.dsc is not signed"):
        upload.read_changes(_package(tmp_path / "v", dsc_signed=False))


@pytest.fixture
def server(tmp_path):
    key = paramiko.RSAKey.generate(2048)
    (tmp_path / "srv" / "~acme" / "ubuntu" / "acme-proposed").mkdir(parents=True)
    s = SftpServer(tmp_path / "srv", "lpuser", key)
    s.client_key = key
    s.fp = upload.fingerprint(s.host_key)
    yield s
    s.close()


def _go(server, changes, **kw):
    args = dict(host="127.0.0.1", port=server.port, fingerprints={server.fp})
    args.update(kw)
    return upload.upload(changes, "~acme/ubuntu/acme-proposed", "lpuser", args.pop("key", server.client_key), **args)


def test_upload_puts_every_file_and_the_changes_last(server, tmp_path):
    changes = _package(tmp_path)
    res = _go(server, changes)
    sent = [Path(p).name for p in server.created]
    assert sent[-1] == "a_1-1_source.changes" and sorted(sent) == sorted(res.files)
    dest = tmp_path / "srv/~acme/ubuntu/acme-proposed"
    for n in res.files:
        assert (dest / n).read_bytes() == (changes.parent / n).read_bytes()


def test_upload_refuses_a_host_key_that_is_not_the_pinned_one(server, tmp_path):
    with pytest.raises(upload.UploadError, match=r"host key of \[127.0.0.1\]:\d+ is SHA256:.*expected SHA256:AAAA"):
        _go(server, _package(tmp_path), fingerprints={"SHA256:" + "A" * 43})
    assert server.created == []


def test_upload_says_when_the_key_is_not_registered(server, tmp_path):
    with pytest.raises(upload.UploadError, match="refused the key for lpuser"):
        _go(server, _package(tmp_path), key=paramiko.RSAKey.generate(2048))


def test_upload_needs_a_known_host_key_and_a_dry_run_connects_to_nothing(tmp_path):
    changes = _package(tmp_path)
    with pytest.raises(upload.UploadError, match="no known host key for example.org"):
        upload.upload(changes, "~a/ubuntu/b", "u", None, host="example.org")
    res = upload.upload(changes, "~a/ubuntu/b", "u", None, host="example.org", fingerprints={"SHA256:x"}, dry_run=True)
    assert res.dry_run and res.incoming == "~a/ubuntu/b" and res.files[-1].endswith(".changes")
    with pytest.raises(upload.UploadError, match="not ~owner/ubuntu/name"):
        upload.incoming_dir("ppa:a/b")


def test_the_pinned_launchpad_fingerprint_is_the_documented_one():
    assert upload.LAUNCHPAD_FINGERPRINTS["ppa.launchpad.net"] == {"SHA256:MGq+4hxD7RduVTcfwlwwboZnsgJC6SL/NltM8ye+gNg"}


def test_private_keys_load_from_openssh_and_pem_and_a_locked_key_is_explained():
    k = paramiko.RSAKey.generate(2048)
    buf = io.StringIO()
    k.write_private_key(buf)
    assert upload.fingerprint(upload.load_private_key(buf.getvalue().encode())) == upload.fingerprint(k)
    locked = io.StringIO()
    k.write_private_key(locked, password="pw")
    with pytest.raises(upload.UploadError, match="GIT_PPA_SSH_PASSPHRASE"):
        upload.load_private_key(locked.getvalue().encode())
    assert upload.fingerprint(upload.load_private_key(locked.getvalue().encode(), "pw")) == upload.fingerprint(k)
    with pytest.raises(upload.UploadError, match="cannot read the ssh key"):
        upload.load_private_key(b"junk")


def test_wait_keeps_what_it_saw_when_launchpad_lags_or_does_not_answer(monkeypatch):
    built = {"status": "Published", "builds": [{"arch": "amd64", "state": "Successfully built", "link": "w"}]}
    pending = {"status": "Pending", "builds": [{"arch": "amd64", "state": "Needs building", "link": "w"}]}
    answers = [None, pending, None, lp.LaunchpadError("name resolution"), pending, built]
    seen = []

    def publication(ppa, source, version):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(upload, "publication", publication)
    monkeypatch.setattr(upload.time, "sleep", lambda s: None)
    assert upload.wait("~a/ubuntu/b", "src", "1-1", 60, every=1, log=seen.append) == built
    # the first empty answer is the starting state, the one after Pending is a lagging replica, neither is logged
    assert seen == ["Pending: amd64 Needs building",
                    "Launchpad did not answer (name resolution), still waiting", "Published: amd64 Successfully built"]


def test_publication_ignores_other_versions_and_does_not_ask_for_exact_match(monkeypatch):
    urls = []
    other = '{"source_package_name": "src", "source_package_version": "1-10", "status": "Published", "self_link": "https://x/o"}'

    def get(url, retries=3):
        urls.append(url)
        return ('{"entries": [%s]}' % other).encode()

    monkeypatch.setattr(lp, "_get", get)
    assert upload.publication("~a/ubuntu/b", "src", "1-1") is None
    assert "exact_match" not in urls[0] and "version=1-1" in urls[0]


def test_publication_and_wait_follow_the_builds(monkeypatch):
    pages = {
        "pub": '{"entries": [{"source_package_name": "src", "source_package_version": "1-1", "status": "Published", "pocket": "Release", "date_published": "d", "self_link": "https://x/pub1"}]}',
        "builds": '{"entries": [{"arch_tag": "amd64", "buildstate": "%s", "web_link": "w"}]}',
    }
    states = iter(["Needs building", "Currently building", "Successfully built"])
    seen = []

    def get(url, retries=3):
        if url.endswith("getBuilds"):
            return (pages["builds"] % next(states)).encode()
        return pages["pub"].encode() if "getPublishedSources" in url else b"{}"

    monkeypatch.setattr(lp, "_get", get)
    monkeypatch.setattr(upload.time, "sleep", lambda s: None)
    last = upload.wait("~a/ubuntu/b", "src", "1-1", 60, every=1, log=seen.append)
    assert last["builds"] == [{"arch": "amd64", "state": "Successfully built", "link": "w"}]
    assert seen == ["Published: amd64 Needs building", "Published: amd64 Currently building", "Published: amd64 Successfully built"]
    monkeypatch.setattr(lp, "_get", lambda url, retries=3: b'{"entries": []}')
    assert upload.publication("~a/ubuntu/b", "src", "1-1") is None
    assert upload.wait("~a/ubuntu/b", "src", "1-1", 0, log=seen.append) is None


def test_the_cli_refuses_a_ppa_the_profile_does_not_allow(source, monkeypatch, capsys, tmp_path):
    pkg, repo = source
    prof = tmp_path / "p.toml"
    prof.write_text(
        '[project]\ntag="acme"\nseries=["resolute"]\n[repo]\nurl="u"\nupstream_url="u"\n[branches]\nmirror="m"\ndownstream="d"\nstaging="s"\n'
        '[ppa]\nowner="acme"\n[safety]\nallow_ppas=["~acme/ubuntu/test"]\n[[package]]\nname="hello-acme"\narchive="hello"\nppa="live"\n'
    )
    monkeypatch.setenv("GIT_PPA_PROFILE", str(prof))
    monkeypatch.setenv("GIT_PPA_LPUSER", "u")
    assert cli.main(["upload", "hello-acme", "-C", str(repo), "--dry-run"]) == 4
    assert "~acme/ubuntu/live is not in [safety] allow_ppas" in capsys.readouterr().err
    assert cli.main(["upload", "hello-acme", "-C", str(repo), "--ppa", "~acme/ubuntu/test", "--dry-run"]) == 1
    assert "build with --sign-key-file first" in capsys.readouterr().err
    prof.write_text(prof.read_text().replace('ppa="live"\n', ''))
    assert cli.main(["upload", "hello-acme", "-C", str(repo), "--dry-run"]) == 1
    assert "no target PPA" in capsys.readouterr().err


def test_a_failed_build_restores_the_tree_it_found_clean(source, prof, monkeypatch):
    pkg, repo = source

    def fail(cmd, **kw):
        # what dpkg-source leaves when it stops after applying the patches
        (repo / "hello.txt").write_text("patched\n")
        (repo / ".pc").mkdir()
        (repo / ".pc/applied-patches").write_text("x\n")
        return subprocess.CompletedProcess(cmd, 29, "", "dpkg-source: error: boom\n")

    monkeypatch.setattr(build.subprocess, "run", fail)
    with pytest.raises(build.BuildError, match="dpkg-buildpackage failed:\n  dpkg-source: error: boom"):
        build.build(prof, pkg, "resolute", repo)
    assert gitutil.is_clean(repo) and (repo / "hello.txt").read_text() == "hello\n" and not (repo / ".pc").exists()
