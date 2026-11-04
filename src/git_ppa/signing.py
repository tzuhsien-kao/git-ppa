# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign with a secret key that is only here for one command.

The key arrives as exported key material (a file or standard input), is imported into a temporary
GNUPGHOME, used, and deleted with the keyring. Nothing of the user's own keyring or agent is used, a strict
snap cannot reach them anyway.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


class SigningError(Exception):
    pass


def _base_dir() -> Path:
    # the agent socket lives in the keyring directory and its path is limited to about 100 characters
    for var in ("SNAP_USER_COMMON", "XDG_RUNTIME_DIR"):
        if os.environ.get(var):
            return Path(os.environ[var])
    return Path(tempfile.gettempdir())


def _snap_bin(name: str) -> str | None:
    snap = os.environ.get("SNAP")
    p = Path(snap) / "usr/bin" / name if snap else None
    return str(p) if p and p.exists() else None


class Keyring:
    """A throwaway keyring holding one secret key. Use as a context manager."""

    def __init__(self, secret: bytes, passphrase: str | None = None):
        if not secret.strip():
            raise SigningError("the secret key is empty")
        self.secret = secret
        self.passphrase = passphrase
        self.home: Path | None = None
        self.fingerprint = ""

    def __enter__(self) -> "Keyring":
        base = _base_dir()
        base.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="gp-", dir=base))
        self.home.chmod(0o700)
        conf = ["batch", "no-tty"]
        agent = _snap_bin("gpg-agent")
        if agent:
            # the agent path is built into gpg, and /usr/bin is not ours inside a snap
            snap_name = os.environ.get("SNAP_NAME")
            conf.append(f"agent-program /snap/{snap_name}/current/usr/bin/gpg-agent" if snap_name else f"agent-program {agent}")
        (self.home / "gpg.conf").write_text("\n".join(conf) + "\n")
        (self.home / "gpg-agent.conf").write_text("allow-loopback-pinentry\n")
        try:
            self._import()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self.home:
            subprocess.run(["gpgconf", "--homedir", str(self.home), "--kill", "gpg-agent"], capture_output=True, env=self.env())
            shutil.rmtree(self.home, ignore_errors=True)
            self.home = None

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.home:
            env["GNUPGHOME"] = str(self.home)
        return env

    def _gpg(self, *args: str, input_: bytes | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["gpg", *args], input=input_, capture_output=True, env=self.env())

    def _pass_args(self) -> list[str]:
        if self.passphrase is None:
            return ["--pinentry-mode", "loopback", "--passphrase", ""]
        return ["--pinentry-mode", "loopback", "--passphrase-file", str(self._passfile())]

    def _passfile(self) -> Path:
        p = self.home / "passphrase"
        if not p.exists():
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(self.passphrase or "")
        return p

    def _import(self) -> None:
        try:
            r = self._gpg(*self._pass_args(), "--import", input_=self.secret)
        except FileNotFoundError:
            raise SigningError("gpg not found, install gnupg") from None
        err = r.stderr.decode(errors="replace").strip()
        if r.returncode != 0 or "secret keys read: 1" not in err:
            last = err.splitlines()[-1] if err else "gpg gave no output"
            raise SigningError(f"cannot import the secret key: {last}")
        out = self._gpg("--list-secret-keys", "--with-colons").stdout.decode().splitlines()
        fprs = []
        for i, line in enumerate(out):
            if line.startswith("sec:"):
                nxt = next((l for l in out[i + 1:] if l.startswith("fpr:")), "")
                fprs.append(nxt.split(":")[9])
        if len(fprs) != 1:
            raise SigningError(f"expected one secret key, found {len(fprs)}, export only the signing key")
        self.fingerprint = fprs[0]
        # importing a protected key needs no passphrase, signing does, so find out now, not in the middle of a build
        t = self._gpg(*self._pass_args(), "--armor", "--clearsign", "--local-user", self.fingerprint, input_=b"test\n")
        if t.returncode != 0:
            raise SigningError("the key cannot sign, wrong or missing passphrase? "
                               + (t.stderr.decode(errors="replace").strip().splitlines() or [""])[-1])

    def sign_env(self) -> dict[str, str]:
        """Environment for dpkg-buildpackage -k<fingerprint>.

        dpkg-buildpackage runs gpg from PATH. With a passphrase a small script named gpg, first in PATH,
        adds it. --sign-command is not used, a recent dpkg-buildpackage takes it for another OpenPGP
        backend and calls it with other options.
        """
        env = self.env()
        if self.passphrase is not None:
            real = shutil.which("gpg", path=env.get("PATH"))
            if not real:
                raise SigningError("gpg not found, install gnupg")
            bindir = self.home / "bin"
            bindir.mkdir(exist_ok=True)
            script = bindir / "gpg"
            script.write_text(f'#!/bin/sh\nexec "{real}" --batch --pinentry-mode loopback --passphrase-file "{self._passfile()}" "$@"\n')
            script.chmod(stat.S_IRWXU)
            env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
        return env

    def verify(self, path: Path) -> bool:
        pub = self._gpg("--export", self.fingerprint).stdout
        with tempfile.TemporaryDirectory(dir=_base_dir()) as tmp:
            os.chmod(tmp, 0o700)
            env = dict(self.env(), GNUPGHOME=tmp)
            conf = Path(tmp) / "gpg.conf"
            agent = _snap_bin("gpg-agent")
            if agent and os.environ.get("SNAP_NAME"):
                conf.write_text(f"agent-program /snap/{os.environ['SNAP_NAME']}/current/usr/bin/gpg-agent\n")
            try:
                subprocess.run(["gpg", "--batch", "--import"], input=pub, capture_output=True, env=env, check=True)
                ok = subprocess.run(["gpg", "--batch", "--verify", str(path)], capture_output=True, env=env).returncode == 0
            finally:
                subprocess.run(["gpgconf", "--homedir", tmp, "--kill", "gpg-agent"], capture_output=True, env=env)
            return ok


def read_secret(source: str) -> bytes:
    """Key material from a file or standard input (-). A file other users can read is refused."""
    import sys

    if source == "-":
        return sys.stdin.buffer.read()
    f = Path(source).expanduser()
    if not f.is_file():
        raise SigningError(f"{f} is not a file")
    if f.stat().st_mode & 0o077:
        raise SigningError(f"{f} can be read by other users, run `chmod 600 {f}` first")
    return f.read_bytes()
