"""A recorded path must mean the same thing on every machine that reads the plan.

Three separate defects of this class have now been fixed here: verify_resolved_path_
bindings asserting Windows paths equal under POSIX resolve(), and two absoluteness
checks that called the plan's own capture root relative on Linux. Each passed on the
developer's machine and failed on CI, and each looked like a broken plan rather than a
platform-dependent question.

The plan records absolute Windows paths. Nothing that reads them may take the running
interpreter's flavour as the authority on what they mean.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import project_config as config  # noqa: E402


class AbsolutenessTests(unittest.TestCase):
    def test_a_windows_drive_path_is_absolute_on_any_platform(self):
        # PurePosixPath calls this relative; that verdict is about the reader, not
        # about the path.
        self.assertTrue(config.path_is_absolute("E:/trading_mvp/captures"))
        self.assertFalse(PurePosixPath("E:/trading_mvp/captures").is_absolute())

    def test_a_posix_root_path_is_absolute_on_any_platform(self):
        self.assertTrue(config.path_is_absolute("/var/lib/captures"))
        self.assertFalse(PureWindowsPath("/var/lib/captures").is_absolute())

    def test_genuinely_relative_paths_are_still_refused(self):
        # The protection must survive the portability fix.
        for value in ("captures/today", "../captures", "./x", "", None):
            with self.subTest(value=value):
                self.assertFalse(config.path_is_absolute(value))


class PlanBindingTests(unittest.TestCase):
    def test_every_path_the_plan_records_is_absolute_under_this_rule(self):
        plan = json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))
        bindings = plan.get("resolved_path_bindings") or {}
        self.assertTrue(bindings, "the plan records no resolved path bindings")
        for name, value in bindings.items():
            with self.subTest(binding=name):
                self.assertTrue(
                    config.path_is_absolute(value),
                    f"{name}={value!r} is not absolute under either flavour",
                )

    def test_the_verdict_does_not_depend_on_the_running_platform(self):
        plan = json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))
        for value in (plan.get("resolved_path_bindings") or {}).values():
            with self.subTest(value=value):
                by_flavour = (
                    PurePosixPath(str(value)).is_absolute(),
                    PureWindowsPath(str(value)).is_absolute(),
                )
                # Exactly the asymmetry that broke CI: one flavour says yes, the
                # other no. The rule must not care which interpreter is asking.
                self.assertEqual(config.path_is_absolute(value), any(by_flavour))


class NoPlatformFlavourInBoundCodeTests(unittest.TestCase):
    """No bound module may judge a plan-recorded path with the platform's flavour."""

    def test_bound_runtime_never_judges_a_path_with_the_platform_flavour(self):
        # An explicit PurePosixPath/PureWindowsPath receiver states which semantics it
        # means and is fine - that is how path_is_absolute is built. The hazard is
        # Path(...).is_absolute(), whose answer changes with the interpreter.
        offenders = []
        for role, relative in config.BOUND_RUNTIME_FILES:
            if not relative.endswith(".py"):
                continue
            source = (ROOT / relative).read_text(encoding="utf-8")
            for number, line in enumerate(source.splitlines(), start=1):
                if not re.search(r"\.is_absolute\(\)", line):
                    continue
                if any(token in line for token in
                       ("PurePosixPath", "PureWindowsPath", "path_is_absolute")):
                    continue
                offenders.append(f"{relative}:{number}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "use project_config.path_is_absolute for plan-recorded paths:\n"
            + "\n".join(offenders),
        )


class WorstCasePathLengthTests(unittest.TestCase):
    """Paths this repository can produce must fit inside a Windows checkout.

    Two separate breaks came from here: a quarantine archive at 200 characters and a
    retained tombstone at 225, both of which git refused to index. MAX_PATH is 260 for
    the whole absolute path, so what a clone root leaves is the repo-relative budget.
    """

    # 200 was wrong and let a 198-character path through that git still refused: the
    # absolute path was 261 against MAX_PATH 260 on a 62-character checkout root. The
    # budget has to leave room for a root, so it is set from the longest plausible one.
    REPO_RELATIVE_BUDGET = 180

    def test_no_path_in_the_working_tree_exceeds_the_budget(self):
        worst = max(
            (
                (len(item.relative_to(ROOT).as_posix()), item.relative_to(ROOT).as_posix())
                for item in ROOT.rglob("*")
                if ".git" not in item.parts
            ),
            default=(0, ""),
        )
        self.assertLessEqual(
            worst[0],
            self.REPO_RELATIVE_BUDGET,
            f"longest path is {worst[0]} chars: {worst[1]}",
        )

    def test_a_quarantine_tombstone_stays_within_the_budget(self):
        import registry_quarantine as quarantine

        tombstone = quarantine._tombstone_name(
            "mutation_receipts", "20260823T212914Z-bc6c206eb6-8fb85904"
        )
        # The worst case this repository produces: a receipt file inside a tombstoned
        # receipt directory.
        longest_receipt = "0" * 20 + "-" + "f" * 64 + ".json"
        worst = len("docs/registry/") + len(tombstone) + 1 + len(longest_receipt)
        self.assertLessEqual(
            worst,
            self.REPO_RELATIVE_BUDGET,
            f"a tombstoned receipt would sit {worst} chars from the repo root",
        )

if __name__ == "__main__":
    unittest.main()
