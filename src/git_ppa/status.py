# SPDX-License-Identifier: GPL-3.0-or-later
"""status: how far is each downstream behind the Ubuntu pockets we follow."""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil, lp, versions
from .profile import Package, Profile

TOP_RE = re.compile(r"^(\S+) \(([^)]+)\)", re.M)


@dataclass
class Row:
    name: str
    archive: str
    downstream_version: str | None = None
    base: str | None = None
    newest: str | None = None
    pocket: str | None = None
    behind: bool | None = None
    entries_behind: int | None = None
    cves: int | None = None
    notify: str | None = None
    note: str = ""
    cve_ids: list[str] = field(default_factory=list)


def cache_dir() -> Path:
    if os.environ.get("GIT_PPA_CACHE"):
        return Path(os.environ["GIT_PPA_CACHE"])
    # a confined snap has no ~/.cache of its own, and $SNAP_USER_COMMON outlives refreshes
    if os.environ.get("SNAP_USER_COMMON"):
        return Path(os.environ["SNAP_USER_COMMON"]) / "cache"
    return Path.home() / ".cache" / "git-ppa"


def _bare(repo: Path, *args: str, check: bool = True):
    # safe.bareRepository=explicit refuses a bare repository found by discovery, so name it
    return gitutil.run("--git-dir", str(repo), *args, check=check)


def downstream_top_version(profile: Profile, pkg: Package, series: str, lpuser: str, refresh: bool) -> str:
    """Top version of debian/changelog on the downstream branch, through a shallow cached fetch."""
    names = profile.names(pkg, series)
    ref = f"refs/heads/{names['downstream']}"
    url = profile.downstream_url(pkg, lpuser)
    repo = cache_dir() / f"{pkg.name}.git"
    if not repo.exists():
        repo.mkdir(parents=True)
        _bare(repo, "init", "--bare", "-q")
    remote = gitutil.out("ls-remote", url, ref)
    if not remote:
        raise gitutil.GitError(("ls-remote", ref), 2, f"branch {names['downstream']} not found")
    sha = remote.split()[0]
    cp = _bare(repo, "rev-parse", "--verify", "--quiet", "refs/cache/tip", check=False)
    cached = cp.stdout.strip() if cp.returncode == 0 else None
    if refresh or cached != sha:
        _bare(repo, "fetch", "--depth", "1", "--no-tags", "-q", url, f"+{ref}:refs/cache/tip")
    cl = _bare(repo, "show", "refs/cache/tip:debian/changelog", check=False)
    m = TOP_RE.search(cl.stdout if cl.returncode == 0 else "")
    if not m:
        raise gitutil.GitError(("show", "debian/changelog"), 2, "cannot read the top changelog entry")
    return m.group(2)


def one(profile: Profile, pkg: Package, series: str, lpuser: str, include_large: bool, refresh: bool) -> Row:
    row = Row(pkg.name, pkg.archive)
    try:
        pubs = lp.published(pkg.archive, series)
        followed = lp.newest_in_pockets(pubs, profile.pockets)
        if followed:
            row.newest, row.pocket = followed.version, followed.pocket
        notify = lp.newest_in_pockets(pubs, profile.notify_pockets)
        if notify and (not followed or versions.gt(notify.version, followed.version)):
            row.notify = f"{notify.version} ({notify.pocket})"
        if pkg.large and not include_large:
            row.note = "base not read (large, use --include-large)"
            return row
        top = downstream_top_version(profile, pkg, series, lpuser, refresh)
        row.downstream_version = top
        row.base = versions.strip_suffix(top, profile.tag)
        if followed:
            row.behind = versions.gt(followed.version, row.base)
            if row.behind:
                text = lp.changelog_text(followed)
                row.entries_behind, ids = lp.changes_between(text, row.base, followed.version)
                row.cve_ids = sorted(ids)
                row.cves = len(ids)
    except lp.LaunchpadError as e:
        row.note = f"error: {str(e)[-150:]}"
    except gitutil.GitError as e:
        row.note = "error: " + (" ".join(e.stderr.split())[-170:] or str(e))
    return row


def run(profile: Profile, names: list[str], series: str, lpuser: str, include_large: bool, refresh: bool, jobs: int = 4) -> list[Row]:
    pkgs = [profile.package(n) for n in names] if names else list(profile.packages.values())
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = [ex.submit(one, profile, p, series, lpuser, include_large, refresh) for p in pkgs]
        return [f.result() for f in futs]


def render(rows: list[Row]) -> str:
    head = ("package", "downstream base", "newest followed", "pocket", "behind", "CVEs", "note")
    lines = [head]
    for r in rows:
        behind = "?" if r.behind is None else (f"yes ({r.entries_behind})" if r.behind else "no")
        note = r.note or (f"proposed: {r.notify}" if r.notify else "")
        lines.append((r.name, r.base or "-", r.newest or "-", r.pocket or "-", behind, "-" if r.cves is None else str(r.cves), note))
    widths = [max(len(str(l[i])) for l in lines) for i in range(len(head))]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(l, widths)).rstrip() for l in lines)
