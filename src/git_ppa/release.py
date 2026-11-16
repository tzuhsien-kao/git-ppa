# SPDX-License-Identifier: GPL-3.0-or-later
"""release: the human step. Sign what was reviewed, upload it to a PPA and wait for the build.

The reviewed thing is a tree hash. Nothing is signed, and no key is read, unless the checkout is clean and its
tree is the one named by --approve. The agent side (merge, finish, build, lint) never needs a key.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import build as build_mod, gitutil, lp, merge, signing, upload as upload_mod, versions
from .profile import Package, Profile

BUSY = upload_mod.BUSY
# exit code of the command when the package is uploaded and the build is not over, the others are those of the tool
BUILDING = 5


class ReleaseError(Exception):
    pass


@dataclass
class Released:
    package: str
    version: str
    ppa: str
    tree: str
    uploaded: bool = False
    publication: dict | None = None
    code: int = 0
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def check_approval(tree: str, approve: str | None) -> None:
    if not approve or len(approve) < 12 or not tree.startswith(approve.lower()):
        raise merge.Refused(f"--approve {tree[:12]} is needed, the first 12 or more characters of the tree that was reviewed")


def release(prof: Profile, pkg: Package, series: str, repo: Path, ppa: str, approve: str | None,
            sign_key_file: str | None, ssh_key_file: str | None, wait: int, lpuser: str,
            log: Callable[[str], None] = print) -> Released:
    if ppa not in prof.allow_ppas:
        raise merge.Refused(f"{ppa} is not in [safety] allow_ppas of the profile, the profile has to name every PPA it may write to")
    if not gitutil.is_clean(repo):
        raise merge.Refused(f"{repo} has changes that are not committed, the tree that was reviewed is the committed one")
    tree = gitutil.out("rev-parse", "HEAD^{tree}", cwd=repo)
    check_approval(tree, approve)
    version = versions.newest_of((repo / "debian/changelog").read_text(encoding="utf-8"), pkg.name)
    if not version:
        raise ReleaseError(f"debian/changelog of {repo} has no entry of {pkg.name} with the suffix of the profile, run merge finish first")
    r = Released(pkg.name, version, ppa, tree)

    # a version is uploaded once per PPA, so a run that stopped after the upload only waits, with no key
    if version in lp.ppa_versions(ppa, pkg.name):
        log(f"{version} is already in {ppa}")
    else:
        if not sign_key_file:
            raise ReleaseError("--sign-key-file FILE or - is needed, Launchpad accepts signed uploads only")
        if not ssh_key_file:
            raise ReleaseError(f"--ssh-key-file FILE or - is needed for the upload to {ppa}")
        if sign_key_file == "-" and ssh_key_file == "-":
            raise ReleaseError("only one of --sign-key-file and --ssh-key-file can be read from standard input")
        secret = signing.read_secret(sign_key_file)
        ssh_key = upload_mod.load_private_key(signing.read_secret(ssh_key_file), os.environ.get("GIT_PPA_SSH_PASSPHRASE"))
        with signing.Keyring(secret, os.environ.get("GIT_PPA_GPG_PASSPHRASE")) as key:
            built = build_mod.build(prof, pkg, series, repo, key)
        if gitutil.out("rev-parse", "HEAD^{tree}", cwd=repo) != tree or not gitutil.is_clean(repo):
            raise merge.Refused("the tree changed while it was being built, nothing was uploaded")
        log(f"built and signed {built.changes}")
        upload_mod.upload(repo.parent / build_mod.changes_name(pkg.name, version), ppa, lpuser, ssh_key)
        r.uploaded = True
        log(f"uploaded to {ppa}")

    state = upload_mod.wait(ppa, pkg.name, version, wait, log=log)
    r.publication = state
    if state is None or not state["builds"] or any(b["state"] in BUSY for b in state["builds"]):
        r.code, r.message = BUILDING, f"not built yet after {wait} s, run the same command again, no key is needed any more"
        return r
    failed = [b for b in state["builds"] if b["state"] != "Successfully built"]
    if failed:
        r.code, r.message = 1, f"the build failed in {ppa}: " + ", ".join(f"{b['arch']} {b['state']} {b['link']}" for b in failed)
        return r
    r.message = f"{version} is built in {ppa}"
    return r


def render(r: Released) -> str:
    return "\n".join([f"{r.package} {r.version}", f"  ppa       {r.ppa}", f"  tree      {r.tree}", f"  {r.message}"])
