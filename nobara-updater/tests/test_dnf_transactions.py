from __future__ import annotations

import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"
PACKAGE_NAME = "nobara_updater"
MODULE_NAME = f"{PACKAGE_NAME}._dnf_transaction_test_target"

if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(SOURCE_DIR)]
    sys.modules[PACKAGE_NAME] = package

spec = importlib.util.spec_from_file_location(MODULE_NAME, SOURCE_DIR / "dnf.py")
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load dnf.py for testing")
dnf_module = importlib.util.module_from_spec(spec)
sys.modules[MODULE_NAME] = dnf_module
spec.loader.exec_module(dnf_module)


class FakeResolvedTransaction:
    pass


class UpgradeGoal:
    latest = None

    def __init__(self, base):
        self.upgrades: list[str] = []
        self.installs: list[str] = []
        UpgradeGoal.latest = self

    def add_upgrade(self, target, settings=None):
        self.upgrades.append(target)

    def add_install(self, target, settings=None):
        self.installs.append(target)

    def resolve(self):
        return FakeResolvedTransaction()


class UpgradeSettings:
    latest = None

    def __init__(self):
        self.best = None
        self.skip_broken = None
        self.skip_unavailable = None
        UpgradeSettings.latest = self

    def set_best(self, enabled):
        self.best = enabled

    def set_skip_broken(self, enabled):
        self.skip_broken = enabled

    def set_skip_unavailable(self, enabled):
        self.skip_unavailable = enabled


class RevertSettings:
    latest = None

    def __init__(self):
        self.ignore_installed = False
        RevertSettings.latest = self

    def set_ignore_installed(self, enabled):
        self.ignore_installed = enabled


class RevertGoal:
    latest = None

    def __init__(self, base):
        self.allow_erasing = False
        self.history_transactions = None
        self.settings = None
        RevertGoal.latest = self

    def set_allow_erasing(self, enabled):
        self.allow_erasing = enabled

    def add_revert_transactions(self, history_transactions, settings):
        self.history_transactions = history_transactions
        self.settings = settings

    def resolve(self):
        return FakeResolvedTransaction()


class HistoryTransaction:
    def __init__(self, state):
        self.state = state

    def get_state(self):
        return self.state


class History:
    def __init__(self, transaction):
        self.transaction = transaction
        self.requested_range = None

    def list_transactions(self, first, last):
        self.requested_range = (first, last)
        return [self.transaction]


class HistoryBase:
    def __init__(self, history):
        self.history = history

    def get_transaction_history(self):
        return self.history


class ResolvePackage:
    def __init__(self, nevra):
        self.nevra = nevra

    def get_nevra(self):
        return self.nevra


class ResolveLog:
    def __init__(self, problem, message):
        self.problem = problem
        self.message = message

    def get_problem(self):
        return self.problem

    def to_string(self):
        return self.message


class ResolveTransaction:
    def __init__(self, conflicts=(), broken=(), logs=()):
        self.conflicts = conflicts
        self.broken = broken
        self.logs = logs

    def get_conflicting_packages(self):
        return self.conflicts

    def get_broken_dependency_packages(self):
        return self.broken

    def get_resolve_logs(self):
        return self.logs


class DnfTransactionPlanningTests(unittest.TestCase):
    def test_pending_install_items_are_installed_but_installed_items_upgrade(self):
        expected = dnf_module.DnfTransactionOutcome(True)
        with (
            patch.object(dnf_module, "_prepare_transaction_base", return_value=object()),
            patch.object(
                dnf_module,
                "_installed_package_specs",
                return_value=frozenset({"installed-package"}),
            ),
            patch.object(dnf_module.dnf5_base, "Goal", UpgradeGoal),
            patch.object(
                dnf_module.dnf5_base, "GoalJobSettings", UpgradeSettings
            ),
            patch.object(dnf_module, "_log_transaction_resolve_problems"),
            patch.object(dnf_module, "_resolve_failure_reason", return_value=None),
            patch.object(
                dnf_module,
                "_execute_resolved_transaction",
                return_value=expected,
            ),
        ):
            outcome = dnf_module.run_package_upgrade_transaction(
                ("installed-package", "new-dependency"), logging.getLogger("test")
            )

        self.assertIs(outcome, expected)
        self.assertEqual(UpgradeGoal.latest.upgrades, ["installed-package"])
        self.assertEqual(UpgradeGoal.latest.installs, ["new-dependency"])
        self.assertTrue(UpgradeSettings.latest.best)
        self.assertFalse(UpgradeSettings.latest.skip_broken)
        self.assertFalse(UpgradeSettings.latest.skip_unavailable)

    def test_incomplete_history_reverts_applied_subset_with_erasing_enabled(self):
        history_transaction = HistoryTransaction(
            dnf_module.dnf5_trans.TransactionState_ERROR
        )
        history = History(history_transaction)
        base = HistoryBase(history)
        expected = dnf_module.DnfTransactionOutcome(True)

        with (
            patch.object(dnf_module, "_prepare_transaction_base", return_value=base),
            patch.object(dnf_module.dnf5_base, "Goal", RevertGoal),
            patch.object(dnf_module.dnf5_base, "GoalJobSettings", RevertSettings),
            patch.object(dnf_module, "_log_transaction_resolve_problems"),
            patch.object(dnf_module, "_resolve_failure_reason", return_value=None),
            patch.object(
                dnf_module,
                "_execute_resolved_transaction",
                return_value=expected,
            ),
        ):
            outcome = dnf_module.revert_package_transaction(
                123, logging.getLogger("test")
            )

        self.assertIs(outcome, expected)
        self.assertEqual(history.requested_range, (123, 123))
        self.assertTrue(RevertGoal.latest.allow_erasing)
        self.assertEqual(RevertGoal.latest.history_transactions, [history_transaction])
        self.assertTrue(RevertSettings.latest.ignore_installed)

    def test_conflicts_and_strict_resolution_logs_are_failures(self):
        conflict = ResolveTransaction(conflicts=(ResolvePackage("foo-2.x86_64"),))
        self.assertIn("Package conflicts", dnf_module._resolve_failure_reason(conflict))

        unavailable = ResolveTransaction(
            logs=(
                ResolveLog(
                    dnf_module.dnf5_base.GoalProblem_NOT_AVAILABLE,
                    "foo is not available",
                ),
            )
        )
        self.assertIn(
            "foo is not available",
            dnf_module._resolve_failure_reason(unavailable, strict_logs=True),
        )

        missing_unapplied_item = ResolveTransaction(
            logs=(
                ResolveLog(
                    dnf_module.dnf5_base.GoalProblem_NOT_INSTALLED,
                    "foo was never installed",
                ),
            )
        )
        self.assertIsNone(
            dnf_module._resolve_failure_reason(
                missing_unapplied_item,
                strict_logs=True,
                allowed_log_problems={
                    dnf_module.dnf5_base.GoalProblem_NOT_INSTALLED
                },
            )
        )


if __name__ == "__main__":
    unittest.main()
