# SPDX-License-Identifier: GPL-3.0-or-later
"""Thin git wrapper. Everything shells out to plain git."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import credentials


class GitError(Exception):
    def __init__(self, args: tuple, rc: int, stderr: str):
        self.rc = rc
        self.stderr = stderr.strip()
        super().__init__(f"git {' '.join(args)} failed ({rc}): {self.stderr}")


class ToolMissing(Exception):
    pass


HINTS = {
    "git": "install git",
    "dpkg-mergechangelogs": "install the dpkg-dev package, without it every merge conflicts in debian/changelog",
}

MERGE_DRIVER_ARGS = "-m %O %A %B %A"


def require(*names: str) -> None:
    """Fail early with a hint when a tool is neither in the snap nor on the host."""
    missing = [
        f"{n} not found, {HINTS.get(n, 'install it')}"
        for n in names
        if snap_bin(n) is None and shutil.which(n) is None
    ]
    if missing:
        raise ToolMissing("; ".join(missing))


def snap_bin(name: str) -> str | None:
    """A tool that ships in the snap, found through $SNAP so that it works whatever PATH is."""
    snap = os.environ.get("SNAP")
    if snap and (Path(snap) / "usr/bin" / name).exists():
        return str(Path(snap) / "usr/bin" / name)
    return None


def snap_git() -> str | None:
    return snap_bin("git")


def _perl_lib(snap: Path) -> str:
    dirs = [*snap.glob("usr/share/perl5"), *snap.glob("usr/lib/*/perl5/*"), *snap.glob("usr/share/perl/*"),
            *snap.glob("usr/lib/*/perl/*"), *snap.glob("usr/lib/*/perl-base")]
    return ":".join(str(d) for d in dirs if d.is_dir())


def _stable(path: str) -> str:
    """Name the snap through its `current` link, so that a path written into a clone outlives a refresh."""
    snap, name = os.environ.get("SNAP"), os.environ.get("SNAP_NAME")
    if snap and name and path.startswith(snap):
        return f"/snap/{name}/current" + path[len(snap):]
    return path


def changelog_driver() -> str:
    """Command line of the git merge driver for debian/changelog."""
    script, perl = snap_bin("dpkg-mergechangelogs"), snap_bin("perl")
    if not (script and perl):
        return f"dpkg-mergechangelogs {MERGE_DRIVER_ARGS}"
    # dpkg-mergechangelogs is a Perl script, its shebang and modules would be the base's
    snap = Path(os.environ["SNAP"])
    lib = ":".join(_stable(d) for d in _perl_lib(snap).split(":"))
    # Dpkg::Vendor reads /etc/dpkg/origins, which a confined snap is not allowed to open
    origins = f"DPKG_ORIGINS_DIR={_stable(str(snap / 'usr/share/dpkg-origins'))} "
    return f"{origins}PERL5LIB={lib} {_stable(perl)} {_stable(script)} {MERGE_DRIVER_ARGS}"


def _git_command(env: dict[str, str]) -> str:
    exe = snap_git()
    if not exe:
        return "git"
    # Debian's git looks for its helpers and templates under /usr, which would be the host's
    snap = env["SNAP"]
    env["GIT_EXEC_PATH"] = f"{snap}/usr/lib/git-core"
    env["GIT_TEMPLATE_DIR"] = f"{snap}/usr/share/git-core/templates"
    return exe


def _default_gitconfig(env: dict[str, str]) -> None:
    """A confined snap cannot read ~/.gitconfig, give it an identity for the merge commits."""
    common = env.get("SNAP_USER_COMMON")
    home = env.get("HOME")
    if "GIT_CONFIG_GLOBAL" in env or not common or (home and (Path(home) / ".gitconfig").exists()):
        return
    cfg = Path(common) / "gitconfig"
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("[user]\n\tname = git-ppa\n\temail = git-ppa@localhost\n")
    env["GIT_CONFIG_GLOBAL"] = str(cfg)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    # only for a downstream that is reached over ssh, the snap does not carry an ssh client
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=20")
    if env.get("SNAP"):
        _default_gitconfig(env)
    return env


def _auth_hint(stderr: str) -> str:
    """git says only that it could not read a username when a private repository is asked without a token."""
    if re.search(r"could not read (Username|Password)|unable to get password|terminal prompts disabled|Authentication failed|Invalid username or password|error: 40[13]", stderr):
        return "\nrun `git ppa login` with a Launchpad access token that has the repository:pull scope"
    return ""


def _credential_args() -> list[str]:
    args: list[str] = []
    helpers = credentials.helpers()
    for h in helpers:
        args += ["-c", f"credential.{credentials.SCOPE}.helper={h}"]
    if helpers:
        # a token is made for one repository, so git must ask for credentials by path
        args += ["-c", f"credential.{credentials.SCOPE}.useHttpPath=true"]
    return args


def run(*args: str, cwd: str | Path | None = None, check: bool = True, timeout: int = 900, input_: str | None = None) -> subprocess.CompletedProcess:
    env = _env()
    try:
        p = subprocess.run(
            [_git_command(env), "-c", "commit.gpgsign=false", *_credential_args(), *args],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, input=input_,
        )
    except FileNotFoundError:
        raise ToolMissing(f"git not found, {HINTS['git']}") from None
    if check and p.returncode != 0:
        raise GitError(args, p.returncode, p.stderr + _auth_hint(p.stderr))
    return p


def out(*args: str, cwd: str | Path | None = None, check: bool = True) -> str:
    return run(*args, cwd=cwd, check=check).stdout.strip()


def ok(*args: str, cwd: str | Path | None = None) -> bool:
    return run(*args, cwd=cwd, check=False).returncode == 0


def rev(ref: str, cwd: str | Path) -> str | None:
    p = run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd, check=False)
    return p.stdout.strip() if p.returncode == 0 else None


def show(ref: str, path: str, cwd: str | Path) -> str:
    p = run("show", f"{ref}:{path}", cwd=cwd, check=False)
    return p.stdout if p.returncode == 0 else ""


def is_clean(cwd: str | Path) -> bool:
    return out("status", "--porcelain", cwd=cwd) == ""


def current_branch(cwd: str | Path) -> str | None:
    p = run("symbolic-ref", "--quiet", "--short", "HEAD", cwd=cwd, check=False)
    return p.stdout.strip() or None


def register_credential_helper(cwd: str | Path) -> None:
    """Let a plain `git fetch` in the clone find the stored tokens as well, the file is the only thing it points to."""
    if credentials.path().exists():
        run("config", "--replace-all", f"credential.{credentials.SCOPE}.helper", credentials.file_helper(credentials.path()), cwd=cwd)
        run("config", f"credential.{credentials.SCOPE}.useHttpPath", "true", cwd=cwd)


def register_changelog_driver(cwd: str | Path) -> None:
    """Merge driver for debian/changelog, local to this clone only (nothing committed)."""
    run("config", "merge.dpkg-mergechangelogs.name", "debian/changelog merge driver", cwd=cwd)
    run("config", "merge.dpkg-mergechangelogs.driver", changelog_driver(), cwd=cwd)
    git_dir = Path(out("rev-parse", "--git-dir", cwd=cwd))
    if not git_dir.is_absolute():
        git_dir = Path(cwd) / git_dir
    info = git_dir / "info"
    info.mkdir(exist_ok=True)
    attrs = info / "attributes"
    line = "debian/changelog merge=dpkg-mergechangelogs\n"
    existing = attrs.read_text() if attrs.exists() else ""
    if line not in existing:
        attrs.write_text(existing + line)
