# SPDX-License-Identifier: GPL-3.0-or-later
"""clone and sync (local only, nothing here writes to Launchpad)."""

from __future__ import annotations

from pathlib import Path

from . import gitutil, versions
from .gitutil import GitError
from .profile import Package, Profile


class WorkspaceError(Exception):
    pass


def import_tag(profile: Profile, version: str) -> str:
    return f"{profile.upstream_remote}/import/{versions.enc_tag(version)}"


def clone(profile: Profile, pkg: Package, series: str, lpuser: str, dest: Path) -> Path:
    names = profile.names(pkg, series)
    if dest.exists():
        raise WorkspaceError(f"{dest} already exists")
    gitutil.run("clone", "-q", "--origin", "origin", profile.downstream_url(pkg, lpuser), str(dest))
    remote = profile.upstream_remote
    # --no-tags stops git from auto-following upstream tags under their own names, which would
    # put hundreds of import/* tags into this clone where a careless `git push --tags` sends them to our repo
    gitutil.run("remote", "add", "--no-tags", remote, profile.upstream_repo_url(pkg), cwd=dest)
    # only the importer tags are needed, they are kept under <remote>/ so they never clash with downstream tags
    gitutil.run("config", "--replace-all", f"remote.{remote}.fetch", f"+refs/tags/import/*:refs/tags/{remote}/import/*", cwd=dest)
    gitutil.run("fetch", "-q", remote, cwd=dest)
    for b in (names["mirror"], names["downstream"]):
        if not gitutil.rev(f"refs/remotes/origin/{b}", dest):
            raise WorkspaceError(f"origin has no branch {b}")
    gitutil.run("checkout", "-q", "-B", names["downstream"], f"origin/{names['downstream']}", cwd=dest)
    gitutil.run("branch", "-q", "-f", names["mirror"], f"origin/{names['mirror']}", cwd=dest)
    gitutil.register_changelog_driver(dest)
    gitutil.register_credential_helper(dest)
    return dest


def sync_local(profile: Profile, pkg: Package, series: str, repo: Path, target: str) -> tuple[str, str]:
    """Fast-forward the local mirror branch to the import tag of target. Returns (old, new) shas."""
    names = profile.names(pkg, series)
    tag = import_tag(profile, target)
    new = gitutil.rev(tag, repo)
    if not new:
        raise WorkspaceError(f"no import tag {tag}, fetch the {profile.upstream_remote} remote first")
    mirror = names["mirror"]
    old = gitutil.rev(f"refs/heads/{mirror}", repo)
    if not old:
        raise WorkspaceError(f"no local branch {mirror}")
    if old == new:
        return old, new
    if not gitutil.ok("merge-base", "--is-ancestor", old, new, cwd=repo):
        raise WorkspaceError(f"{mirror} is not an ancestor of {tag}, refusing a non fast-forward update")
    if gitutil.current_branch(repo) == mirror:
        gitutil.run("merge", "-q", "--ff-only", tag, cwd=repo)
    else:
        gitutil.run("update-ref", f"refs/heads/{mirror}", new, old, cwd=repo)
    return old, new
