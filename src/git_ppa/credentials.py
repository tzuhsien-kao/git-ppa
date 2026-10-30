# SPDX-License-Identifier: GPL-3.0-or-later
"""A Launchpad access token for https access to private repositories.

Launchpad accepts an access token as the https password together with the Launchpad user name. The
token is kept in a file in git-credential-store format, readable by the owner only, and handed to git
through `credential.helper`, so it never appears in a command line or in a clone's remote URL.

A Launchpad token is made for one repository (or one project, which the user may not be allowed to
create), so the file holds one line per repository and git is told to match on the path as well.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

# git-ppa works on Launchpad PPAs, whose git repositories are on this host
SCOPE = "https://git.launchpad.net"


def path() -> Path:
    if os.environ.get("GIT_PPA_CREDENTIALS"):
        return Path(os.environ["GIT_PPA_CREDENTIALS"])
    # the common directory of a snap survives refreshes
    if os.environ.get("SNAP_USER_COMMON"):
        return Path(os.environ["SNAP_USER_COMMON"]) / "credentials"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "git-ppa" / "credentials"


def repo_path(url: str) -> str:
    """The path git asks credentials for, `~team/project/+git/name` for a Launchpad repository URL."""
    return urlsplit(url).path.lstrip("/")


def _entries() -> dict[str, str]:
    p = path()
    lines = p.read_text().splitlines() if p.exists() else []
    return {_key(l): l for l in lines if l.strip()}


def _key(line: str) -> str:
    return unquote(line.split("@", 1)[1].split("/", 1)[1]) if "/" in line.split("@", 1)[-1] else ""


def _write(entries: dict[str, str]) -> None:
    p = path()
    if not entries:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(l + "\n" for l in entries.values()))
    os.replace(tmp, p)


def save(user: str, token: str, repo: str) -> Path:
    """One line per repository, a token made for one repository is refused by every other one."""
    host = SCOPE.split("://", 1)[1]
    entries = _entries()
    entries[repo] = f"https://{quote(user, safe='')}:{quote(token, safe='')}@{host}/{quote(repo, safe='/~+')}"
    _write(entries)
    return path()


def snapshot() -> dict[str, str]:
    return _entries()


def restore(entries: dict[str, str]) -> None:
    """Put back the lines of repositories as they were, used when a new token turns out to be refused."""
    current = _entries()
    for repo, line in entries.items():
        current[repo] = line
    _write(current)


def stored() -> list[str]:
    return sorted(_entries())


def remove(repos: list[str] | None = None) -> int:
    """Remove the tokens of these repositories, or the whole file. Returns how many were removed."""
    entries = _entries()
    gone = list(entries) if repos is None else [r for r in repos if r in entries]
    _write({k: v for k, v in entries.items() if k not in gone})
    return len(gone)


def helpers() -> list[str]:
    """`credential.helper` values for git, most specific source first."""
    found = []
    if os.environ.get("GIT_PPA_TOKEN") and os.environ.get("GIT_PPA_LPUSER"):
        # a CI token comes from the environment, the helper reads it there instead of carrying it in the arguments
        found.append('!f() { test "$1" = get && echo username=$GIT_PPA_LPUSER && echo password=$GIT_PPA_TOKEN; }; f')
    if path().exists():
        found.append(file_helper(path()))
    return found


def file_helper(file: Path) -> str:
    """Answer `get` from the file and nothing else.

    git's `store` helper erases an entry when the server refuses it. A token refused by one request
    (a wrong scope, a blip) would then disappear for good, so `store` and `erase` are ignored here.
    """
    return f'!f() {{ test "$1" = get && git credential-store --file={shlex.quote(str(file))} get; }}; f'
