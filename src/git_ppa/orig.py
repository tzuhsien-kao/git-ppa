# SPDX-License-Identifier: GPL-3.0-or-later
"""orig: fetch the orig tarball(s) of the base version and name them for the downstream source.

The files come straight from Launchpad and are checked against the checksums in the archive's .dsc,
so nothing here needs ubuntu-dev-tools."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from debian import deb822
from debian.changelog import Changelog

from . import lp, versions
from .profile import Package, Profile


class OrigError(Exception):
    pass


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(profile: Profile, pkg: Package, series: str, repo: Path, outdir: Path, version: str | None = None) -> list[Path]:
    if version is None:
        with (repo / "debian/changelog").open(encoding="utf-8") as f:
            version = versions.strip_suffix(str(next(iter(Changelog(f))).version), profile.tag)
    pubs = [p for p in lp.published(pkg.archive, series, status=None) if p.version == version]
    if not pubs:
        raise OrigError(f"{pkg.archive} {version} is not published in {series}")
    urls = {u.rsplit("/", 1)[-1]: u for u in lp.source_file_urls(pubs[0])}
    dsc_name = next((n for n in urls if n.endswith(".dsc")), None)
    if not dsc_name:
        raise OrigError(f"no .dsc among the source files of {pkg.archive} {version}")
    with tempfile.TemporaryDirectory(prefix="git-ppa-orig-") as tmp:
        tmpdir = Path(tmp)
        lp.download(urls[dsc_name], tmpdir / dsc_name)
        with (tmpdir / dsc_name).open(encoding="utf-8", errors="replace") as f:
            dsc = deb822.Dsc(f)
        origs = [e for e in dsc.get("Checksums-Sha256", []) if ".orig" in e["name"]]
        if not origs:
            return []
        outdir.mkdir(parents=True, exist_ok=True)
        written = []
        for e in origs:
            name = e["name"]
            if name not in urls:
                raise OrigError(f"{name} is listed in the .dsc but not offered by Launchpad")
            src = tmpdir / name
            lp.download(urls[name], src)
            if src.stat().st_size != int(e["size"]) or _sha256(src) != e["sha256"]:
                raise OrigError(f"checksum mismatch for {name}")
            if profile.source_rename and name.startswith(f"{pkg.archive}_"):
                name = f"{pkg.name}_{name[len(pkg.archive) + 1:]}"
            dest = outdir / name
            shutil.copyfile(src, dest)
            written.append(dest)
        return written
