# SPDX-License-Identifier: GPL-3.0-or-later
"""merge start: merge an archive import tag into the downstream branch, then report tier and risk."""

from __future__ import annotations

import difflib
import email.utils
import fnmatch
import io
import os
import re
import tarfile
import tempfile
import textwrap
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import gitutil, lint, lp, versions, workspace
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
    # conflicts the tool resolved itself, so a reader can see what was not decided by git
    resolved: list[str] = field(default_factory=list)
    # build system files the merge changed that our delta does not touch, listed but not counted as risk
    build_system_other: list[str] = field(default_factory=list)

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


SERIES = "debian/patches/series"


def _edits(base: list[str], side: list[str]) -> tuple[set[int], dict[int, list[str]]]:
    """What one side did to the base list: the base lines it dropped, and the lines it put before base line N.

    Lines are aligned as a sequence, so repeated lines such as the blank separators of a series file are
    fine, and a line that was moved shows up as a drop plus an addition.
    """
    dropped: set[int] = set()
    added: dict[int, list[str]] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, side, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        dropped.update(range(i1, i2))
        if j2 > j1:
            added.setdefault(i1, []).extend(side[j1:j2])
    return dropped, added


def merge_lists(base: list[str], ours: list[str], theirs: list[str]) -> tuple[list[str], dict[str, int]] | None:
    """Three way merge of an ordered list of lines such as debian/patches/series.

    Git calls it a conflict when one side adds lines next to a line the other side drops, although both
    changes are plain. Here each side may add and drop lines, additions at the same place keep ours first.
    Refused (None) when both sides drop the same line (a replacement on both sides cannot be told from two
    different ones, blank lines excepted), when both sides add the same line in different places, or when
    the result would name a line twice that the base named once.
    """
    drop_o, add_o = _edits(base, ours)
    drop_t, add_t = _edits(base, theirs)
    if any(base[i].strip() for i in drop_o & drop_t):
        return None
    where = {line: i for i, lines in add_o.items() for line in lines if line.strip()}
    if any(line in where and where[line] != i for i, lines in add_t.items() for line in lines if line.strip()):
        return None
    out: list[str] = []
    for i in range(len(base) + 1):
        mine = add_o.get(i, [])
        out += mine + [x for x in add_t.get(i, []) if not (x.strip() and x in mine)]
        if i < len(base) and i not in drop_o | drop_t:
            out.append(base[i])
    twice = {x for x in out if x.strip() and out.count(x) > 1}
    if twice - {x for x in base if x.strip() and base.count(x) > 1}:
        return None
    stats = {
        "ours_added": sum(len(v) for v in add_o.values()), "ours_dropped": len(drop_o),
        "theirs_added": sum(len(v) for v in add_t.values()), "theirs_dropped": len(drop_t),
    }
    return out, stats


def _resolve_series(repo: Path) -> str | None:
    """Resolve a conflict in debian/patches/series by a list merge, when it is safe. Returns what was done."""
    stages = []
    for n in (1, 2, 3):
        p = gitutil.run("show", f":{n}:{SERIES}", cwd=repo, check=False)
        if p.returncode != 0:
            return None
        stages.append(p.stdout.splitlines())
    merged = merge_lists(*stages)
    if merged is None:
        return None
    lines, s = merged
    (repo / SERIES).write_text("\n".join(lines) + ("\n" if lines else ""))
    gitutil.run("add", SERIES, cwd=repo)
    return (f"{SERIES}: merged as a list, ours +{s['ours_added']} -{s['ours_dropped']}, "
            f"theirs +{s['theirs_added']} -{s['theirs_dropped']}, the patches still have to apply")


PATCH_TARGET = re.compile(r"^\+\+\+ (\S+)", re.M)


def patch_targets(text: str) -> set[str]:
    """Files a patch modifies. The leading directory is dropped as well, patches use -p0 or -p1."""
    out: set[str] = set()
    for m in PATCH_TARGET.finditer(text):
        path = m[1]
        if path == "/dev/null":
            continue
        out.add(path)
        if "/" in path:
            out.add(path.split("/", 1)[1])
    return out


def delta_files(repo: Path, old_ref: str) -> set[str] | None:
    """Files our delta touches: the ones it changes in the tree, and the ones its patches modify.

    None when the previous archive version is not known, nothing can be said about the delta then.
    """
    if not gitutil.rev("old/ubuntu", repo):
        return None
    names = gitutil.out("diff", "--name-only", "old/ubuntu", old_ref, cwd=repo).splitlines()
    files = set(names)
    for n in names:
        if n.startswith("debian/patches/") and n != SERIES:
            files |= patch_targets(gitutil.show(old_ref, n, repo))
    return files


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
    old_text = gitutil.show(start, "debian/changelog", repo)
    old_top = _top_version(old_text)
    base = versions.archive_base(old_text, pkg.archive, profile.tag) if old_text else None
    if not base and old_top:
        base = versions.strip_suffix(old_top, profile.tag)
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

    resolved: list[str] = []
    unmerged = gitutil.out("diff", "--name-only", "--diff-filter=U", cwd=repo).splitlines()
    if SERIES in unmerged:
        done = _resolve_series(repo)
        if done:
            resolved.append(done)
    conflicts: list[Conflict] = []
    conflict_text: dict[str, str] = {}
    left = gitutil.out("diff", "--name-only", "--diff-filter=U", cwd=repo).splitlines()
    if resolved and not left and not merged:
        gitutil.run("commit", "--no-edit", "-q", cwd=repo)
        merged = True
    for path in left:
        text = (repo / path).read_text(errors="replace") if (repo / path).exists() else ""
        conflict_text[path] = text
        conflicts.append(Conflict(path, parse_conflicts(text)))

    changed: list[tuple[str, str]] = []
    for line in gitutil.out("diff", "--name-status", old_ref, cwd=repo).splitlines():
        status, _, path = line.partition("\t")
        changed.append((status, path.split("\t")[-1]))

    delta = delta_files(repo, old_ref)
    if delta is None:
        warnings.append("old/ubuntu is not set, every build system file the merge changed counts as risk")
    build_other: list[str] = []
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
            if delta is None or path in delta:
                hits.append(Hit("build-system", path, 0, status))
            else:
                build_other.append(path)

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
        resolved=resolved, build_system_other=build_other,
    )


class LintFailed(Exception):
    """merge finish found lint errors the downstream did not have before the merge. Exit code 1."""

    def __init__(self, findings: list[lint.Finding]):
        self.findings = findings
        super().__init__(f"{len(findings)} new lint error(s), the changelog entry was not committed")


@dataclass
class Finish:
    package: str
    target: str
    previous: str
    version: str
    kept_base: bool
    entry: str
    ppa: str | None
    ppa_checked: bool
    carried: list[str]
    commit: str | None
    warnings: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


CVE = re.compile(r"CVE-\d{4}-\d{4,}")


def _identity(profile: Profile, me: bool, repo: Path) -> tuple[str, str]:
    if me:
        name = os.environ.get("DEBFULLNAME") or gitutil.out("config", "user.name", cwd=repo, check=False)
        email = os.environ.get("DEBEMAIL") or gitutil.out("config", "user.email", cwd=repo, check=False)
        if not (name and email) or email.endswith("@localhost"):
            raise Refused("--me needs DEBFULLNAME and DEBEMAIL, or a git user.name and user.email")
        return name, email
    if not (profile.changelog_name and profile.changelog_email):
        raise Refused("the profile has no [changelog] name and email (the bot identity), use --me to sign as yourself")
    return profile.changelog_name, profile.changelog_email


def changelog_entry(source: str, version: str, series: str, bullets: list[str], name: str, email: str,
                    when: datetime | None = None) -> str:
    when = when or datetime.now().astimezone()
    body = "".join(
        textwrap.fill(b, width=79, initial_indent="  * ", subsequent_indent="    ") + "\n" for b in bullets
    )
    return f"{source} ({version}) {series}; urgency=medium\n\n{body}\n -- {name} <{email}>  {email_date(when)}\n\n"


def email_date(when: datetime) -> str:
    return email.utils.format_datetime(when)


def _cves_between(changelog_text: str, archive: str, tag: str, old_base: str | None, target: str) -> list[str]:
    from debian.changelog import Changelog
    ids: set[str] = set()
    for b in Changelog(changelog_text):
        v = str(b.version)
        if b.package != archive or versions.has_suffix(v, tag):
            continue
        if versions.gt(v, target) or (old_base and not versions.gt(v, old_base)):
            continue
        ids |= set(CVE.findall("\n".join(b.changes())))
    return sorted(ids)


def _lint_errors_at(ref: str, profile: Profile, pkg: Package, repo: Path) -> set[tuple[str, str]]:
    """Lint errors of the debian/ directory of a commit. Python's tarfile, the snap's tar cannot extract."""
    data = gitutil.run_bytes("archive", "--format=tar", ref, "debian", cwd=repo)
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(tmp, filter="data")
        return {(f.rule, f.message) for f in lint.run(Path(tmp), profile, pkg) if f.severity == "error"}


def merge_finish(profile: Profile, pkg: Package, series: str, repo: Path, me: bool = False,
                 dry_run: bool = False, skip_ppa_check: bool = False, when: datetime | None = None) -> Finish:
    names = profile.names(pkg, series)
    old_ref = f"old/{profile.tag}"
    warnings: list[str] = []
    if gitutil.current_branch(repo) != names["staging"]:
        raise Refused(f"run merge finish on {names['staging']}, after merge start")
    git_dir = Path(gitutil.out("rev-parse", "--absolute-git-dir", cwd=repo))
    if (git_dir / "MERGE_HEAD").exists():
        raise Refused("the merge is not committed, resolve the conflicts, commit, then run merge finish")
    if not gitutil.is_clean(repo):
        raise Refused("working tree is not clean")
    for t in (old_ref, "new/ubuntu"):
        if not gitutil.rev(t, repo):
            raise Refused(f"no tag {t}, run merge start first")
    if not gitutil.ok("merge-base", "--is-ancestor", "new/ubuntu", "HEAD", cwd=repo):
        raise Refused("new/ubuntu is not merged into this branch")
    old_text = gitutil.show(old_ref, "debian/changelog", repo)
    previous = versions.newest_of(old_text, pkg.name) or versions.top_version(old_text) or ""
    current = (repo / "debian/changelog").read_text(encoding="utf-8")
    ours = versions.newest_of(current, pkg.name)
    if ours and ours != previous:
        raise Refused(f"there is already an entry {ours} after {previous}, merge finish was run")
    target = versions.top_version(gitutil.show("new/ubuntu", "debian/changelog", repo)) or ""
    old_base = versions.archive_base(old_text, pkg.archive, profile.tag)

    ref = profile.ppa_reference(pkg)
    used: set[str] = set()
    if ref and not skip_ppa_check:
        used = lp.ppa_versions(ref, pkg.name)
    elif ref:
        warnings.append(f"versions already used in {ref} were not checked (--skip-ppa-check)")
    else:
        warnings.append("the profile names no target PPA for this package, versions used there were not checked")
    version, kept = versions.choose_version(target, previous, profile.tag, pkg.in_archive, used)

    sep = "+" if pkg.in_archive else "~"
    bullets = [f"Merge {target} from the Ubuntu archive."]
    cves = _cves_between(current, pkg.archive, profile.tag, old_base, target)
    if cves:
        bullets.append("It includes the fixes for " + ", ".join(cves) + ".")
    if kept:
        bullets.append(f"The version keeps the base {versions.strip_suffix(version, profile.tag)}, "
                       f"{target}{sep}{profile.tag}1 would sort below the previous version {previous}.")
    name, mail = _identity(profile, me, repo)
    entry = changelog_entry(pkg.name, version, series, bullets, name, mail, when)
    result = Finish(pkg.name, target, previous, version, kept, entry, ref, bool(ref and not skip_ppa_check),
                    [], None, warnings)
    if dry_run:
        return result

    before = _lint_errors_at(old_ref, profile, pkg, repo)
    (repo / "debian/changelog").write_text(entry + current, encoding="utf-8")
    after = {(f.rule, f.message) for f in lint.run(repo, profile, pkg) if f.severity == "error"}
    new = sorted(after - before)
    if new:
        gitutil.run("checkout", "--", "debian/changelog", cwd=repo)
        raise LintFailed([lint.Finding(r, "error", m, "") for r, m in new])
    result.carried = [f"[{r}] {m}" for r, m in sorted(after & before)]
    gitutil.run("add", "debian/changelog", cwd=repo)
    # the author is whoever signs the entry, --author wins over GIT_AUTHOR_* in the environment
    gitutil.run("commit", "-q", f"--author={name} <{mail}>", "-m", f"{pkg.name} {version}", cwd=repo)
    result.commit = (gitutil.rev("HEAD", repo) or "")[:12]
    return result


def render_finish(r: Finish) -> str:
    lines = [
        f"package   {r.package}",
        f"merged    {r.target}",
        f"previous  {r.previous}",
        f"version   {r.version}" + ("   (keeps an older base, see the entry)" if r.kept_base else ""),
        f"ppa       {r.ppa or '-'}" + ("" if r.ppa_checked or not r.ppa else "   (not checked)"),
    ]
    if r.commit:
        lines.append(f"commit    {r.commit}")
    else:
        lines.append("dry run   nothing written")
    for c in r.carried:
        lines.append(f"carried   {c}   (already there before the merge)")
    for w in r.warnings:
        lines.append(f"warning   {w}")
    lines.append("entry")
    lines += ["  " + l if l else "" for l in r.entry.rstrip("\n").splitlines()]
    return "\n".join(lines)


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
    for x in r.resolved:
        lines.append(f"resolved  {x}")
    if r.build_system_other:
        lines.append(
            f"note      {len(r.build_system_other)} build system file(s) changed that our delta does not touch, "
            "not counted as risk: " + ", ".join(r.build_system_other[:limit]) + (" ..." if len(r.build_system_other) > limit else "")
        )
    for w in r.warnings:
        lines.append(f"warning   {w}")
    return "\n".join(lines)
