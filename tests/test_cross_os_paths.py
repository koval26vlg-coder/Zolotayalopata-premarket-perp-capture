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


if __name__ == "__main__":
    unittest.main()
