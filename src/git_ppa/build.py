# SPDX-License-Identifier: GPL-3.0-or-later
"""build: a source package from a downstream checkout, signed when a key is given.

Source only, the builders of the PPA make the binaries. The orig tarball is included (-sa), a PPA accepts
an orig that it already has when it is identical, and a renamed source needs it the first time.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from debian.deb822 import Changes

from . import gitutil, lint, orig, signing, versions
from .profile import Package, Profile


class BuildError(Exception):
    pass


@dataclass
class Built:
    package: str
    version: str
    changes: str
    files: list[str]
    signed: bool
    key: str | None
    orig_fetched: bool
    warnings: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def changes_name(source: str, version: str) -> str:
    """Name dpkg gives the .changes of a source only build: the epoch is not part of it."""
    return f"{source}_{version.split(':', 1)[-1]}_source.changes"


def _orig_present(outdir: Path, source: str, upstream: str) -> bool:
    return any(outdir.glob(f"{source}_{upstream}.orig.tar.*"))


def upstream_version(version: str) -> str:
    v = version.split(":", 1)[-1]
    return v.rsplit("-", 1)[0] if "-" in v else v


def build(profile: Profile, pkg: Package, series: str, repo: Path, key: signing.Keyring | None = None) -> Built:
    warnings: list[str] = []
    text = (repo / "debian/changelog").read_text(encoding="utf-8")
    top = versions.top_version(text) or ""
    ours = versions.newest_of(text, pkg.name)
    if ours != top:
        raise BuildError(f"the top changelog entry is {top}, not an entry of {pkg.name} (newest is {ours}), run merge finish first")
    if not versions.has_suffix(top, profile.tag):
        raise BuildError(f"{top} has no +{profile.tag}<N> or ~{profile.tag}<N> suffix")
    if not gitutil.is_clean(repo):
        raise BuildError("working tree is not clean")
    # old findings are not ours to stop at, a new top entry that breaks a rule is
    for f in lint.run(repo, profile, pkg):
        if f.severity == "error" and f.rule == "version-order":
            raise BuildError(f"[{f.rule}] {f.message}")
        warnings.append(f"[{f.rule}] {f.message}")

    outdir = repo.parent
    fetched = False
    if not _orig_present(outdir, pkg.name, upstream_version(top)):
        orig.fetch(profile, pkg, series, repo, outdir)
        fetched = True

    cmd = ["dpkg-buildpackage", "-S", "-d", "-nc", "-sa"]
    env = None
    if key:
        cmd += [f"-k{key.fingerprint}"]
        env = key.sign_env()
    else:
        cmd += ["-us", "-uc"]
    try:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    except FileNotFoundError:
        raise BuildError("dpkg-buildpackage not found, install dpkg-dev") from None
    if r.returncode != 0:
        # it applies the patches before it builds and does not undo that when it fails, the tree was clean
        # when we started, so everything that differs now is its doing
        gitutil.run("checkout", "--", ".", cwd=repo, check=False)
        gitutil.run("clean", "-fdq", cwd=repo, check=False)
        tail = [l for l in (r.stderr or r.stdout).splitlines() if l.strip()][-6:]
        raise BuildError("dpkg-buildpackage failed:\n  " + "\n  ".join(tail))
    # dpkg-buildpackage leaves its list of products in the tree, nothing of ours
    (repo / "debian/files").unlink(missing_ok=True)

    changes = outdir / changes_name(pkg.name, top)
    if not changes.exists():
        raise BuildError(f"{changes.name} was not produced")
    names = [changes.name] + [x["name"] for x in Changes(changes.read_text()).get("Files", [])]
    signed = False
    if key:
        signed = key.verify(changes) and key.verify(outdir / next(n for n in names if n.endswith(".dsc")))
        if not signed:
            raise BuildError("the signature on the .changes or the .dsc does not verify")
    extra = gitutil.out("status", "--porcelain", cwd=repo)
    if extra:
        warnings.append("the build left files in the tree: " + " ".join(extra.split()))
    return Built(pkg.name, top, str(changes), names, signed, key.fingerprint if key else None, fetched, warnings)


def render(b: Built) -> str:
    lines = [
        f"package   {b.package}",
        f"version   {b.version}",
        f"signed    {'yes, key ' + b.key[-16:] if b.signed else 'no'}",
        f"orig      {'downloaded' if b.orig_fetched else 'already there'}",
        "files",
    ] + [f"  {n}" for n in b.files]
    lines += [f"warning   {w}" for w in b.warnings]
    return "\n".join(lines)
