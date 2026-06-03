"""
AC#8 — Portability guard.

Asserts that mail_evidence:
  1. Does not import gspread, Google, or any invoicing-assistant module.
  2. Does not contain the string "billable" anywhere in its source files.

This test enforces the §1 boundary: all domain judgment is injected; the
package itself is domain-agnostic.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).parent.parent / "mail_evidence"

_FORBIDDEN_IMPORTS = {"gspread", "google", "googleapiclient", "invoicing_rules"}
_FORBIDDEN_STRING = "billable"


def _all_py_files() -> list[Path]:
    return sorted(_PACKAGE_ROOT.rglob("*.py"))


def test_no_forbidden_imports():
    """mail_evidence must not import gspread, Google, or invoicing_rules."""
    violations: list[str] = []

    for path in _all_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in _FORBIDDEN_IMPORTS:
                        violations.append(
                            f"{path.relative_to(_PACKAGE_ROOT)}: import {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    if top in _FORBIDDEN_IMPORTS:
                        violations.append(
                            f"{path.relative_to(_PACKAGE_ROOT)}: from {node.module} import ..."
                        )

    assert not violations, "mail_evidence imports forbidden modules:\n" + "\n".join(
        violations
    )


def test_no_billable_string():
    """mail_evidence source must not contain the word 'billable'."""
    violations: list[str] = []
    for path in _all_py_files():
        text = path.read_text(encoding="utf-8")
        if _FORBIDDEN_STRING in text.lower():
            # Find all lines containing the word.
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _FORBIDDEN_STRING in line.lower():
                    violations.append(
                        f"{path.relative_to(_PACKAGE_ROOT)}:{lineno}: {line.strip()}"
                    )

    assert not violations, (
        "mail_evidence contains 'billable' (domain logic must be injected):\n"
        + "\n".join(violations)
    )


def test_package_importable_without_network():
    """
    Importing mail_evidence must not trigger any network call or LLM dependency.

    We verify by importing the package fresh (removing from sys.modules first)
    and asserting no forbidden modules were loaded as a side effect.
    """
    # Remove mail_evidence from cache to force a fresh import.
    to_remove = [k for k in sys.modules if k.startswith("mail_evidence")]
    for key in to_remove:
        del sys.modules[key]

    importlib.import_module("mail_evidence")

    # Check that gspread / google / openai / anthropic were NOT imported as side effects.
    _NETWORK_MODULES = {"gspread", "google", "openai", "anthropic", "httpx"}
    loaded = {k.split(".")[0] for k in sys.modules}
    violations = loaded & _NETWORK_MODULES
    # httpx is a core dep of the parent project; only flag actual domain modules.
    violations -= {"httpx"}
    assert not violations, (
        f"Importing mail_evidence pulled in network modules: {violations}"
    )
