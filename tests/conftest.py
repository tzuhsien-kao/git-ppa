# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixture repositories: a fake Ubuntu import (upstream) and a fake downstream, all on local disk."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_ppa import profile as profile_mod

CHANGELOG_V1 = """hello (1.0-1ubuntu1) resolute; urgency=medium

  * Initial.

 -- Ubuntu Dev <dev@example.com>  Mon, 01 Jan 2026 00:00:00 +0000
"""

CONTROL_V1 = """Source: hello
Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>

Package: hello
Architecture: any
Description: hello
 test package
"""

RULES_V1 = """#!/usr/bin/make -f
export DEB_BUILD_MAINT_OPTIONS = hardening=+all
# filler 1
# filler 2
# filler 3
CFLAGS += -O2
%:
\tdh $@
"""

DOWN_CONTROL = """Source: hello-acme
Maintainer: Tzu <tz@example.com>

Package: hello-acme
Architecture: any
Provides: hello (= ${binary:Version})
Conflicts: hello
Replaces: hello
Description: hello
 test package
"""

DOWN_RULES = """#!/usr/bin/make -f
export DEB_BUILD_MAINT_OPTIONS = hardening=+all
ifeq ($(DEB_HOST_ARCH),arm64)
CC := clang-acme
endif
# filler 1
# filler 2
# filler 3
CFLAGS += -O2
%:
\tdh $@
"""

DOWN_ENTRY = """hello-acme (1.0-1ubuntu1+acme1) resolute; urgency=medium

  * Acme delta.

 -- Tzu <tz@example.com>  Tue, 02 Jan 2026 00:00:00 +0000

"""

NEW_ENTRY = """hello (1.0-1ubuntu1.1) resolute-security; urgency=medium

  * SECURITY UPDATE: CVE-2026-0001

 -- Ubuntu Dev <dev@example.com>  Wed, 03 Jan 2026 00:00:00 +0000

"""


def sh(repo: Path, *args: str) -> str:
    p = subprocess.run(args, cwd=repo, capture_output=True, text=True, check=True)
    return p.stdout.strip()


def git(repo: Path, *args: str) -> str:
    return sh(repo, "git", *args)


def write(repo: Path, rel: str, text: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def commit(repo: Path, msg: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    for k, v in (("NAME", "Test"), ("EMAIL", "test@example.com")):
        monkeypatch.setenv(f"GIT_AUTHOR_{k}", v)
        monkeypatch.setenv(f"GIT_COMMITTER_{k}", v)


@pytest.fixture
def prof(tmp_path) -> profile_mod.Profile:
    return profile_mod.from_dict({
        "project": {"tag": "acme", "series": ["resolute"]},
        "repo": {"url": f"{tmp_path}/{{name}}.git", "upstream_url": f"{tmp_path}/up"},
        "branches": {"mirror": "ubuntu/{series}", "downstream": "{tag}/{series}", "staging": "{tag}-proposed/{series}"},
        "risk": {
            "rules_markers": [r"^\s*ifeq\s*\(\$\(DEB_HOST_ARCH\),\s*arm64\)", r"^\s*(export\s+)?(CC|CXX|CFLAGS)\s*[:+?]?=", "-mcpu"],
            "control_fields": ["Provides", "Conflicts", "Replaces"],
            "files": ["configure.ac", "*.m4"],
        },
        "rules": {"enable": ["version-order", "binary-rename", "no-stale-names", "no-empty-packages"]},
        "package": [{"name": "hello-acme", "archive": "hello"}],
    })


@pytest.fixture
def world(tmp_path, prof):
    """up: the fake importer repo at v1. origin: the downstream repo with mirror and delta branches."""
    up = tmp_path / "up"
    up.mkdir()
    git(up, "init", "-q", "-b", "ubuntu/resolute")
    write(up, "debian/changelog", CHANGELOG_V1)
    write(up, "debian/control", CONTROL_V1)
    write(up, "debian/rules", RULES_V1)
    write(up, "configure.ac", "AC_INIT([hello],[1.0])\n")
    commit(up, "1.0-1ubuntu1 (patches unapplied)")
    git(up, "tag", "import/1.0-1ubuntu1")

    origin = tmp_path / "hello-acme.git"
    git(tmp_path, "clone", "-q", "--bare", str(up), str(origin))
    sh(tmp_path, "git", "--git-dir", str(origin), "tag", "-d", "import/1.0-1ubuntu1")  # the team repo carries no importer tags
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "checkout", "-q", "-b", "acme/resolute")
    write(work, "debian/control", DOWN_CONTROL)
    write(work, "debian/rules", DOWN_RULES)
    write(work, "debian/changelog", DOWN_ENTRY + CHANGELOG_V1)
    commit(work, "hello-acme 1.0-1ubuntu1+acme1")
    git(work, "push", "-q", "origin", "acme/resolute")
    return up


def new_version(up: Path, files: dict[str, str], version: str = "1.0-1ubuntu1.1") -> None:
    """Publish a new archive version in the fake importer repo."""
    for rel, text in files.items():
        write(up, rel, text)
    git(up, "add", "-A")
    git(up, "commit", "-q", "-m", f"{version} (patches unapplied)")
    git(up, "tag", f"import/{version.replace(':', '%').replace('~', '_')}")
