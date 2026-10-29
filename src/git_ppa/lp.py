# SPDX-License-Identifier: GPL-3.0-or-later
"""Launchpad API, anonymous and read only."""

from __future__ import annotations

import json
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from debian.changelog import Changelog

from . import versions

API = "https://api.launchpad.net/devel"
CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}")


class LaunchpadError(Exception):
    pass


@dataclass(frozen=True)
class Pub:
    version: str
    pocket: str
    date: str
    self_link: str


def _get(url: str, retries: int = 3) -> bytes:
    last: Exception | None = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(1 + i)
    raise LaunchpadError(f"GET {url} failed: {last}")


def published(archive_pkg: str, series: str, status: str | None = "Published") -> list[Pub]:
    params = {
        "ws.op": "getPublishedSources",
        "source_name": archive_pkg,
        "distro_series": f"{API}/ubuntu/{series}",
        "exact_match": "true",
        "ws.size": "150",
    }
    if status:
        params["status"] = status
    url: str | None = f"{API}/ubuntu/+archive/primary?" + urllib.parse.urlencode(params)
    pubs: list[Pub] = []
    while url:
        d = json.loads(_get(url))
        for e in d.get("entries", []):
            pubs.append(Pub(e["source_package_version"], e["pocket"], e.get("date_published") or "", e["self_link"]))
        url = d.get("next_collection_link")
    return pubs


def newest_in_pockets(pubs: list[Pub], pockets: list[str]) -> Pub | None:
    sel = [p for p in pubs if p.pocket in pockets]
    return max(sel, key=lambda p: versions.Version(p.version)) if sel else None


def changelog_text(pub: Pub) -> str:
    url = json.loads(_get(pub.self_link + "?ws.op=changelogUrl"))
    if not url:
        raise LaunchpadError(f"no changelog for {pub.version}")
    return _get(url).decode("utf-8", "replace")


def changes_between(text: str, base: str, target: str) -> tuple[int, set[str]]:
    """Number of changelog entries above base up to target, and the CVE ids they mention."""
    n = 0
    cves: set[str] = set()
    for block in Changelog(text):
        v = str(block.version)
        if versions.gt(v, base) and not versions.gt(v, target):
            n += 1
            cves |= set(CVE_RE.findall(str(block)))
    return n, cves


def source_file_urls(pub: Pub) -> list[str]:
    return json.loads(_get(pub.self_link + "?ws.op=sourceFileUrls"))


def download(url: str, dest: Path, retries: int = 3) -> None:
    last: Exception | None = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r, dest.open("wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(1 + i)
    raise LaunchpadError(f"download {url} failed: {last}")
