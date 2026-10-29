# SPDX-License-Identifier: GPL-3.0-or-later
"""Profile: everything project specific lives here, the core never names a project."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class Package:
    name: str
    archive: str
    ppa: str = ""
    large: bool = False
    in_archive: bool = True


@dataclass
class Risk:
    rules_markers: list[str] = field(default_factory=list)
    control_fields: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


@dataclass
class Caps:
    low: tuple[int, int, int] = (2, 3, 10)
    high: tuple[int, int, int] = (1, 1, 3)


@dataclass
class Profile:
    tag: str
    series: list[str]
    pockets: list[str]
    notify_pockets: list[str]
    repo_url: str
    upstream_url: str
    upstream_remote: str
    mirror_branch: str
    downstream_branch: str
    staging_branch: str
    source_rename: bool
    binaries_mode: str
    rename_suffix: str
    relations: list[str]
    risk: Risk
    caps: Caps
    rules_enabled: list[str]
    identity_allowed: list[str]
    packages: dict[str, Package]

    def package(self, name: str) -> Package:
        if name in self.packages:
            return self.packages[name]
        for p in self.packages.values():
            if p.archive == name:
                return p
        raise ProfileError(f"unknown package {name!r}, known: {', '.join(self.packages)}")

    def expand(self, template: str, **kw: str) -> str:
        values = {"tag": self.tag, **kw}
        try:
            return template.format_map(values)
        except KeyError as e:
            raise ProfileError(f"template {template!r} needs variable {e}") from None

    def names(self, pkg: Package, series: str) -> dict[str, str]:
        return {
            "mirror": self.expand(self.mirror_branch, series=series, name=pkg.name, archive=pkg.archive),
            "downstream": self.expand(self.downstream_branch, series=series, name=pkg.name, archive=pkg.archive),
            "staging": self.expand(self.staging_branch, series=series, name=pkg.name, archive=pkg.archive),
        }

    def downstream_url(self, pkg: Package, lpuser: str) -> str:
        return self.expand(self.repo_url, name=pkg.name, archive=pkg.archive, lpuser=lpuser)

    def upstream_repo_url(self, pkg: Package) -> str:
        return self.expand(self.upstream_url, name=pkg.name, archive=pkg.archive)

    def binary_suffix(self) -> str:
        return self.expand(self.rename_suffix)


def from_dict(d: dict) -> Profile:
    try:
        proj = d["project"]
        repo = d["repo"]
        br = d["branches"]
    except KeyError as e:
        raise ProfileError(f"profile is missing the [{e.args[0]}] table") from None
    risk = d.get("risk", {})
    caps = d.get("caps", {})
    binaries = d.get("binaries", {})
    rules = d.get("rules", {})
    packages = {}
    for p in d.get("package", []):
        pkg = Package(
            name=p["name"],
            archive=p["archive"],
            ppa=p.get("ppa", ""),
            large=p.get("large", False),
            in_archive=p.get("in_archive", True),
        )
        packages[pkg.name] = pkg
    return Profile(
        tag=proj["tag"],
        series=list(proj["series"]),
        pockets=list(proj.get("pockets", ["Release", "Security", "Updates"])),
        notify_pockets=list(proj.get("notify_pockets", ["Proposed"])),
        repo_url=repo["url"],
        upstream_url=repo["upstream_url"],
        upstream_remote=repo.get("upstream_remote", "pkg"),
        mirror_branch=br["mirror"],
        downstream_branch=br["downstream"],
        staging_branch=br["staging"],
        source_rename=d.get("source", {}).get("rename", True),
        binaries_mode=binaries.get("mode", "rename"),
        rename_suffix=binaries.get("rename_suffix", "-{tag}"),
        relations=list(binaries.get("relations", ["Provides", "Conflicts", "Replaces"])),
        risk=Risk(
            rules_markers=list(risk.get("rules_markers", [])),
            control_fields=list(risk.get("control_fields", [])),
            files=list(risk.get("files", [])),
        ),
        caps=Caps(
            low=tuple(caps.get("low", Caps().low)),
            high=tuple(caps.get("high", Caps().high)),
        ),
        rules_enabled=list(rules.get("enable", [])),
        identity_allowed=list(rules.get("changelog_identity_allowed", [])),
        packages=packages,
    )


def load(path: str | Path) -> Profile:
    p = Path(path)
    if not p.is_file():
        raise ProfileError(f"profile file not found: {p}")
    with p.open("rb") as f:
        return from_dict(tomllib.load(f))
