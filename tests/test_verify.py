"""Tests for the guarantee checker.

`pytest` proves the parts work; `ledgerlock verify` proves the *claims* hold.
These tests prove the checker itself is not vacuous -- a check that passes no
matter what is worse than no check, because it manufactures confidence.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from ledgerlock.generate.engine import build
from ledgerlock.generate.params import PROFILES
from ledgerlock.generate.writer import write_world
from ledgerlock.verify import _checks_for, committed_dataset_matches


def test_the_committed_dataset_is_what_the_generator_produces():
    """Guards against a hand-edited CSV or a forgotten regeneration.

    Without this, someone could tweak a source file and every published number
    would quietly describe data that is no longer in the repo. Not covered by
    any other test, because the rest of the suite generates into a tmpdir.
    """
    check = committed_dataset_matches()
    assert check.ok, check.detail


def test_missing_data_is_a_failure_not_a_pass(tmp_path):
    """The check must fail when there is nothing to check."""
    check = committed_dataset_matches(tmp_path)
    assert not check.ok
    assert "nothing generated" in check.detail


def test_a_tampered_source_file_is_caught(tmp_path):
    """The point of the check, demonstrated."""
    world = build(replace(PROFILES["smoke"], seed=42))
    write_world(world, tmp_path)
    assert committed_dataset_matches(tmp_path).ok

    orders = tmp_path / "raw" / "orders.csv"
    lines = orders.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace(",paid,", ",failed,")      # one field, one row
    orders.write_text("\n".join(lines) + "\n", encoding="utf-8")

    check = committed_dataset_matches(tmp_path)
    assert not check.ok
    assert "orders.csv" in check.detail


@pytest.mark.parametrize("profile", ["smoke", "default"])
def test_every_critical_guarantee_holds(profile, tmp_path):
    checks, _ = _checks_for(profile, 42, tmp_path)
    failed = [c.name for c in checks if c.critical and not c.ok]
    assert not failed, f"{profile}: {failed}"


def test_the_checker_asserts_something_real(tmp_path):
    """A checklist of eight things that can never fail is decoration."""
    checks, score = _checks_for("smoke", 42, tmp_path)
    assert len(checks) >= 7
    assert sum(1 for c in checks if c.critical) >= 6
    # And it is reading real numbers, not defaults.
    assert score.n_records > 100
    assert score.total_proposed > 0


def test_false_alarms_are_reported_but_never_asserted_away(tmp_path):
    """Deliberate: `default` has 2 false alarms and `scale` has 3, from the
    model disagreeing with borderline narration labels. A critical check
    demanding zero would be an invitation to tune the data until it passed."""
    checks, _ = _checks_for("default", 42, tmp_path)
    open_check = next(c for c in checks if "remain open" in c.name)
    assert not open_check.critical
    assert "false alarms" in open_check.detail


# ---------------------------------------------------------------------------
# packaging
# ---------------------------------------------------------------------------

def test_every_third_party_import_is_a_declared_dependency():
    """Guards F14: httpx and python-dotenv were imported but never declared.

    Invisible on a machine that happens to have them, fatal in a clean venv --
    which is exactly where a stranger runs `pip install -e .`. The first CI run
    ever attempted found it in 40 seconds.
    """
    import ast
    import tomllib
    from importlib.metadata import packages_distributions

    root = Path(__file__).resolve().parents[1]
    meta = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        # "httpx>=0.27" -> "httpx"
        __import__("re").split(r"[<>=!\[;]", spec)[0].strip().lower()
        for spec in (meta["project"]["dependencies"]
                     + [s for v in meta["project"]
                        .get("optional-dependencies", {}).values() for s in v])
    }

    module_to_dist = packages_distributions()
    stdlib = set(__import__("sys").stdlib_module_names)
    undeclared: set[str] = set()

    for py in (root / "src").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import: our own package.
                names = [] if node.level else [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if not name or name in stdlib or name == "ledgerlock":
                    continue
                dists = {d.lower() for d in module_to_dist.get(name, [])}
                if not (dists & declared):
                    undeclared.add(f"{name} (provides: {sorted(dists) or '?'})")

    assert not undeclared, (
        f"imported but not in pyproject dependencies: {sorted(undeclared)}")
