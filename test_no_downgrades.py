import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from no_downgrades import main, parse, show


REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
OTHER_REGISTRY = "registry+https://example.com/index"


def package(name, version, dependencies=(), source: str | None = REGISTRY):
    result = {"name": name, "version": version, "dependencies": list(dependencies)}
    if source is not None:
        result["source"] = source
    return result


def lockfile(packages):
    # JSON strings/arrays are also valid TOML for these ASCII test values.
    return "version = 4\n" + "".join(
        "\n[[package]]\n"
        + "".join(f"{key} = {json.dumps(value)}\n" for key, value in pkg.items())
        for pkg in packages
    )


class SemverTests(unittest.TestCase):
    def test_spec_precedence(self):
        ordered = [
            "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.2",
            "1.0.0-alpha.10", "1.0.0-alpha.beta", "1.0.0-beta",
            "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0",
            "2.0.0", "2.1.0", "2.1.1",
        ]
        for index, lower in enumerate(ordered):
            for higher in ordered[index + 1:]:
                with self.subTest(lower=lower, higher=higher):
                    self.assertLess(parse(lower), parse(higher))

    def test_numeric_identifier_below_text(self):
        self.assertLess(parse("1.0.0-999"), parse("1.0.0-10a"))

    def test_ascii_order(self):
        self.assertLess(parse("1.0.0-Z"), parse("1.0.0-a"))

    def test_build_metadata_ignored(self):
        for version in ("1.2.3", "1.2.3-alpha.10"):
            self.assertEqual(parse(version + "+123"), parse(version + "+abc"))

    def test_show_roundtrip(self):
        for version in ("1.0.0", "1.0.0-alpha.10", "1.0.0-0.a-b.99"):
            self.assertEqual(show(parse(version + "+build")), version)


class LockfileTests(unittest.TestCase):
    def check(self, base, head, expected, diagnostic=None):
        with tempfile.TemporaryDirectory() as directory:
            base_path = Path(directory) / "base.lock"
            head_path = Path(directory) / "head.lock"
            base_path.write_text(lockfile(base))
            head_path.write_text(lockfile(head))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(base_path, head_path)
        self.assertEqual(result, expected, output.getvalue())
        if diagnostic:
            self.assertIn(diagnostic, output.getvalue())
        if expected == 0:
            self.assertEqual(output.getvalue(), "")

    def test_simple_downgrade(self):
        self.check([package("dep", "2.0.0")], [package("dep", "1.0.0")], 1)

    def test_upgrade(self):
        self.check([package("dep", "1.0.0")], [package("dep", "2.0.0")], 0)

    def test_unchanged(self):
        packages = [package("dep", "1.0.0")]
        self.check(packages, packages, 0)

    def test_added_and_removed_packages(self):
        self.check([package("old", "2.0.0")], [package("new", "1.0.0")], 0)
        self.check([], [], 0)

    def test_prerelease_downgrade(self):
        self.check([package("dep", "1.0.0-alpha.10")],
                   [package("dep", "1.0.0-alpha.2")], 1,
                   "1.0.0-alpha.10 on the base branch, 1.0.0-alpha.2 here")

    def test_prerelease_upgrade(self):
        self.check([package("dep", "1.0.0-alpha.2")],
                   [package("dep", "1.0.0-alpha.10")], 0)

    def test_release_to_prerelease(self):
        self.check([package("dep", "1.0.0")], [package("dep", "1.0.0-rc.1")], 1)

    def test_metadata_only_change(self):
        self.check([package("dep", "1.0.0+z")], [package("dep", "1.0.0+a")], 0)

    def test_different_sources_are_not_compared(self):
        self.check([package("dep", "2.0.0")],
                   [package("dep", "1.0.0", source=OTHER_REGISTRY)], 0)

    def test_highest_locked_version_still_checked(self):
        self.check([package("dep", "1.0.0"), package("dep", "3.0.0")],
                   [package("dep", "1.0.0"), package("dep", "2.0.0")], 1)

    def test_coreos_installer_regression(self):
        # Minimized from coreos/coreos-installer#1772, base 6235c0ad53f66273bee3a357d5687e97afeca635
        # and head 42716b7da1f17ca686c04ac40b5ee4a40427396f. Both rand versions remain locked.
        shared = [package("rand", "0.8.8"), package("rand", "0.9.5"),
                  package("rust_decimal", "1.43.0", ["rand 0.8.8", "rand 0.9.5"])]
        base = [package("coreos-installer", "0.26.0", ["rand 0.9.5"], None)] + shared
        head = [package("coreos-installer", "0.27.0", ["rand 0.8.8"], None)] + shared
        self.check(base, head, 1, "coreos-installer 0.26.0 -> 0.27.0 -> rand")
        self.check(head, base, 1, "coreos-installer: 0.27.0")  # root version downgrade
        self.check(base, base, 0)
        self.check(head, head, 0)

    def test_dependency_switch_without_parent_version_change(self):
        shared = [package("dep", "1.0.0"), package("dep", "2.0.0")]
        self.check([package("app", "1.0.0", ["dep 2.0.0"])] + shared,
                   [package("app", "1.0.0", ["dep 1.0.0"])] + shared, 1, "app 1.0.0 -> dep")

    def test_dependency_switch_upgrade(self):
        shared = [package("dep", "1.0.0"), package("dep", "2.0.0")]
        self.check([package("app", "1.0.0", ["dep 1.0.0"])] + shared,
                   [package("app", "2.0.0", ["dep 2.0.0"])] + shared, 0)

    def test_unqualified_dependency_resolved_per_lockfile(self):
        self.check([package("app", "1.0.0", ["dep"]), package("dep", "2.0.0")],
                   [package("app", "1.0.0", ["dep"]), package("dep", "1.0.0")], 1,
                   "app 1.0.0 -> dep")

    def test_qualification_change_is_not_a_downgrade(self):
        self.check([package("app", "1.0.0", ["dep"]), package("dep", "2.0.0")],
                   [package("app", "1.0.0", ["dep 2.0.0"]),
                    package("dep", "2.0.0"), package("dep", "1.0.0")], 0)

    def test_source_qualified_dependency(self):
        shared = [package("dep", "2.0.0"), package("dep", "1.0.0"),
                  package("dep", "1.0.0", source=OTHER_REGISTRY)]
        self.check([package("app", "1.0.0", [f"dep 2.0.0 ({REGISTRY})"])] + shared,
                   [package("app", "1.0.0", [f"dep 1.0.0 ({REGISTRY})"])] + shared, 1)

    def test_source_switch_not_compared(self):
        shared = [package("dep", "2.0.0"), package("dep", "1.0.0", source=OTHER_REGISTRY)]
        self.check([package("app", "1.0.0", [f"dep 2.0.0 ({REGISTRY})"])] + shared,
                   [package("app", "1.0.0", [f"dep 1.0.0 ({OTHER_REGISTRY})"])] + shared, 0)

    def test_path_dependency_wins_over_same_version_registry_package(self):
        shared = [package("dep", "1.0.0"), package("dep", "1.0.0", source=None)]
        packages = [package("app", "1.0.0", ["dep 1.0.0"])] + shared
        self.check(packages, packages, 0)

    def test_source_qualified_git_dependency_omits_commit(self):
        source = "git+https://example.com/dep?branch=stable"
        shared = [package("dep", "1.0.0", source=source + "#abcdef"),
                  package("dep", "1.0.0", source=OTHER_REGISTRY)]
        packages = [package("app", "1.0.0", [f"dep 1.0.0 ({source})"])] + shared
        self.check(packages, packages, 0)

    def test_same_git_source_dependency_switch(self):
        source = "git+https://example.com/dep#abcdef"
        shared = [package("dep", "1.0.0", source=source), package("dep", "2.0.0", source=source)]
        self.check([package("app", "1.0.0", ["dep 2.0.0"])] + shared,
                   [package("app", "1.0.0", ["dep 1.0.0"])] + shared, 1)

    def test_git_revision_changes_not_ordered(self):
        self.check([package("dep", "1.0.0", source="git+https://example.com/dep#ffff")],
                   [package("dep", "1.0.0", source="git+https://example.com/dep#aaaa")], 0)

    def test_unrelated_parent_versions_not_combined(self):
        shared = [package("dep", "1.0.0"), package("dep", "2.0.0")]
        base = [package("app", "1.0.0", ["dep 2.0.0"]),
                package("app", "2.0.0", ["dep 1.0.0"])] + shared
        self.check(base, base[1:], 0)

    def test_one_changed_parent_among_multiple_versions(self):
        shared = [package("app", "3.0.0", ["dep 2.0.0"]),
                  package("dep", "1.0.0"), package("dep", "2.0.0")]
        self.check([package("app", "1.0.0", ["dep 2.0.0"])] + shared,
                   [package("app", "2.0.0", ["dep 1.0.0"])] + shared, 1)

    def test_ambiguous_parent_updates_not_guessed(self):
        shared = [package("app", "5.0.0"), package("dep", "1.0.0"), package("dep", "2.0.0")]
        base = [package("app", "1.0.0", ["dep 2.0.0"]), package("app", "2.0.0")] + shared
        head = [package("app", "3.0.0", ["dep 1.0.0"]), package("app", "4.0.0")] + shared
        self.check(base, head, 0)

    def test_added_or_removed_edge_does_not_fail(self):
        shared = [package("dep", "1.0.0"), package("dep", "2.0.0")]
        for before, after in [([], ["dep 1.0.0"]), (["dep 2.0.0"], []),
                              (["dep 2.0.0"], ["dep 1.0.0", "dep 2.0.0"]),
                              (["dep 1.0.0", "dep 2.0.0"], ["dep 1.0.0"])]:
            with self.subTest(before=before, after=after):
                self.check([package("app", "1.0.0", before)] + shared,
                           [package("app", "1.0.0", after)] + shared, 0)

    def test_switch_below_unchanged_highest_dependency(self):
        shared = [package("dep", version) for version in ("1.0.0", "2.0.0", "3.0.0")]
        self.check([package("app", "1.0.0", ["dep 2.0.0", "dep 3.0.0"])] + shared,
                   [package("app", "1.0.0", ["dep 1.0.0", "dep 3.0.0"])] + shared, 1)

    def test_multiple_dependency_replacements(self):
        shared = [package("dep", f"{v}.0.0") for v in range(1, 6)]
        self.check([package("app", "1.0.0", ["dep 2.0.0", "dep 4.0.0"])] + shared,
                   [package("app", "1.0.0", ["dep 1.0.0", "dep 5.0.0"])] + shared, 1)

    def test_unequal_dependency_replacements_not_guessed(self):
        shared = [package("dep", f"{v}.0.0") for v in range(1, 4)]
        self.check([package("app", "1.0.0", ["dep 2.0.0", "dep 3.0.0"])] + shared,
                   [package("app", "1.0.0", ["dep 1.0.0"])] + shared, 0)

    def test_dependency_prerelease_switch(self):
        shared = [package("dep", "1.0.0-alpha.2"), package("dep", "1.0.0-alpha.10")]
        self.check([package("app", "1.0.0", ["dep 1.0.0-alpha.10"])] + shared,
                   [package("app", "1.0.0", ["dep 1.0.0-alpha.2"])] + shared, 1)

    def test_duplicate_packages_rejected(self):
        packages = [package("dep", "1.0.0"), package("dep", "1.0.0")]
        with self.assertRaises(ValueError):
            self.check(packages, packages, 0)

    def test_legacy_root_table(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base.lock"
            head = Path(directory) / "head.lock"
            shared = lockfile([package("dep", "1.0.0"), package("dep", "2.0.0")])
            base.write_text(shared + '\n[root]\nname = "app"\nversion = "1.0.0"\ndependencies = ["dep 2.0.0"]\n')
            head.write_text(shared + '\n[root]\nname = "app"\nversion = "1.0.0"\ndependencies = ["dep 1.0.0"]\n')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(base, head), 1)

    def test_unknown_or_ambiguous_dependencies_fail_closed(self):
        for dependency in ("missing", "dep", "dep invalid syntax"):
            with self.subTest(dependency=dependency), self.assertRaises(ValueError):
                packages = [package("app", "1.0.0", [dependency]),
                            package("dep", "1.0.0"), package("dep", "2.0.0")]
                self.check(packages, packages, 0)

    def test_cli_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base.lock"
            head = Path(directory) / "head.lock"
            base.write_text(lockfile([package("dep", "2.0.0")]))
            head.write_text(lockfile([package("dep", "1.0.0")]))
            script = Path(__file__).with_name("no_downgrades.py")
            for first, second, expected in ((base, head, 1), (head, base, 0), (base, base, 0)):
                result = subprocess.run([sys.executable, script, first, second], capture_output=True)
                self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
