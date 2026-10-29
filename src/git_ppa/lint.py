# SPDX-License-Identifier: GPL-3.0-or-later
"""lint: packaging rules for a renamed downstream source, each one comes from a real packaging failure."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from debian import deb822
from debian.changelog import Changelog

from . import gitutil, versions, workspace
from .profile import Package, Profile

DH_EXT = (
    "install", "dirs", "docs", "links", "manpages", "examples", "conffiles", "logrotate", "init",
    "default", "service", "triggers", "postinst", "postrm", "preinst", "prerm", "lintian-overrides",
    "bug-control", "bug-presubj", "NEWS", "changelog", "cron.d", "cron.daily", "tmpfiles", "maintscript",
)
STALE_CMD = re.compile(r"dpkg-divert|dpkg-query|dpkg\s+-[LSsl]\b|--package\b")
SCRIPTS = ("preinst", "postinst", "prerm", "postrm", "config")


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _relation_names(value: str) -> set[str]:
    names = set()
    for part in value.replace("\n", " ").split(","):
        part = part.strip()
        if part:
            names.add(re.split(r"[\s(\[<]", part, maxsplit=1)[0])
    return names


def _binaries(repo: Path) -> list[deb822.Deb822]:
    with (repo / "debian/control").open(encoding="utf-8") as f:
        return [p for p in deb822.Deb822.iter_paragraphs(f) if "Package" in p]


def binary_rename(repo: Path, profile: Profile, pkg: Package) -> list[Finding]:
    if profile.binaries_mode != "rename":
        return []
    suffix = profile.binary_suffix()
    res = []
    for para in _binaries(repo):
        name = para["Package"]
        if not name.endswith(suffix):
            res.append(Finding("binary-rename", "error", f"binary {name} is not renamed to *{suffix}", "debian/control"))
            continue
        archive_name = name[: -len(suffix)]
        for rel in profile.relations:
            if archive_name not in _relation_names(para.get(rel, "")):
                res.append(Finding("binary-rename", "error", f"{name} has no {rel} on {archive_name}", "debian/control"))
    return res


def no_empty_packages(repo: Path, profile: Profile, pkg: Package) -> list[Finding]:
    """Static part only: debhelper per-package files whose stem is a pre-rename package name."""
    if profile.binaries_mode != "rename":
        return []
    suffix = profile.binary_suffix()
    declared = {p["Package"] for p in _binaries(repo)}
    res = []
    for f in sorted((repo / "debian").iterdir()):
        if not f.is_file():
            continue
        stem = None
        for ext in DH_EXT:
            if f.name.endswith("." + ext):
                stem = f.name[: -len(ext) - 1]
                break
        if stem and stem not in declared and f"{stem}{suffix}" in declared:
            res.append(Finding("no-empty-packages", "error", f"{f.name} belongs to {stem}, which is renamed to {stem}{suffix}, debhelper will silently ignore it", f"debian/{f.name}"))
    return res


def no_stale_names(repo: Path, profile: Profile, pkg: Package) -> list[Finding]:
    if profile.binaries_mode != "rename":
        return []
    suffix = profile.binary_suffix()
    old_names = {p["Package"][: -len(suffix)] for p in _binaries(repo) if p["Package"].endswith(suffix)}
    if not old_names:
        return []
    files = [repo / "debian/rules"] + [f for f in (repo / "debian").iterdir() if f.name.split(".")[-1] in SCRIPTS]
    res = []
    for f in files:
        if not f.is_file():
            continue
        for no, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith("#") or not STALE_CMD.search(line):
                continue
            for tok in line.split():
                tok = tok.strip("'\"`;()")
                if tok.startswith("--package="):
                    tok = tok[len("--package="):]
                elif tok.startswith("-") or any(c in tok for c in "=/$"):
                    continue
                if tok in old_names:
                    res.append(Finding("no-stale-names", "error", f"{f.name}:{no} refers to {tok}, now {tok}{suffix}: {line.strip()[:80]}", f"debian/{f.name}"))
                    break
    return res


def version_order(repo: Path, profile: Profile, pkg: Package) -> list[Finding]:
    res = []
    with (repo / "debian/changelog").open(encoding="utf-8") as f:
        entries = list(Changelog(f))
    top = entries[0]
    v = str(top.version)
    if profile.source_rename and top.package != pkg.name:
        res.append(Finding("version-order", "error", f"top changelog entry is for {top.package}, expected {pkg.name}", "debian/changelog"))
    if not versions.has_suffix(v, profile.tag):
        res.append(Finding("version-order", "error", f"{v} has no +{profile.tag}<N> or ~{profile.tag}<N> suffix", "debian/changelog"))
        return res
    base = versions.strip_suffix(v, profile.tag)
    plus = "+" in v[len(base):len(base) + 1]
    if pkg.in_archive and not plus:
        res.append(Finding("version-order", "warn", f"{v} uses ~ but {pkg.name} is in the archive, convention is +{profile.tag}<N>", "debian/changelog"))
    if plus and not versions.gt(v, base):
        res.append(Finding("version-order", "error", f"{v} does not sort above its base {base}", "debian/changelog"))
    for older in entries[1:]:
        if older.package == top.package:
            if not versions.gt(v, str(older.version)):
                res.append(Finding("version-order", "error", f"{v} does not sort above the previous downstream version {older.version}", "debian/changelog"))
            break
    if gitutil.rev(workspace.import_tag(profile, base), repo) is None:
        res.append(Finding("version-order", "warn", f"no import tag for the base {base}, cannot confirm it is an archive version (is the {profile.upstream_remote} remote fetched?)", "debian/changelog"))
    return res


def changelog_identity(repo: Path, profile: Profile, pkg: Package) -> list[Finding]:
    if not profile.identity_allowed:
        return []
    with (repo / "debian/changelog").open(encoding="utf-8") as f:
        top = next(iter(Changelog(f)))
    m = re.search(r"<([^>]+)>", top.author or "")
    email = m.group(1) if m else ""
    if email not in profile.identity_allowed:
        return [Finding("changelog-identity", "warn", f"top entry is signed by {top.author}, allowed: {', '.join(profile.identity_allowed)}", "debian/changelog")]
    return []


RULES = {
    "binary-rename": binary_rename,
    "no-empty-packages": no_empty_packages,
    "no-stale-names": no_stale_names,
    "version-order": version_order,
    "changelog-identity": changelog_identity,
}


def run(repo: Path, profile: Profile, pkg: Package, only: list[str] | None = None) -> list[Finding]:
    enabled = only or profile.rules_enabled or list(RULES)
    findings: list[Finding] = []
    for rule in enabled:
        if rule not in RULES:
            findings.append(Finding(rule, "warn", "rule is not implemented in this prototype"))
            continue
        findings += RULES[rule](repo, profile, pkg)
    return findings


def render(findings: list[Finding]) -> str:
    if not findings:
        return "lint: no findings"
    return "\n".join(f"{f.severity:5} [{f.rule}] {f.message}" for f in findings)
