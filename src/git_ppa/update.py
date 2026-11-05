# SPDX-License-Identifier: GPL-3.0-or-later
"""update-package: the interface of the PE team's draft spec, for the steps git-ppa has.

The steps run in order and record what is done in the repository, so a run that stops (conflicts,
a build still running, an error) goes on with --continue DIR. Options of the spec git-ppa does not
implement are refused, never ignored, so adding them later does not change what a run does.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from . import build as build_mod, gitutil, lp, merge, signing, upload as upload_mod, versions, workspace
from .profile import Package, Profile

STEPS = ("clone", "merge", "finish", "build", "upload", "wait")
BUSY = ("Needs building", "Currently building", "Uploading build", "Dependency wait")

# exit codes, the others are those of the tool (0 ok, 1 error, 3 conflicts, 4 refused)
BUILDING = 5


class UpdateError(Exception):
    pass


@dataclass
class Options:
    pkg_name: str
    ppa_name: str
    base: str = "git-ubuntu"
    source: str = "archive"
    build: list[str] = field(default_factory=lambda: ["ppa"])
    manual_review: bool = True
    wait: int = 3600
    sign_key_file: str | None = None
    ssh_key_file: str | None = None
    me: bool = False
    skip_ppa_check: bool = False
    # spec options git-ppa does not implement yet, kept so they can be refused
    ai_model: str | None = None
    review_model: str | None = None
    budget: str | None = None
    bug: str | None = None
    ppa_dependencies: str | None = None


# option name as written on the command line and in config.yaml, and the Options field
KEYS = {
    "pkg-name": "pkg_name", "ppa-name": "ppa_name", "base": "base", "from": "source", "build": "build",
    "manual-review": "manual_review", "wait": "wait", "sign-key-file": "sign_key_file",
    "ssh-key-file": "ssh_key_file", "me": "me", "skip-ppa-check": "skip_ppa_check",
    "ai-model": "ai_model", "review-model": "review_model", "budget": "budget", "bug": "bug",
    "ppa-dependencies": "ppa_dependencies",
    # these belong to the whole tool, update-package passes them on
    "series": "series", "lp-user": "lpuser", "skillset": "profile", "continue": "workdir",
}


def load_config(path: str | Path) -> dict:
    """config.yaml of the spec: the same names as the options, without the dashes in front."""
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise UpdateError(f"cannot read {p}: {e}") from None
    if not isinstance(data, dict):
        raise UpdateError(f"{p} must be a mapping of option to value")
    out = {}
    for k, v in data.items():
        key = str(k).replace("_", "-")
        if key not in KEYS:
            raise UpdateError(f"{p}: unknown option {k!r}, known: {', '.join(sorted(KEYS))}")
        if key == "build" and isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        out[KEYS[key]] = v
    return out


def check(o: Options) -> None:
    """Refuse what the spec allows and git-ppa does not do yet."""
    if o.ai_model and o.review_model and o.ai_model == o.review_model:
        raise UpdateError("--review-model must not be the same as --ai-model")
    for name, value in (("--ai-model", o.ai_model), ("--review-model", o.review_model), ("--budget", o.budget),
                        ("--bug", o.bug), ("--ppa-dependencies", o.ppa_dependencies)):
        if value:
            raise UpdateError(f"{name} is not supported by git-ppa yet")
    if o.base != "git-ubuntu":
        raise UpdateError(f"--base {o.base} is not supported, git-ppa takes the base from git-ubuntu")
    if o.source != "archive":
        raise UpdateError(f"--from {o.source} is not supported, git-ppa follows the Ubuntu archive (--from archive)")
    if o.build != ["ppa"]:
        raise UpdateError(f"--build {','.join(o.build)} is not supported, git-ppa builds in a PPA only (--build ppa)")
    if not o.ppa_name:
        raise UpdateError("--ppa-name is needed, creating the staging PPA is not supported yet, name an existing one")
    if o.sign_key_file == "-" and o.ssh_key_file == "-":
        raise UpdateError("only one of --sign-key-file and --ssh-key-file can be read from standard input")


@dataclass
class Result:
    package: str
    workdir: str
    target: str | None = None
    version: str | None = None
    staging_ppa: str | None = None
    done: list[str] = field(default_factory=list)
    stopped: str = ""
    code: int = 0
    merge: dict | None = None
    publication: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _state_path(repo: Path) -> Path:
    git_dir = Path(gitutil.out("rev-parse", "--absolute-git-dir", cwd=repo))
    return git_dir / "git-ppa-update.json"


def _save(repo: Path, r: Result) -> None:
    _state_path(repo).write_text(json.dumps({k: getattr(r, k) for k in ("package", "target", "version", "staging_ppa", "done")}, indent=2))


def _load(repo: Path) -> dict:
    p = _state_path(repo)
    if not p.is_file():
        raise UpdateError(f"{repo} has no update-package state, it was not started by update-package")
    return json.loads(p.read_text())


def _newest_target(prof: Profile, pkg: Package, series: str) -> str:
    newest = lp.newest_in_pockets(lp.published(pkg.archive, series), prof.pockets)
    if not newest:
        raise UpdateError(f"{pkg.archive} has no published version in {', '.join(prof.pockets)} for {series}")
    return newest.version


def run(prof: Profile, pkg: Package, series: str, o: Options, workdir: Path, resume: bool, lpuser: str,
        log: Callable[[str], None] = print) -> Result:
    staging = prof.ppa_ref(o.ppa_name)
    if staging not in prof.allow_ppas:
        raise merge.Refused(f"{staging} is not in [safety] allow_ppas of the profile, the profile has to name every PPA it may write to")
    if staging == prof.ppa_reference(pkg):
        raise merge.Refused(f"{staging} is the target PPA of {pkg.name}, update-package uploads to a staging PPA and the target is reached by promote")
    r = Result(pkg.name, str(workdir), staging_ppa=staging)

    if resume:
        if not (workdir / ".git").exists():
            raise UpdateError(f"{workdir} is not a repository")
        state = _load(workdir)
        if state.get("package") != pkg.name:
            raise UpdateError(f"{workdir} is an update of {state.get('package')}, not {pkg.name}")
        if state.get("staging_ppa") != staging:
            raise UpdateError(f"{workdir} was started for {state.get('staging_ppa')}, not {staging}")
        r.target, r.version, r.done = state.get("target"), state.get("version"), list(state.get("done", []))
    else:
        if workdir.exists():
            raise UpdateError(f"{workdir} already exists, use --continue {workdir} to go on with it")
        r.target = _newest_target(prof, pkg, series)
        log(f"clone {pkg.name} into {workdir}")
        try:
            workspace.clone(prof, pkg, series, lpuser, workdir)
        except BaseException:
            # half a clone has no state, it could neither be started again nor continued
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        r.done.append("clone")
        _save(workdir, r)
    repo = workdir

    def step(name: str) -> None:
        r.done.append(name)
        _save(repo, r)

    # the keys are read before anything is written, a missing key should not stop a run half way
    secret = signing.read_secret(o.sign_key_file) if o.sign_key_file and "build" not in r.done else None
    ssh_key = None
    if "upload" not in r.done:
        if not o.ssh_key_file:
            raise UpdateError("--ssh-key-file FILE or - is needed for the upload to the staging PPA")
        ssh_key = upload_mod.load_private_key(signing.read_secret(o.ssh_key_file), os.environ.get("GIT_PPA_SSH_PASSPHRASE"))
    if "build" not in r.done and not secret:
        raise UpdateError("--sign-key-file FILE or - is needed, Launchpad accepts signed uploads only")

    if "merge" not in r.done:
        names = prof.names(pkg, series)
        git_dir = Path(gitutil.out("rev-parse", "--absolute-git-dir", cwd=repo))
        started = gitutil.current_branch(repo) == names["staging"] and gitutil.rev("new/ubuntu", repo)
        if started and (git_dir / "MERGE_HEAD").exists():
            r.stopped, r.code = "the merge has conflicts, resolve and commit them, then --continue", 3
            return r
        if started and gitutil.ok("merge-base", "--is-ancestor", "new/ubuntu", "HEAD", cwd=repo):
            log("merge resolved and committed")
        else:
            log(f"merge {r.target} from the archive")
            workspace.sync_local(prof, pkg, series, repo, r.target)
            try:
                report = merge.merge_start(prof, pkg, series, repo, r.target)
            except merge.UpToDate as e:
                r.stopped = f"up to date: {e}"
                return r
            r.merge = {"tier": report.tier, "risk": report.risk, "conflicts": len(report.conflicts)}
            if not report.merged:
                _save(repo, r)
                r.stopped, r.code = f"the merge has conflicts (tier {report.tier}, risk {report.risk}), resolve and commit them, then --continue", 3
                return r
        step("merge")

    if "finish" not in r.done:
        fin = merge.merge_finish(prof, pkg, series, repo, me=o.me, skip_ppa_check=o.skip_ppa_check, also_check=(staging,))
        r.version = fin.version
        log(f"changelog entry {fin.version}")
        step("finish")

    if "build" not in r.done:
        with signing.Keyring(secret, os.environ.get("GIT_PPA_GPG_PASSPHRASE")) as key:
            built = build_mod.build(prof, pkg, series, repo, key)
        log(f"built and signed {built.changes}")
        step("build")

    changes = repo.parent / build_mod.changes_name(pkg.name, r.version or "")
    if "upload" not in r.done:
        # a version can be uploaded once, a run that died after the upload must not try again
        if r.version in lp.ppa_versions(staging, pkg.name):
            log(f"{r.version} is already in {staging}")
        else:
            upload_mod.upload(changes, staging, lpuser, ssh_key)
            log(f"uploaded to {staging}")
        step("upload")

    if "wait" not in r.done:
        state = upload_mod.wait(staging, pkg.name, r.version or "", o.wait, log=log)
        r.publication = state
        if state is None or not state["builds"] or any(b["state"] in BUSY for b in state["builds"]):
            r.stopped, r.code = f"not built yet after {o.wait} s, run again with --continue {workdir}", BUILDING
            return r
        failed = [b for b in state["builds"] if b["state"] != "Successfully built"]
        if failed:
            r.stopped, r.code = "the build failed in the staging PPA: " + ", ".join(f"{b['arch']} {b['state']} {b['link']}" for b in failed), 1
            return r
        step("wait")

    target_ppa = prof.ppa_reference(pkg) or "the target PPA"
    if o.manual_review:
        r.stopped = f"built in {staging}, waiting for a review before promote to {target_ppa} (promote is not implemented yet)"
    else:
        r.stopped = f"built in {staging}, promote to {target_ppa} is not implemented yet"
    return r


def render(r: Result) -> str:
    lines = [f"{r.package} {r.version or ''}".rstrip(), f"  workdir   {r.workdir}", f"  staging   {r.staging_ppa}",
             f"  done      {', '.join(r.done) or 'nothing'}"]
    if r.merge:
        lines.append(f"  merge     tier {r.merge['tier']}, risk {r.merge['risk']}, {r.merge['conflicts']} conflict(s)")
    lines.append(f"  {r.stopped}")
    return "\n".join(lines)
