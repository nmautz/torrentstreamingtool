"""The service wrapper daemon.py writes is valid Python and serves both ports (20.30.11).

    python tests/test_service_wrapper.py      (or `make test`)

The updater rewrites `streamlink_service.py` from `daemon._WRAPPER_CONTENT` on
every apply, and the box runs it at the next boot. A template that doesn't compile
is a box that doesn't start, with nobody there to see why. The template is read
out of daemon.py's source, so this needs no venv and imports nothing from it.
"""
import ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
fails = []


def check(name, ok):
    print(("  ok    " if ok else "  FAIL  ") + name)
    if not ok:
        fails.append(name)


tree = ast.parse((ROOT / "daemon.py").read_text(encoding="utf-8"))
node = next((n for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "_WRAPPER_CONTENT" for t in n.targets)),
            None)
check("daemon.py defines _WRAPPER_CONTENT", node is not None)
wrapper = ast.literal_eval(node.value) if node is not None else ""
try:
    wtree = ast.parse(wrapper)
    check("the wrapper compiles", True)
except SyntaxError as exc:
    wtree = None
    check(f"the wrapper compiles ({exc})", False)


def _config_kwargs(t, app):
    for n in ast.walk(t):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "Config" \
                and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == app:
            return {k.arg: k.value for k in n.keywords}
    return None


if wtree is not None:
    http = _config_kwargs(wtree, "main:app")
    https = _config_kwargs(wtree, "https_proxy:app")
    check("it serves main:app on port 80",
          http is not None and isinstance(http.get("port"), ast.Constant) and http["port"].value == 80)
    check("and the HTTPS proxy on 443",
          https is not None and isinstance(https.get("port"), ast.Constant) and https["port"].value == 443)
    # uvicorn's 5 s default dropped an HTTPS request sent on a connection right
    # after a long download over it: 84 of 482 bundle files on 2026-10-22.
    ka = (https or {}).get("timeout_keep_alive")
    check("the HTTPS server keeps a connection open well past 5 s",
          isinstance(ka, ast.Constant) and ka.value >= 30)

run_tree = ast.parse((ROOT / "run.py").read_text(encoding="utf-8"))
run_https = _config_kwargs(run_tree, "https_proxy:app")
check("run.py's interactive launch matches",
      run_https is not None and isinstance(run_https.get("timeout_keep_alive"), ast.Constant)
      and run_https["timeout_keep_alive"].value == (ka.value if isinstance(ka, ast.Constant) else None))

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
