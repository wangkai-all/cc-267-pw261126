"""Tiny test runner (no third-party deps).

Usage:  python3 -m tests.run [-v]

Discovers `test_*` functions in `tests/test_*.py` and runs them.  Each test
gets an isolated temp database.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def discover() -> list:
    import glob

    mods = []
    for path in sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py"))):
        name = "tests." + os.path.splitext(os.path.basename(path))[0]
        mods.append(importlib.import_module(name))
    return mods


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    failures = 0
    passed = 0
    for mod in discover():
        tests = [getattr(mod, n) for n in sorted(dir(mod)) if n.startswith("test_") and callable(getattr(mod, n))]
        print(f"\n{mod.__name__}  ({len(tests)} tests)")
        for fn in tests:
            tmp = tempfile.mkdtemp(prefix="orders-test-")
            os.environ["ORDERS_TEST_DIR"] = tmp
            try:
                fn()
            except Exception:
                failures += 1
                print(f"  FAIL {fn.__name__}")
                traceback.print_exc()
            else:
                passed += 1
                print(f"  ok   {fn.__name__}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
                from tests import conftest

                conftest.cleanup_tmp()
    print(f"\n{passed} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
