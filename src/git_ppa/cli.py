# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line. Exit codes: 0 ok, 1 error or lint failure, 2 status --check found a package behind,
3 merge stopped with conflicts, 4 refused by a safety check."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

from . import __version__, credentials, gitutil, lint, lp, merge, orig, profile as profile_mod, status, versions, workspace


class CliError(Exception):
    pass


def _lpuser(args: argparse.Namespace) -> str:
    user = args.lpuser or os.environ.get("GIT_PPA_LPUSER")
    if not user:
        for key in ("gitppa.lpuser", "gitubuntu.lpuser"):
            p = gitutil.run("config", "--get", key, check=False)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
    if not user:
        raise CliError("no Launchpad user, use --lpuser, GIT_PPA_LPUSER or git config gitppa.lpuser")
    return user


def _user_for(args: argparse.Namespace, prof: profile_mod.Profile) -> str:
    """The Launchpad user is only needed when the repository URL has a {lpuser} in it (ssh)."""
    return _lpuser(args) if "{lpuser}" in prof.repo_url else ""


def _profile(args: argparse.Namespace) -> profile_mod.Profile:
    path = args.profile or os.environ.get("GIT_PPA_PROFILE")
    if not path:
        raise CliError("no profile, use --profile FILE or GIT_PPA_PROFILE")
    return profile_mod.load(path)


def _repo(args: argparse.Namespace) -> Path:
    repo = Path(args.directory or ".").resolve()
    if not (repo / ".git").exists():
        raise CliError(f"{repo} is not a git repository, use -C DIR")
    return repo


def _pkg(profile: profile_mod.Profile, args: argparse.Namespace, repo: Path) -> profile_mod.Package:
    if getattr(args, "package", None):
        return profile.package(args.package)
    url = gitutil.out("remote", "get-url", "origin", cwd=repo, check=False)
    name = re.sub(r"(\.git)?/?$", "", url).rsplit("/", 1)[-1]
    if name in profile.packages:
        return profile.packages[name]
    raise CliError("cannot tell the package from the origin remote, pass it as an argument")


def _target(profile: profile_mod.Profile, pkg: profile_mod.Package, args: argparse.Namespace, series: str) -> str:
    if getattr(args, "to", None):
        return args.to
    newest = lp.newest_in_pockets(lp.published(pkg.archive, series), profile.pockets)
    if not newest:
        raise CliError(f"{pkg.archive} has no published version in {', '.join(profile.pockets)} for {series}")
    return newest.version


def _emit(args: argparse.Namespace, data, text: str) -> None:
    print(json.dumps(data, indent=2) if args.json else text)


def cmd_status(args: argparse.Namespace) -> int:
    prof = _profile(args)
    series = args.series or prof.series[0]
    rows = status.run(prof, args.packages, series, _user_for(args, prof), args.include_large, args.refresh, args.jobs)
    _emit(args, [r.__dict__ for r in rows], status.render(rows))
    if any(r.note.startswith("error") for r in rows):
        return 1
    return 2 if args.check and any(r.behind for r in rows) else 0


def cmd_clone(args: argparse.Namespace) -> int:
    prof = _profile(args)
    pkg = prof.package(args.package)
    series = args.series or prof.series[0]
    dest = Path(args.dest or pkg.name)
    workspace.clone(prof, pkg, series, _user_for(args, prof), dest)
    print(f"cloned {pkg.name} into {dest}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    if args.push:
        raise merge.Refused("pushing to Launchpad is not implemented in phase 1, sync only updates the local mirror branch")
    prof = _profile(args)
    repo = _repo(args)
    pkg = _pkg(prof, args, repo)
    series = args.series or prof.series[0]
    target = _target(prof, pkg, args, series)
    old, new = workspace.sync_local(prof, pkg, series, repo, target)
    names = prof.names(pkg, series)
    print(f"{names['mirror']}: {old[:12]} -> {new[:12]} ({target})" if old != new else f"{names['mirror']} already at {target}")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    prof = _profile(args)
    repo = _repo(args)
    pkg = _pkg(prof, args, repo)
    series = args.series or prof.series[0]
    if args.abort:
        for line in merge.abort(prof, pkg, series, repo):
            print(line)
        return 0
    target = _target(prof, pkg, args, series)
    try:
        report = merge.merge_start(prof, pkg, series, repo, target, args.force)
    except merge.UpToDate as e:
        print(f"up to date: {e}")
        return 0
    _emit(args, report.to_dict(), merge.render(report))
    return 0 if report.merged else 3


def _login_targets(args: argparse.Namespace, prof: profile_mod.Profile, user: str) -> list[tuple[str, str]]:
    names = list(prof.packages) if args.all else args.packages
    if not names:
        raise CliError("name the packages the token is for, or use --all for a token that covers every repository")
    return [(n, prof.downstream_url(prof.package(n), user)) for n in names]


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: dict = {}
    for k, v in pairs:
        if k in seen:
            raise CliError(f"{k!r} appears twice")
        seen[k] = v
    return seen


def _read_tokens(source: str, prof: profile_mod.Profile) -> list[tuple[str, str]]:
    """A JSON object of package name to token, `*` is a token for every package not named.

    `-` reads standard input, which is how a CI secret is passed without a file.
    """
    if source == "-":
        text, where = sys.stdin.read(), "standard input"
    else:
        f = Path(source).expanduser()
        if not f.is_file():
            raise CliError(f"{f} is not a file")
        if f.stat().st_mode & 0o077:
            raise CliError(f"{f} can be read by other users, run `chmod 600 {f}` first")
        text, where = f.read_text(), str(f)
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (json.JSONDecodeError, CliError) as e:
        raise CliError(f"{where} is not a JSON object of package to token: {e}") from None
    if not isinstance(data, dict) or not data:
        raise CliError(f"{where} must be a JSON object of package to token, with at least one entry")
    named: dict[str, str] = {}
    fallback = None
    for key, token in data.items():
        if not isinstance(token, str) or not token.strip() or token != token.strip() or " " in token:
            raise CliError(f"{where}: the token of {key!r} must be a non-empty string without spaces")
        if key == "*":
            fallback = token
            continue
        name = prof.package(key).name
        if name in named:
            raise CliError(f"{where}: {name} appears twice")
        named[name] = token
    if fallback:
        for name in prof.packages:
            named.setdefault(name, fallback)
    return list(named.items())


def _keep_if_it_reads(name: str, url: str, token: str, user: str, verify: bool) -> bool:
    """Store the token for one repository, keep it only if the repository can be read with it."""
    repo = credentials.repo_path(url)
    before = credentials.snapshot()
    credentials.save(user, token, repo)
    if not verify:
        print(f"{name}: token saved, not verified")
        return True
    p = gitutil.run("ls-remote", "--heads", url, check=False)
    if p.returncode == 0:
        print(f"{name}: token saved and verified")
        return True
    credentials.remove([repo])
    if repo in before:
        credentials.restore({repo: before[repo]})
    lines = p.stderr.strip().splitlines()
    print(f"{name}: token refused, not kept ({lines[-1] if lines else p.returncode})", file=sys.stderr)
    return False


def cmd_login(args: argparse.Namespace) -> int:
    prof = _profile(args)
    user = _lpuser(args)
    if args.from_file:
        if args.packages or args.all:
            raise CliError("--from-file takes the package names from the file")
        work = [(n, prof.downstream_url(prof.package(n), user), t) for n, t in _read_tokens(args.from_file, prof)]
    else:
        targets = _login_targets(args, prof, user)
        token = os.environ.get("GIT_PPA_TOKEN") or (
            getpass.getpass(f"Launchpad access token for {', '.join(n for n, _ in targets)} ({user}): ")
            if sys.stdin.isatty() else sys.stdin.readline()
        ).strip()
        if not token:
            raise CliError("no token, type it at the prompt or pipe it in")
        work = [(n, u, token) for n, u in targets]
    failed = sum(not _keep_if_it_reads(n, u, t, user, not args.no_verify) for n, u, t in work)
    print(f"tokens are kept in {credentials.path()}")
    return 1 if failed else 0


def cmd_logout(args: argparse.Namespace) -> int:
    if not (args.packages or args.all):
        print(f"{credentials.remove()} token(s) removed")
        return 0
    prof = _profile(args)
    targets = _login_targets(args, prof, _user_for(args, prof))
    print(f"{credentials.remove([credentials.repo_path(u) for _, u in targets])} token(s) removed")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    prof = _profile(args)
    repo = _repo(args)
    pkg = _pkg(prof, args, repo)
    findings = lint.run(repo, prof, pkg, args.rule or None)
    _emit(args, [f.to_dict() for f in findings], lint.render(findings))
    return 1 if any(f.severity == "error" for f in findings) else 0


def cmd_orig(args: argparse.Namespace) -> int:
    prof = _profile(args)
    repo = _repo(args)
    pkg = _pkg(prof, args, repo)
    series = args.series or prof.series[0]
    outdir = Path(args.outdir) if args.outdir else repo.parent
    files = orig.fetch(prof, pkg, series, repo, outdir, args.version)
    if not files:
        print("no orig tarball, native source package")
    for f in files:
        print(f"{f}  {f.stat().st_size} bytes")
    return 0


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        # argparse exits 2 by default, which would collide with `status --check` meaning "behind"
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _common(p: argparse.ArgumentParser, sub: bool) -> None:
    """Global options. On a subcommand an absent option must not override one given before it."""
    d = argparse.SUPPRESS if sub else None
    p.add_argument("--profile", default=d, help="profile file, or GIT_PPA_PROFILE")
    p.add_argument("--series", default=d, help="Ubuntu series, default is the first one in the profile")
    p.add_argument("--lpuser", default=d, help="Launchpad user, or GIT_PPA_LPUSER")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS if sub else False, help="machine readable output")
    p.add_argument("-C", dest="directory", default=d, help="run in this repository")


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="git-ppa", description="Keep a downstream PPA delta current with the Ubuntu archive")
    p.add_argument("--version", action="version", version=__version__)
    _common(p, sub=False)
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def add(name: str, help: str) -> argparse.ArgumentParser:
        s = sub.add_parser(name, help=help)
        _common(s, sub=True)
        return s

    s = add("status", "how far behind the followed Ubuntu pockets is each downstream")
    s.add_argument("packages", nargs="*")
    s.add_argument("--check", action="store_true", help="exit 2 if any package is behind")
    s.add_argument("--include-large", action="store_true", help="also read the base of packages marked large (slow first time)")
    s.add_argument("--refresh", action="store_true", help="refetch the cached downstream branches")
    s.add_argument("--jobs", type=int, default=4)
    s.set_defaults(func=cmd_status)

    s = add("clone", "clone a downstream repo and add the Ubuntu import as a remote")
    s.add_argument("package")
    s.add_argument("dest", nargs="?")
    s.set_defaults(func=cmd_clone)

    s = add("sync", "fast-forward the local mirror branch to the import tag")
    s.add_argument("package", nargs="?")
    s.add_argument("--to", help="archive version, default is the newest followed")
    s.add_argument("--push", action="store_true", help="not implemented in phase 1")
    s.set_defaults(func=cmd_sync)

    s = add("merge", "merge an archive import tag into the downstream branch")
    s.add_argument("action", choices=["start"])
    s.add_argument("package", nargs="?")
    s.add_argument("--to", help="archive version, default is the newest followed")
    s.add_argument("--force", action="store_true", help="recreate the staging branch")
    s.add_argument("--abort", action="store_true", help="discard the staging branch and the merge tags")
    s.set_defaults(func=cmd_merge)

    s = add("login", "store a Launchpad access token for the repositories of some packages")
    s.add_argument("packages", nargs="*", help="a token made for one repository covers one package")
    s.add_argument("--all", action="store_true", help="the token covers every package of the profile (a project token)")
    s.add_argument("--from-file", metavar="FILE", help="read a JSON object of package to token from FILE (mode 600), or from standard input with -")
    s.add_argument("--no-verify", action="store_true", help="do not try the token on the repositories")
    s.set_defaults(func=cmd_login)

    s = add("logout", "remove stored tokens, those of the named packages or all of them")
    s.add_argument("packages", nargs="*")
    s.add_argument("--all", action="store_true", help="every package of the profile")
    s.set_defaults(func=cmd_logout)

    s = add("lint", "check packaging rules")
    s.add_argument("package", nargs="?")
    s.add_argument("--rule", action="append")
    s.set_defaults(func=cmd_lint)

    s = add("orig", "fetch the orig tarball and rename it for the downstream source")
    s.add_argument("package", nargs="?")
    s.add_argument("--version", help="archive version, default is the base of the changelog")
    s.add_argument("--outdir", help="default is the directory above the repository")
    s.set_defaults(func=cmd_orig)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except merge.Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 4
    except (CliError, profile_mod.ProfileError, workspace.WorkspaceError, orig.OrigError, lp.LaunchpadError, gitutil.GitError, gitutil.ToolMissing) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
