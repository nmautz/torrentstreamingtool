"""The backend's source, for the tests that lift functions out of it by name.

main.py can't be imported without its whole dependency tree, so a dozen tests
parse its source and exec the functions they need. main.py is being split into
the `backend/` package, and a function that moves must stay findable without
every test learning the new address. `read()` is main.py followed by every
module in backend/, so `ast.parse(read())` sees each top-level def wherever it
lives now. A name that exists nowhere still fails the test's own "not found"
assert.

The package must not use `from __future__` imports: one in the middle of the
joined text is a SyntaxError.
"""
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def files():
    pkg = ROOT / "backend"
    return [ROOT / "main.py"] + (sorted(pkg.rglob("*.py")) if pkg.is_dir() else [])


def read():
    return "\n\n".join(io.open(p, encoding="utf-8").read() for p in files())
