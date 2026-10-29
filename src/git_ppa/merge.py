# SPDX-License-Identifier: GPL-3.0-or-later
"""merge start: merge an archive import tag into the downstream branch, then report tier and risk."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import gitutil, versions, workspace
from .profile import Caps, Package, Profile, Risk


class Refused(Exception):
    """A safety check stopped the command. Exit code 4."""


class UpToDate(Exception):
    pass


@dataclass
class Hunk:
    removed: list[tuple[int, str]] = field(default_factory=list)
    added: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class Hit:
    kind: str
    path: str
    line: int
    text: str


@dataclass
class Conflict:
    path: str
    regions: list[tuple[int, int]]


@dataclass
class Report:
    package: str
    series: str
    target: str
    base: str | None
    old_tip: str
    staging: str
    merged: bool
    merge_commit: str | None
    conflicts: list[Conflict]
    changed: list[tuple[str, str]]
    series_changes: list[str]
    hits: list[Hit]
    risk: str
    tier: str
    warnings: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


HDR = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
IF_RE = re.compile(r"\s*(ifeq|ifneq|ifdef|ifndef)\b")
ENDIF_RE = re.compile(r"\s*endif\b")


def parse_hunks(diff: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    cur: Hunk | None = None
    o = n = 0
    for line in diff.splitlines():
        m = HDR.match(line)
        if m:
            cur = Hunk()
            hunks.append(cur)
            o, n = int(m[1]), int(m[3])
        elif cur is not None and line.startswith("-"):
            cur.removed.append((o, line[1:]))
            o += 1
        elif cur is not None and line.startswith("+"):
            cur.added.append((n, line[1:]))
            n += 1
    return hunks


def parse_conflicts(text: str) -> list[tuple[int, int]]:
    regions: list[tuple[int, int]] = []
    state = None
    a = b = 0
    for line in text.splitlines():
        if line.startswith("<<<<<<<"):
            state, a, b = "ours", 0, 0
        elif state == "ours" and line.rstrip() == "=======":
            state = "theirs"
        elif state == "theirs" and line.startswith(">>>>>>>"):
            regions.append((a, b))
            state = None
        elif state == "ours":
            a += 1
        elif state == "theirs":
            b += 1
    return regions


def conflict_lines(text: str) -> list[tuple[int, str]]:
    """Every line inside a conflict region, both sides, with its line number."""
    res: list[tuple[int, str]] = []
    inside = False
    for i, line in enumerate(text.splitlines(), 1):
        if line.startswith("<<<<<<<"):
            inside = True
        elif inside and line.startswith(">>>>>>>"):
            inside = False
        elif inside and line.rstrip() != "=======":
            res.append((i, line))
    return res


def rules_spans(text: str, markers: list[re.Pattern[str]]) -> list[tuple[int, int]]:
    """ifeq/ifdef ... endif blocks of debian/rules that contain a risk marker."""
    stack: list[list] = []
    spans: list[tuple[int, int]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if IF_RE.match(line):
            stack.append([i, any(p.search(line) for p in markers)])
        elif ENDIF_RE.match(line) and stack:
            start, hit = stack.pop()
            if hit:
                spans.append((start, i))
        elif stack and any(p.search(line) for p in markers):
            stack[-1][1] = True
    return spans


def control_field(lines: list[str], lineno: int) -> str | None:
    i = lineno - 1
    while 0 <= i < len(lines) and lines[i][:1] in (" ", "\t"):
        i -= 1
    if not 0 <= i < len(lines):
        return None
    m = re.match(r"([A-Za-z0-9-]+):", lines[i])
    return m.group(1) if m else None


def risk_hits(path: str, old_text: str, new_text: str, removed: list[tuple[int, str]], added: list[tuple[int, str]], risk: Risk) -> list[Hit]:
    hits: list[Hit] = []
    if path == "debian/rules" and risk.rules_markers:
        pats = [re.compile(m) for m in risk.rules_markers]
        old_spans = rules_spans(old_text, pats)
        new_spans = rules_spans(new_text, pats)

        def inside(no: int, spans: list[tuple[int, int]]) -> bool:
            return any(a <= no <= b for a, b in spans)

        for no, txt in removed:
            if inside(no, old_spans) or any(p.search(txt) for p in pats):
                hits.append(Hit("rules-toolchain", path, no, txt.strip()))
        for no, txt in added:
            if inside(no, new_spans) or any(p.search(txt) for p in pats):
                hits.append(Hit("rules-toolchain", path, no, txt.strip()))
    elif path == "debian/control" and risk.control_fields:
        wanted = {f.lower() for f in risk.control_fields}
        old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
        for no, txt in removed:
            f = control_field(old_lines, no)
            if f and f.lower() in wanted:
                hits.append(Hit(f"control-{f}", path, no, txt.strip()))
        for no, txt in added:
            f = control_field(new_lines, no)
            if f and f.lower() in wanted:
                hits.append(Hit(f"control-{f}", path, no, txt.strip()))
    return hits


def classify(conflicts: list[Conflict], risk_level: str, caps: Caps) -> str:
    if not conflicts:
        return "mechanical"
    f, r, l = caps.high if risk_level == "high" else caps.low
    regions = [reg for c in conflicts for reg in c.regions]
    within = len(conflicts) <= f and len(regions) <= r and all(max(a, b) <= l for a, b in regions)
    return "ai-assisted" if within else "re-port"


def _top_version(text: str) -> str | None:
    m = re.match(r"\S+ \(([^)]+)\)", text)
    return m.group(1) if m else None


def merge_start(profile: Profile, pkg: Package, series: str, repo: Path, target: str, force: bool = False) -> Report:
    names = profile.names(pkg, series)
    tag = workspace.import_tag(profile, target)
    warnings: list[str] = []
    gitutil.require("git", "dpkg-mergechangelogs")
    if not gitutil.is_clean(repo):
        git_dir = Path(gitutil.out("rev-parse", "--absolute-git-dir", cwd=repo))
        if (git_dir / "MERGE_HEAD").exists():
            raise Refused("a merge is in progress, resolve it, or run `git merge --abort` to discard it")
        raise Refused("working tree is not clean")
    target_sha = gitutil.rev(tag, repo)
    if not target_sha:
        raise Refused(f"no import tag {tag}, fetch the {profile.upstream_remote} remote")
    mirror_sha = gitutil.rev(f"refs/heads/{names['mirror']}", repo)
    if mirror_sha != target_sha:
        raise Refused(f"{names['mirror']} is not at {target}, run `git ppa sync` first")
    local = f"refs/heads/{names['downstream']}"
    start = local if gitutil.rev(local, repo) else f"refs/remotes/origin/{names['downstream']}"
    if not gitutil.rev(start, repo):
        raise Refused(f"no downstream branch {names['downstream']}")
    if gitutil.ok("merge-base", "--is-ancestor", target_sha, start, cwd=repo):
        raise UpToDate(f"{target} is already in {names['downstream']}")
    old_top = _top_version(gitutil.show(start, "debian/changelog", repo))
    base = versions.strip_suffix(old_top, profile.tag) if old_top else None
    base_tag = workspace.import_tag(profile, base) if base else None
    base_sha = gitutil.rev(base_tag, repo) if base_tag else None
    if not base_sha:
        warnings.append(f"base version {base} has no import tag, old/ubuntu not set")

    staging = names["staging"]
    if gitutil.rev(f"refs/heads/{staging}", repo):
        if not force:
            raise Refused(f"branch {staging} exists, use --force to recreate it")
        if gitutil.current_branch(repo) == staging:
            gitutil.run("checkout", "-q", "--detach", start, cwd=repo)
        gitutil.run("branch", "-q", "-D", staging, cwd=repo)
    gitutil.run("checkout", "-q", "-b", staging, start, cwd=repo)
    old_tip = gitutil.rev("HEAD", repo) or ""
    gitutil.run("tag", "-f", f"old/{profile.tag}", old_tip, cwd=repo)
    if base_sha:
        gitutil.run("tag", "-f", "old/ubuntu", base_sha, cwd=repo)
    gitutil.run("tag", "-f", "new/ubuntu", target_sha, cwd=repo)
    gitutil.register_changelog_driver(repo)

    p = gitutil.run("merge", "--no-edit", tag, cwd=repo, check=False)
    merged = p.returncode == 0
    old_ref = f"old/{profile.tag}"

    conflicts: list[Conflict] = []
    conflict_text: dict[str, str] = {}
    for path in gitutil.out("diff", "--name-only", "--diff-filter=U", cwd=repo).splitlines():
        text = (repo / path).read_text(errors="replace") if (repo / path).exists() else ""
        conflict_text[path] = text
        conflicts.append(Conflict(path, parse_conflicts(text)))

    changed: list[tuple[str, str]] = []
    for line in gitutil.out("diff", "--name-status", old_ref, cwd=repo).splitlines():
        status, _, path = line.partition("\t")
        changed.append((status, path.split("\t")[-1]))

    hits: list[Hit] = []
    for status, path in changed:
        if path in ("debian/rules", "debian/control"):
            new_path = repo / path
            new_text = new_path.read_text(errors="replace") if new_path.exists() else ""
            hunks = parse_hunks(gitutil.run("diff", "-U0", old_ref, "--", path, cwd=repo).stdout)
            removed = [x for h in hunks for x in h.removed]
            added = [x for h in hunks for x in h.added]
            if path in conflict_text:
                added += conflict_lines(conflict_text[path])
            hits += risk_hits(path, gitutil.show(old_ref, path, repo), new_text, removed, added, profile.risk)
        if any(fnmatch.fnmatch(Path(path).name, g) for g in profile.risk.files):
            hits.append(Hit("build-system", path, 0, status))

    hits = list(dict.fromkeys((h.kind, h.path, h.line, h.text) for h in hits))
    hits = [Hit(*h) for h in hits]
    series_changes = [
        l for l in gitutil.out("diff", old_ref, "--", "debian/patches/series", cwd=repo).splitlines()
        if l[:1] in "+-" and not l.startswith(("+++", "---"))
    ]
    risk = "high" if hits else "low"
    return Report(
        package=pkg.name, series=series, target=target, base=base, old_tip=old_tip[:12], staging=staging,
        merged=merged, merge_commit=(gitutil.rev("HEAD", repo) or "")[:12] if merged else None,
        conflicts=conflicts, changed=changed, series_changes=series_changes, hits=hits, risk=risk,
        tier=classify(conflicts, risk, profile.caps), warnings=warnings,
    )


def abort(profile: Profile, pkg: Package, series: str, repo: Path) -> list[str]:
    names = profile.names(pkg, series)
    done: list[str] = []
    git_dir = Path(gitutil.out("rev-parse", "--absolute-git-dir", cwd=repo))
    if (git_dir / "MERGE_HEAD").exists():
        gitutil.run("merge", "--abort", cwd=repo)
        done.append("merge aborted")
    if gitutil.current_branch(repo) == names["staging"]:
        local = f"refs/heads/{names['downstream']}"
        gitutil.run("checkout", "-q", names["downstream"] if gitutil.rev(local, repo) else f"origin/{names['downstream']}", cwd=repo)
        done.append(f"checked out {names['downstream']}")
    if gitutil.rev(f"refs/heads/{names['staging']}", repo):
        gitutil.run("branch", "-q", "-D", names["staging"], cwd=repo)
        done.append(f"deleted {names['staging']}")
    for t in (f"old/{profile.tag}", "old/ubuntu", "new/ubuntu"):
        if gitutil.run("tag", "-d", t, cwd=repo, check=False).returncode == 0:
            done.append(f"deleted tag {t}")
    return done


def render(r: Report, limit: int = 8) -> str:
    lines = [
        f"package   {r.package} ({r.series})",
        f"target    {r.target}   base {r.base or '?'}",
        f"staging   {r.staging}   previous tip {r.old_tip}",
        f"result    {'merged cleanly' if r.merged else 'conflicts to resolve'}"
        + (f", merge commit {r.merge_commit}" if r.merge_commit else ""),
        f"tier      {r.tier}",
        f"risk      {r.risk}",
    ]
    for c in r.conflicts:
        lines.append(f"conflict  {c.path}: {len(c.regions)} region(s), lines per side {c.regions}")
    patches = [x for x in r.changed if x[1].startswith("debian/patches/")]
    other = [f"{s} {p}" for s, p in r.changed if not p.startswith("debian/patches/")]
    more = f", and {len(other) - limit} more" if len(other) > limit else ""
    lines.append(f"changed   {len(r.changed)} file(s): {len(patches)} under debian/patches/, {len(other)} other" + (": " + ", ".join(other[:limit]) + more if other else ""))
    if r.series_changes:
        lines.append(f"series    {len(r.series_changes)} line(s): " + "; ".join(r.series_changes[:limit]) + (" ..." if len(r.series_changes) > limit else ""))
    if r.hits:
        kinds: dict[str, int] = {}
        for h in r.hits:
            kinds[h.kind] = kinds.get(h.kind, 0) + 1
        lines.append("risk hits " + ", ".join(f"{k} x{n}" for k, n in sorted(kinds.items())))
        ranked = sorted(r.hits, key=lambda h: h.kind == "build-system")
        for h in ranked[:limit]:
            lines.append(f"          [{h.kind}] {h.path}:{h.line} {h.text[:80]}".rstrip())
        if len(r.hits) > limit:
            lines.append(f"          ... {len(r.hits) - limit} more, use --json for all")
    for w in r.warnings:
        lines.append(f"warning   {w}")
    return "\n".join(lines)
