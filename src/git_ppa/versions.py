# SPDX-License-Identifier: GPL-3.0-or-later
"""Version helpers. Tag names, suffix handling and comparison."""

from __future__ import annotations

import re

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
