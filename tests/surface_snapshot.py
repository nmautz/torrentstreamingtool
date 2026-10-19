"""The backend's outside surface, pinned while main.py is split into modules.

    .venv/bin/python tests/surface_snapshot.py            (check; `make surface`)
    .venv\\Scripts\\python tests\\surface_snapshot.py      (Windows)
    ... --write                                           (re-pin, on purpose only)

Moving code out of main.py must change nothing anyone can see. This imports
`main` (so it needs the venv, unlike the leaf-module tests) and compares what
uvicorn, https_proxy, the dashboard and the iOS app depend on against
tests/fixtures/main_surface.json:

  - every route IN REGISTRATION ORDER (Starlette matches the first one, so
    `/api/library/coverage` must stay ahead of `/api/library/{item_id}`):
    type, methods, path, name, status code, response class, its parameters
  - the OpenAPI document (request models, field types, defaults), by hash
  - mounts, middleware order, exception handlers, the lifespan
  - every top-level name in `main`, with its kind, and a callable's parameters.
    Moved code is re-imported into main, so `main.<name>` keeps resolving.

What a value IS (a path, a found binary) differs per machine, so a plain value
is pinned only by its name. Nothing here is platform-conditional, so a fixture
written on macOS holds on Windows. A failure prints what changed; re-pin with
--write only when the change was meant (a new route, a new function).
"""
import hashlib, inspect, json, os, sys, types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "main_surface.json"
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def _kind(v):
    if isinstance(v, types.ModuleType):
        return "module " + v.__name__
    if inspect.isclass(v):
        return "class"
    if inspect.iscoroutinefunction(v):
        return "async def"
    if inspect.isfunction(v) or inspect.isbuiltin(v):
        return "def"
    return "value"


def _params(fn):
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    return ["%s:%s" % (p.name, p.kind.name) for p in sig.parameters.values()]


def _ann(p):
    t = getattr(p, "type_", None) or getattr(getattr(p, "field_info", None), "annotation", None)
    return getattr(t, "__name__", None) or str(t)


def surface():
    import main
    app = main.app
    routes = []
    for r in app.router.routes:
        row = {"type": type(r).__name__, "path": r.path, "name": getattr(r, "name", None)}
        if getattr(r, "methods", None):
            row["methods"] = sorted(r.methods)
        ep = getattr(r, "endpoint", None)
        if ep is not None:
            row["endpoint"] = getattr(ep, "__name__", repr(ep))
        if type(r).__name__ == "APIRoute":
            d = r.dependant
            row["status_code"] = r.status_code
            rc = getattr(r.response_class, "value", r.response_class)   # unwrap DefaultPlaceholder
            row["response_class"] = getattr(rc, "__name__", str(rc))
            row["include_in_schema"] = r.include_in_schema
            row["params"] = {
                "path": [(p.name, _ann(p)) for p in d.path_params],
                "query": [(p.name, _ann(p)) for p in d.query_params],
                "header": [(p.name, _ann(p)) for p in d.header_params],
                "cookie": [(p.name, _ann(p)) for p in d.cookie_params],
                "body": [(p.name, _ann(p)) for p in d.body_params],
                "request": d.request_param_name,
            }
        routes.append(row)

    middleware = []
    for m in app.user_middleware:
        kw = {k: getattr(v, "__name__", None) or repr(v) for k, v in (m.kwargs or {}).items()}
        middleware.append([m.cls.__name__, kw])

    openapi = json.dumps(app.openapi(), sort_keys=True, separators=(",", ":"))

    names = {}
    for k, v in sorted(vars(main).items()):
        if k.startswith("__"):
            continue
        row = {"kind": _kind(v)}
        if row["kind"] in ("def", "async def"):
            row["params"] = _params(v)
        names[k] = row

    # Through JSON and back, so a fresh surface compares equal to the pinned one
    # (tuples become lists).
    return json.loads(json.dumps({
        "routes": routes,
        "openapi_sha256": hashlib.sha256(openapi.encode()).hexdigest(),
        "middleware": middleware,
        "exception_handlers": sorted(getattr(k, "__name__", str(k)) for k in app.exception_handlers),
        "lifespan": getattr(app.router.lifespan_context, "__name__", None),
        "names": names,
    }))


def _diff(old, new):
    out = []
    for key in ("openapi_sha256", "middleware", "exception_handlers", "lifespan"):
        if old.get(key) != new.get(key):
            out.append("%s changed:\n  was %s\n  now %s" % (key, old.get(key), new.get(key)))
    ro, rn = old.get("routes", []), new.get("routes", [])
    if ro != rn:
        sig = lambda r: (r["type"], tuple(r.get("methods", ())), r["path"])
        so, sn = [sig(r) for r in ro], [sig(r) for r in rn]
        gone, added = set(so) - set(sn), set(sn) - set(so)
        for s in sorted(gone):
            out.append("route gone:  %s %s %s" % (s[0], ",".join(s[1]), s[2]))
        for s in sorted(added):
            out.append("route added: %s %s %s" % (s[0], ",".join(s[1]), s[2]))
        common_o = [s for s in so if s in set(sn)]
        common_n = [s for s in sn if s in set(so)]
        if common_o != common_n:
            for i, (a, b) in enumerate(zip(common_o, common_n)):
                if a != b:
                    out.append("route ORDER changed at #%d: was %s, now %s" % (i, a[2], b[2]))
                    break
        byo = {sig(r): r for r in ro}
        for r in rn:
            o = byo.get(sig(r))
            if o is not None and o != r:
                out.append("route changed: %s %s\n  was %s\n  now %s" % (",".join(r.get("methods", ())), r["path"], o, r))
    no, nn = old.get("names", {}), new.get("names", {})
    for k in sorted(set(no) - set(nn)):
        out.append("name gone from main: %s (%s)" % (k, no[k]["kind"]))
    for k in sorted(set(nn) - set(no)):
        out.append("name added to main: %s (%s)" % (k, nn[k]["kind"]))
    for k in sorted(set(no) & set(nn)):
        if no[k] != nn[k]:
            out.append("name changed: %s\n  was %s\n  now %s" % (k, no[k], nn[k]))
    return out


if __name__ == "__main__":
    now = surface()
    if "--write" in sys.argv:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print("pinned %d routes, %d names -> %s" % (len(now["routes"]), len(now["names"]), FIXTURE))
        sys.exit(0)
    pinned = json.loads(FIXTURE.read_text(encoding="utf-8"))
    problems = _diff(pinned, now)
    if problems:
        print("main.py's surface changed (%d):" % len(problems))
        for p in problems[:200]:
            print("  " + p)
        print("\nIf every change above was meant, re-pin: python tests/surface_snapshot.py --write")
        sys.exit(1)
    print("surface unchanged: %d routes, %d names, openapi %s" % (
        len(now["routes"]), len(now["names"]), now["openapi_sha256"][:12]))
