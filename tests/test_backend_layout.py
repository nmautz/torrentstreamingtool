"""Guard the split of main.py into the backend/ package. Plain python, no deps:

    python tests/test_backend_layout.py      (or `make test`)

Moving code between Python modules fails silently in a few known ways. Each
rule here is one of them:

  1. backend/ never imports main. main is the composition root: it imports the
     package, never the reverse, so there is no import cycle to half-initialise.
  2. Nothing does `from <module> import X` when X is REBOUND at runtime
     (`global X; X = ...`, e.g. the qBit client built in the lifespan). The
     importer keeps the old object forever. Read it as `module.X` instead.
  3. A top-level def/class is defined once across main.py + backend/. A move
     that leaves the old copy behind gives two versions, and which one runs
     depends on import order.
  4. No `from __future__` in backend/ (tests/_backend_src.py joins the sources).

The outside surface (routes, their order, request models, every name main
exports) is pinned separately by tests/surface_snapshot.py, which needs the venv.
"""
import ast, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _backend_src                       # noqa: E402

fails = []
passed = 0


def check(name, ok, detail=""):
    global passed
    if ok:
        passed += 1
    else:
        fails.append(name + (": " + detail if detail else ""))


def modname(path):
    rel = os.path.relpath(str(path), ROOT)[:-3].replace(os.sep, ".")
    return rel[:-9] if rel.endswith(".__init__") else rel


trees = {}
for p in _backend_src.files():
    with open(p, encoding="utf-8") as f:
        trees[modname(p)] = ast.parse(f.read(), str(p))
pkg = {m: t for m, t in trees.items() if m != "main"}

# 1. backend/ never imports main
bad = []
for m, t in pkg.items():
    for n in ast.walk(t):
        if isinstance(n, ast.Import) and any(a.name == "main" for a in n.names):
            bad.append(m)
        elif isinstance(n, ast.ImportFrom) and n.module == "main" and not n.level:
            bad.append(m)
check("backend/ never imports main", not bad, ", ".join(sorted(set(bad))))

# 2. no from-import of a name its module rebinds at runtime
rebound = {}                              # module -> names it rebinds via `global`
for m, t in trees.items():
    for n in ast.walk(t):
        if isinstance(n, ast.Global):
            rebound.setdefault(m, set()).update(n.names)
bad = []
for m, t in trees.items():
    for n in ast.walk(t):
        if isinstance(n, ast.ImportFrom) and n.module in rebound and not n.level:
            stale = [a.name for a in n.names if a.name in rebound[n.module]]
            if stale:
                bad.append("%s: from %s import %s" % (m, n.module, ", ".join(stale)))
check("no from-import of a runtime-rebound global", not bad, "; ".join(bad))

# 3. each top-level def/class defined exactly once
where = {}
for m, t in trees.items():
    for n in t.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            where.setdefault(n.name, []).append(m)
dupes = {k: v for k, v in where.items() if len(v) > 1}
check("each top-level def/class defined once", not dupes,
      "; ".join("%s in %s" % (k, ", ".join(v)) for k, v in sorted(dupes.items())))

# 4. no __future__ imports in the package
bad = [m for m, t in pkg.items() for n in ast.walk(t)
       if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
check("no from __future__ in backend/", not bad, ", ".join(bad))

# 5. a name pulled in with `from backend.x import name` is never rebound by the
#    importer: that would change the importer's copy only, and the moved code
#    would go on using the old object.
bad = []
for m, t in trees.items():
    imported = {(a.asname or a.name) for n in t.body if isinstance(n, ast.ImportFrom)
                and (n.module or "").startswith("backend") for a in n.names}
    hit = set()
    for n in ast.walk(t):
        if isinstance(n, ast.Global):
            hit.update(set(n.names) & imported)
    for n in t.body:
        tgts = (n.targets if isinstance(n, ast.Assign)
                else [n.target] if isinstance(n, (ast.AnnAssign, ast.AugAssign)) else [])
        for tg in tgts:
            hit.update(x.id for x in ast.walk(tg) if isinstance(x, ast.Name) and x.id in imported)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in imported:
            hit.add(n.name)
    bad += ["%s rebinds %s" % (m, x) for x in sorted(hit)]
check("an imported backend name is never rebound", not bad, "; ".join(bad))

print("backend_layout: %d passed, %d failed (%d backend module(s))" % (passed, len(fails), len(pkg)))
for f in fails:
    print("  FAIL " + f)
sys.exit(1 if fails else 0)
