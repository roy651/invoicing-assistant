"""
Assert the bridge's public callable surface is exactly the read whitelist.

Design: the safety guarantee is structural — forbidden operations simply do not
exist as callable functions.  This test locks that guarantee down so a future
edit that accidentally adds 'close_document' or 'send_document' fails loudly.
"""

import importlib
import inspect
import pkgutil

import morning_bridge
from morning_bridge import reads

# The complete set of public functions that MUST exist in reads.py.
# Any addition or removal here is a deliberate contract change.
REQUIRED_READ_FUNCTIONS = {
    "get_account",
    "get_account_settings",
    "list_businesses",
    "get_business",
    "get_client",
    "search_clients",
    "get_item",
    "search_items",
    "get_document",
    "search_documents",
    "get_document_download_links",
}

# Name fragments that must NOT appear in any public callable across the package.
DENY_FRAGMENTS = frozenset(
    {
        "issue",
        "finalize",
        "send",
        "email",
        "payment",
        "charge",
        "close",
        "reopen",
        "create_client",
        "update_client",
        "delete_client",
        "supplier",
        "expense",
        "webhook",
        "delete",
    }
)


def _public_fns(module) -> set[str]:
    """
    Return the names of public functions *defined* in `module`
    (not imported from elsewhere).
    """
    return {
        name
        for name, obj in inspect.getmembers(module, inspect.isfunction)
        if not name.startswith("_") and obj.__module__ == module.__name__
    }


# ── reads.py surface ─────────────────────────────────────────────────────────


def test_reads_exact_whitelist():
    """reads.py exposes exactly the required functions — no more, no less."""
    actual = _public_fns(reads)
    extra = actual - REQUIRED_READ_FUNCTIONS
    missing = REQUIRED_READ_FUNCTIONS - actual
    assert not extra and not missing, (
        f"Extra functions in reads.py: {extra}\n"
        f"Missing functions from reads.py: {missing}"
    )


# ── package-wide deny list ───────────────────────────────────────────────────


def test_no_deny_list_functions_anywhere():
    """
    Walk every module in morning_bridge and assert no public callable contains
    a deny-listed name fragment.  Catches accidental additions to any module.
    """
    violations: dict[str, set[str]] = {}

    for _importer, modname, _ispkg in pkgutil.walk_packages(
        morning_bridge.__path__, prefix="morning_bridge."
    ):
        mod = importlib.import_module(modname)
        bad = {
            name
            for name, obj in inspect.getmembers(mod, inspect.isfunction)
            if not name.startswith("_")
            and obj.__module__ == modname
            and any(frag in name for frag in DENY_FRAGMENTS)
        }
        if bad:
            violations[modname] = bad

    assert not violations, (
        "Deny-listed function names found in morning_bridge:\n"
        + "\n".join(f"  {mod}: {names}" for mod, names in violations.items())
    )
