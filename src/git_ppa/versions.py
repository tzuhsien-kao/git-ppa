# SPDX-License-Identifier: GPL-3.0-or-later
"""Version helpers. Tag names, suffix handling and comparison."""

from __future__ import annotations

import itertools
import re

from debian.changelog import Changelog
from debian.debian_support import Version


def enc_tag(version: str) -> str:
    # the git-ubuntu importer encodes ':' as '%' and '~' as '_' in tag names
    return version.replace(":", "%").replace("~", "_")


def suffix_re(tag: str) -> re.Pattern[str]:
    return re.compile(rf"[+~]{re.escape(tag)}\d+$")


def has_suffix(version: str, tag: str) -> bool:
    return bool(suffix_re(tag).search(version))


def strip_suffix(version: str, tag: str) -> str:
    return suffix_re(tag).sub("", version)


def next_version(base: str, tag: str, in_archive: bool = True, n: int = 1) -> str:
    return f"{base}{'+' if in_archive else '~'}{tag}{n}"


def gt(a: str, b: str) -> bool:
    return Version(a) > Version(b)


def newest(versions: list[str]) -> str | None:
    return max(versions, key=Version) if versions else None


def top_version(changelog_text: str) -> str | None:
    for block in Changelog(changelog_text, max_blocks=1):
        return str(block.version)
    return None


def newest_of(changelog_text: str, package: str) -> str | None:
    """Newest version of the entries for one source name. After a merge the top entry can be the
    archive's (dpkg-mergechangelogs sorts by version), so the top says nothing about whose it is."""
    return newest([str(b.version) for b in Changelog(changelog_text) if b.package == package])


def archive_base(changelog_text: str, archive_name: str, tag: str) -> str | None:
    """The newest archive version merged into a downstream branch, read from its debian/changelog.

    The version of the top entry cannot tell, it may keep an older base (see choose_version). The
    archive entries that a merge brings in are in the changelog, under the archive source name.
    """
    found = [
        str(b.version) for b in Changelog(changelog_text)
        if b.package == archive_name and not has_suffix(str(b.version), tag)
    ]
    return newest(found)


def choose_version(target: str, previous: str, tag: str, in_archive: bool = True,
                   used: frozenset[str] | set[str] = frozenset()) -> tuple[str, bool]:
    """Version of the downstream entry after merging the archive version target.

    It must sort above the previous downstream version and must never have been used in the target PPA.
    The rule is <target>+<tag>1 (or ~ for a package not in the archive). That does not sort above the
    previous version when the archive appended letters to the old version (5:8.0.5-1 to
    5:8.0.5-1ubuntu0.1, 2.10-5 to 2.10-5build1), dpkg sorts letters before '+'. Then the previous base
    is kept and only its counter goes up. Returns the version and whether an older base was kept.
    """
    sep = "+" if in_archive else "~"
    for n in itertools.count(1):
        v = f"{target}{sep}{tag}{n}"
        if not gt(v, previous):
            break
        if v not in used:
            return v, False
    m = suffix_re(tag).search(previous)
    if not m:
        raise ValueError(f"the previous version {previous} has no {tag} suffix, cannot continue its counter")
    base, mark = previous[:m.start()], m.group(0)[0]
    for n in itertools.count(int(re.search(r"\d+$", previous).group(0)) + 1):
        v = f"{base}{mark}{tag}{n}"
        if v not in used:
            return v, base != target
