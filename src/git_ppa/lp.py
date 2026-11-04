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
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise LaunchpadError(f"GET {url} failed: HTTP {e.code}") from None
            last = e
            time.sleep(1 + i)
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


def ppa_versions(reference: str, source_name: str) -> set[str]:
    """Every version of a source ever published in a PPA, deleted and superseded ones too.

    Launchpad never lets a version be uploaded twice to one archive, so all of them are used up.
    reference is ~owner/ubuntu/name. Only a public PPA can be read, the tool has no credential yet.
    """
    m = re.fullmatch(r"~([^/]+)/ubuntu/([^/]+)", reference)
    if not m:
        raise LaunchpadError(f"PPA reference {reference!r} is not ~owner/ubuntu/name")
    params = {"ws.op": "getPublishedSources", "source_name": source_name, "exact_match": "true", "ws.size": "150"}
    url: str | None = f"{API}/~{m[1]}/+archive/ubuntu/{m[2]}?" + urllib.parse.urlencode(params)
    found: set[str] = set()
    try:
        while url:
            d = json.loads(_get(url))
            found |= {e["source_package_version"] for e in d.get("entries", [])}
            url = d.get("next_collection_link")
    except LaunchpadError as e:
        raise LaunchpadError(f"cannot read the PPA {reference} ({e}), a private PPA needs a credential the tool does not have yet, "
                             "check the versions by hand and use --skip-ppa-check") from None
    return found


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
