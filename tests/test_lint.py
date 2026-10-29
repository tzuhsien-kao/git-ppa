# SPDX-License-Identifier: GPL-3.0-or-later
"""Lint rules on plain working trees, no git needed."""

from pathlib import Path

from conftest import DOWN_CONTROL, DOWN_ENTRY, CHANGELOG_V1
from git_ppa import lint


def tree(tmp_path: Path, control=DOWN_CONTROL, changelog=DOWN_ENTRY + CHANGELOG_V1, extra=None) -> Path:
    d = tmp_path / "t"
    (d / "debian").mkdir(parents=True)
    (d / "debian/control").write_text(control)
    (d / "debian/changelog").write_text(changelog)
    (d / "debian/rules").write_text("#!/usr/bin/make -f\n%:\n\tdh $@\n")
    for name, text in (extra or {}).items():
        (d / "debian" / name).write_text(text)
    return d


def rules_hit(findings, rule):
    return [f for f in findings if f.rule == rule]


def test_good_tree_has_no_errors(prof, tmp_path):
    f = lint.run(tree(tmp_path), prof, prof.package("hello-acme"))
    assert [x for x in f if x.severity == "error"] == []


def test_binary_not_renamed_and_missing_relation(prof, tmp_path):
    bad = DOWN_CONTROL.replace("Package: hello-acme", "Package: hello").replace("Replaces: hello\n", "")
    d = tree(tmp_path, control=bad)
    msgs = [f.message for f in lint.run(d, prof, prof.package("hello-acme"), ["binary-rename"])]
    assert any("not renamed" in m for m in msgs)
    d2 = tree(tmp_path / "b", control=DOWN_CONTROL.replace("Replaces: hello\n", ""))
    msgs = [f.message for f in lint.run(d2, prof, prof.package("hello-acme"), ["binary-rename"])]
    assert msgs == ["hello-acme has no Replaces on hello"]


def test_orphan_debhelper_file_is_found(prof, tmp_path):
    d = tree(tmp_path, extra={"hello.install": "usr/bin/*\n", "hello-acme.install": "usr/bin/*\n"})
    f = lint.run(d, prof, prof.package("hello-acme"), ["no-empty-packages"])
    assert [x.path for x in f] == ["debian/hello.install"]


def test_stale_name_in_dpkg_divert(prof, tmp_path):
    d = tree(tmp_path, extra={"hello-acme.postinst": "dpkg-divert --package hello --add /usr/bin/x\n# dpkg-divert --package hello\n"})
    f = lint.run(d, prof, prof.package("hello-acme"), ["no-stale-names"])
    assert len(f) == 1 and "postinst:1" in f[0].message


def test_stale_name_is_not_reported_for_file_names_or_renamed_packages(prof, tmp_path):
    rules = "%:\n\tdh_installlogrotate --package hello-acme --name=hello\n\tdpkg-divert --package=hello-acme --add /usr/bin/hello\n"
    d = tree(tmp_path)
    (d / "debian/rules").write_text(rules)
    assert lint.run(d, prof, prof.package("hello-acme"), ["no-stale-names"]) == []


def test_version_order_rules(prof, tmp_path):
    pkg = prof.package("hello-acme")
    wrong_source = DOWN_ENTRY.replace("hello-acme", "hello") + CHANGELOG_V1
    assert any("expected hello-acme" in f.message for f in lint.run(tree(tmp_path, changelog=wrong_source), prof, pkg, ["version-order"]))
    nosuffix = DOWN_ENTRY.replace("+acme1", "") + CHANGELOG_V1
    assert any("no +acme" in f.message for f in lint.run(tree(tmp_path / "b", changelog=nosuffix), prof, pkg, ["version-order"]))
    older = DOWN_ENTRY.replace("1.0-1ubuntu1+acme1", "1.0-1ubuntu1+acme2") + DOWN_ENTRY.replace("acme1", "acme3") + CHANGELOG_V1
    assert any("previous downstream version" in f.message for f in lint.run(tree(tmp_path / "c", changelog=older), prof, pkg, ["version-order"]))
    tilde = DOWN_ENTRY.replace("+acme1", "~acme1") + CHANGELOG_V1
    assert any(f.severity == "warn" and "uses ~" in f.message for f in lint.run(tree(tmp_path / "d", changelog=tilde), prof, pkg, ["version-order"]))


def test_unknown_rule_is_reported_not_ignored(prof, tmp_path):
    f = lint.run(tree(tmp_path), prof, prof.package("hello-acme"), ["no-such-rule"])
    assert f[0].message == "rule is not implemented in this prototype"
