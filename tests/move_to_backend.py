"""Move line ranges of main.py, verbatim, into a new backend/ module.

    python tests/move_to_backend.py --to backend/NAME.py --doc "..." START-END [START-END ...] [--dry]

The tool behind the main.py split (docs/BACKEND.md § Splitting main.py). It
needs pyflakes: in the running interpreter, or name an interpreter that has it
with PYFLAKES_PYTHON. pyflakes is not a project dependency; a throwaway venv
is fine.

- A range must cut main.py only between top-level statements (checked).
- The new module's imports come from pyflakes' undefined names in the block:
    imported by main.py              -> the same import, for that name only
    defined in a backend/ module     -> from backend.x import name
    still defined in main.py         -> BLOCKER: printed, nothing written.
                                        Move that first (bottom-up only).
- main.py gets `from backend.NAME import (every name the block defines)` in
  the backend/ block under `import winaccept_patch`, so its namespace is
  unchanged.
- Module-level statements other than defs and assignments are printed as
  NOTEs: they run earlier now (at main's import block), so check them.

After a move: leave a one-line pointer comment where the code was, then run
tests/test_backend_layout.py, tests/surface_snapshot.py (venv), pyflakes on
main.py + backend/, and `make test`. The tool only creates modules; to add to
an existing one, move into a new file and merge by hand.
"""
import argparse, ast, builtins, os, re, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARK = "# ── backend/ package (main.py split; see docs/BACKEND.md) ──"
ANCHOR = "import winaccept_patch\n"


def pyflakes_undefined(text):
    py = os.environ.get("PYFLAKES_PYTHON") or sys.executable
    fd, tmp = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        r = subprocess.run([py, "-m", "pyflakes", tmp], capture_output=True, text=True)
        if "No module named pyflakes" in r.stderr:
            sys.exit("pyflakes not found in %s: pip install it in a throwaway venv and set PYFLAKES_PYTHON" % py)
        return sorted(set(re.findall(r"undefined name '([^']+)'", r.stdout)))
    finally:
        os.unlink(tmp)


def is_stdlib(top):
    names = getattr(sys, "stdlib_module_names", None)       # 3.10+
    if names is not None:
        return top in names
    import importlib.util, sysconfig                         # 3.9
    spec = importlib.util.find_spec(top)
    origin = (spec and spec.origin) or ""
    stdlib = os.path.normcase(sysconfig.get_paths()["stdlib"])
    return origin in ("built-in", "frozen") or (
        os.path.normcase(origin).startswith(stdlib) and "site-packages" not in origin)


def targets(n):
    tg = n.targets if isinstance(n, ast.Assign) else [n.target]
    return [x.id for t in tg for x in ast.walk(t) if isinstance(x, ast.Name)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ranges", nargs="+")
    ap.add_argument("--to", required=True)
    ap.add_argument("--doc", default="")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    main_p = ROOT / "main.py"
    raw = main_p.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in raw else "\n"
    src = raw.replace("\r\n", "\n")
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)

    ranges = sorted(tuple(int(x) for x in r.split("-")) for r in a.ranges)
    for s, e in ranges:
        for n in tree.body:
            start = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
            if (start < s <= n.end_lineno) or (start <= e < n.end_lineno):
                sys.exit("range %d-%d cuts the statement at %d-%d" % (s, e, start, n.end_lineno))
    inr = lambda ln: any(s <= ln <= e for s, e in ranges)

    defined = []
    for n in tree.body:
        if not inr(n.lineno):
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.append(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            defined += [x for x in targets(n) if x not in defined]
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            sys.exit("the block has an import at line %d; move it by hand" % n.lineno)
        elif not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)):
            print("NOTE: module-level statement at %d: %s" % (n.lineno, ast.unparse(n)[:100]))
    block = "".join(l for i, l in enumerate(lines, 1) if inr(i))
    if "__file__" in block:
        # Path(__file__).parent is the repo root in main.py and backend/ in a
        # module: moved verbatim, LIBRARY_FILE would quietly point at an empty
        # backend/library.json. Rewrite those lines to a shared root first.
        sys.exit("the block uses __file__ (%s); point it at the repo root by hand first"
                 % ", ".join(str(i) for i, l in enumerate(lines, 1) if inr(i) and "__file__" in l))

    main_imports, main_defs = {}, set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            for al in n.names:
                main_imports[al.asname or al.name.split(".")[0]] = \
                    "import %s%s" % (al.name, " as " + al.asname if al.asname else "")
        elif isinstance(n, ast.ImportFrom):
            for al in n.names:
                main_imports[al.asname or al.name] = (n.module, al.name, al.asname)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            main_defs.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            main_defs.update(targets(n))
    owner = {}
    for p in sorted((ROOT / "backend").glob("*.py")):
        if p.name == "__init__.py":
            continue
        for n in ast.parse(p.read_text(encoding="utf-8")).body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                owner[n.name] = p.stem
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                for x in targets(n):
                    owner.setdefault(x, p.stem)

    plain, froms, blockers = [], {}, []
    for nm in pyflakes_undefined(block):
        if nm in owner:
            froms.setdefault("backend." + owner[nm], []).append(nm)
        elif nm in main_imports:
            v = main_imports[nm]
            if isinstance(v, str):
                plain.append(v)
            else:
                mod, orig, asn = v
                froms.setdefault(mod, []).append(orig + (" as " + asn if asn else ""))
        elif nm in main_defs:
            blockers.append(nm)
        elif not hasattr(builtins, nm):
            blockers.append(nm + " (not found anywhere)")

    print("defines %d names; needs %s %s" % (len(defined), sorted(plain),
                                             {k: sorted(v) for k, v in froms.items()}))
    if blockers:
        print("BLOCKERS (still in main.py):", blockers)
        sys.exit(2)
    if a.dry:
        return

    third = {"httpx", "psutil", "fastapi", "starlette", "pydantic", "pydantic_settings"}
    def group(mod):
        top = mod.split(".")[0]
        return 0 if is_stdlib(top) else 1 if top in third else 3 if top == "backend" else 2
    imps = [(group(s.split()[1]), s) for s in plain]
    imps += [(group(m), "from %s import %s" % (m, ", ".join(sorted(set(ns))))) for m, ns in froms.items()]
    imps.sort(key=lambda gs: (gs[0], gs[1].startswith("from "), gs[1]))
    hdr, prev = [], None
    for g, s in imps:
        if prev is not None and g != prev:
            hdr.append("")
        hdr.append(s)
        prev = g

    dest = ROOT / a.to
    if dest.exists():
        sys.exit("%s exists; move into a new module and merge by hand" % dest)
    text = '"""%s\n"""\n' % a.doc.strip() + ("\n".join(hdr) + "\n\n\n" if hdr else "\n\n") \
        + block.strip("\n") + "\n"
    dest.write_bytes(text.replace("\n", eol).encode("utf-8"))

    new = re.sub(r"\n{4,}", "\n\n\n", "".join(l for i, l in enumerate(lines, 1) if not inr(i)))
    imp = "from backend.%s import (  # noqa: F401\n%s)\n" % (
        Path(a.to).stem, "".join("    %s,\n" % n for n in defined))
    if MARK not in new:
        assert new.count(ANCHOR) == 1, "main.py's `import winaccept_patch` anchor moved"
        new = new.replace(ANCHOR, ANCHOR + "\n" + MARK + "\n" + imp, 1)
    else:
        j = new.index("\n\n", new.index(MARK))
        new = new[:j + 1] + imp + new[j + 1:]
    main_p.write_bytes(new.replace("\n", eol).encode("utf-8"))
    print("wrote %s (%d lines); main.py is now %d lines" % (a.to, text.count("\n"), new.count("\n")))


if __name__ == "__main__":
    main()
