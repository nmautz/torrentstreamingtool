"""Unit tests for `installsteps.py`. Run with plain python, no deps:

    python tests/test_installsteps.py      (or `make test`)

Same shape as test_discovery.py: a list of cases and a counter. The clone test
builds a throwaway git remote on disk, so nothing here touches the network; it
is skipped (and says so) on a machine without git.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import installsteps as s            # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


# ── The defaults shown are the defaults setup.py uses ─────────────────────
src = Path(ROOT, "setup.py").read_text(encoding="utf-8")
body = src[src.index("def gather_config"):src.index("def configure_qbittorrent")]
factory = dict(re.findall(r'ask_(?:field|secret)\([^\n]*?"([A-Z_]+)",\s*"([^"]*)"\)', body))
eq("every prompted key has a default here",
   sorted(k for k in re.findall(r'cfg\["([A-Z_]+)"\]\s*=\s*ask_', body) if k not in s.DEFAULTS), [])
for key, val in factory.items():
    eq("default " + key, s.DEFAULTS.get(key), val)
eq("download folder is the one default not written as a literal",
   sorted(set(s.DEFAULTS) - set(factory)), ["QBIT_DOWNLOAD_PATH"])
eq("no shipped admin password", s.DEFAULTS["ADMIN_PASSWORD"], "")

# ── Child output ──────────────────────────────────────────────────────────
env = s.child_env({"X": "1"})
eq("child env pins utf-8", (env["PYTHONIOENCODING"], env["PYTHONUTF8"], env["X"]),
   ("utf-8", "1", "1"))
# The bytes that hung the first wizard: setup.py's header rule, read as cp1252.
try:
    "━━  Python  ━━".encode("utf-8").decode("cp1252")
    undecodable = False
except UnicodeDecodeError:
    undecodable = True
eq("the header rule really is undecodable as cp1252", undecodable, True)

eq("colour stripped", s.clean_line("\x1b[92m  ✓\x1b[0m  venv created\n"), "  ✓  venv created")
eq("redrawn line keeps its last state", s.clean_line("  10%\r  55%\rDone installing\r\n"),
   "Done installing")
eq("spinner dropped", s.clean_line("   - \r   \\ \r   | \n"), "")
eq("progress bar dropped", s.clean_line("  ██████▒▒▒▒▒▒  12.0 MB / 40.1 MB\n"), "")
eq("percent bar dropped", s.clean_line("  ████████████████  100%\n"), "")
eq("a real line with a dash survives", s.clean_line("  - VLC\n"), "  - VLC")
eq("blank stays blank", s.clean_line("\n"), "")

# ── Which Python ──────────────────────────────────────────────────────────
eq("store python is per-user", s.is_per_user_python(
    r"C:\Users\n\AppData\Local\Microsoft\WindowsApps\python.exe"), True)
eq("default python.org install is per-user", s.is_per_user_python(
    r"C:\Users\n\AppData\Local\Programs\Python\Python312\python.exe"), True)
eq("all-users install is fine", s.is_per_user_python(
    r"C:\Program Files\Python312\python.exe"), False)
eq("mac python is fine", s.is_per_user_python("/usr/bin/python3"), False)

eq("same account", s.wrong_account("Nathan", "nathan"), False)
eq("approved with another account", s.wrong_account("Admin", "kid"), True)
eq("unknown session is not an accusation", s.wrong_account("Admin", ""), False)

# ── .env ──────────────────────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp(prefix="sl-installsteps-"))
try:
    envf = tmp / ".env"
    envf.write_text("# Generated\n\nQBIT_URL=http://localhost:8081\nINDEXER_API_KEY=\n"
                    "TMDB_API_KEY=keep=me\n\n# bins\n_VLC_BIN=C:\\vlc.exe\n", encoding="utf-8")
    eq("read", s.read_env(envf), {"QBIT_URL": "http://localhost:8081", "INDEXER_API_KEY": "",
                                  "TMDB_API_KEY": "keep=me", "_VLC_BIN": "C:\\vlc.exe"})
    s.set_env_keys(envf, {"INDEXER_API_KEY": "abc", "JACKETT_PASSWORD": "pw"})
    after = s.read_env(envf)
    eq("replaced in place", after["INDEXER_API_KEY"], "abc")
    eq("appended", after["JACKETT_PASSWORD"], "pw")
    eq("others untouched", (after["TMDB_API_KEY"], after["_VLC_BIN"]), ("keep=me", "C:\\vlc.exe"))
    eq("comments kept", envf.read_text(encoding="utf-8").count("#"), 2)
    eq("missing file reads empty", s.read_env(tmp / "nope"), {})
    s.set_env_keys(tmp / "new.env", {"A": "1"})
    eq("missing file is created", s.read_env(tmp / "new.env"), {"A": "1"})

    # ── Settings ──────────────────────────────────────────────────────────
    good = dict(s.DEFAULTS, ADMIN_PASSWORD="hunter2", QBIT_DOWNLOAD_PATH="D:\\StreamLink")
    eq("good settings pass", s.validate_settings(good), None)
    eq("blank admin password refused",
       "admin password" in (s.validate_settings(dict(good, ADMIN_PASSWORD="  ")) or ""), True)
    eq("blank qbit password refused",
       "can't be blank" in (s.validate_settings(dict(good, QBIT_PASSWORD="")) or ""), True)
    eq("password .env would mangle refused",
       s.validate_settings(dict(good, ADMIN_PASSWORD="a #b")) is not None, True)
    eq("quoted password refused",
       s.validate_settings(dict(good, VLC_PASSWORD='"x"')) is not None, True)
    eq("same port refused", "same port" in (s.validate_settings(
        dict(good, VLC_URL="http://localhost:8081")) or ""), True)
    eq("same port on different hosts is fine", s.validate_settings(
        dict(good, QBIT_URL="http://192.168.1.9:8080")), None)
    eq("relative folder refused",
       "full path" in (s.validate_settings(dict(good, QBIT_DOWNLOAD_PATH="Downloads")) or ""), True)
    eq("posix folder accepted", s.validate_settings(dict(good, QBIT_DOWNLOAD_PATH="/srv/sl")), None)
    eq("buffer must be a number",
       "number" in (s.validate_settings(dict(good, BUFFER_MIN_MB="lots")) or ""), True)
    eq("url needs a scheme", s.validate_settings(dict(good, INDEXER_URL="localhost:9117"))
       is not None, True)

    e2 = s.setup_env(good, "generic", {})
    eq("wizard flag", e2["STREAMLINK_WIZARD"], "1")
    eq("vpn mode passed", e2["STREAMLINK_VPN_MODE"], "generic")
    eq("service left to the wizard", e2["STREAMLINK_INSTALL_SERVICE"], "0")
    eq("admin password seeded", e2["SL_ADMIN_PASSWORD"], "hunter2")
    eq("jackett key is not seeded (it would blank a stored one)",
       "SL_INDEXER_API_KEY" in e2 or "SL_JACKETT_PASSWORD" in e2, False)
    eq("unknown mode falls back to the safe one",
       s.setup_env(good, "wat", {})["STREAMLINK_VPN_MODE"], "mullvad")

    eq("port", s.port_of("http://localhost:8081", 1), 8081)
    eq("port default", s.port_of("http://localhost", 80), 80)
    eq("local url", (s.is_local_url("http://localhost:9117"), s.is_local_url("http://127.0.0.1"),
                     s.is_local_url("http://192.168.1.50:9117")), (True, True, False))

    # ── The seam: what the wizard hands over is what setup.py writes ──────
    saved = dict(os.environ)
    try:
        os.environ.update(s.setup_env(
            dict(good, QBIT_DOWNLOAD_PATH=str(tmp / "dl"), VLC_PASSWORD="vv"), "generic", {}))
        import importlib
        import contextlib
        import io
        setup = importlib.import_module("setup")
        setup._STDIN_INTERACTIVE = False            # as under the wizard: no stdin
        seam = tmp / "seam"
        seam.mkdir()
        setup.HERE, setup.ENV = seam, seam / ".env"
        eq("setup.py sees the wizard", (setup.WIZARD, setup.VPN_MODE), (True, "generic"))
        eq("service is left for the wizard", setup._env_skip("STREAMLINK_INSTALL_SERVICE"), True)

        stored = {"ADMIN_PASSWORD": "old", "INDEXER_API_KEY": "storedkey",
                  "QBIT_PASSWORD": "oldq", "TMDB_API_KEY": "tm", "WINDOWS_ADMIN_USER": "bob",
                  "_VLC_BIN": "C:\\stale\\vlc.exe"}
        with contextlib.redirect_stdout(io.StringIO()):
            cfg = setup.gather_config(stored)
            setup.write_env(cfg, {"vlc": "C:\\new\\vlc.exe"}, stored)
        eq("chosen admin password wins over the stored one", cfg["ADMIN_PASSWORD"], "hunter2")
        eq("chosen vlc password applied", cfg["VLC_PASSWORD"], "vv")
        eq("a key the wizard does not seed keeps its stored value",
           cfg["INDEXER_API_KEY"], "storedkey")
        written = s.read_env(seam / ".env")
        eq("keys setup never prompts for survive the rewrite",
           (written.get("TMDB_API_KEY"), written.get("WINDOWS_ADMIN_USER")), ("tm", "bob"))
        eq("tool paths are re-detected, not kept", written.get("_VLC_BIN"), "C:\\new\\vlc.exe")
        eq("written once each", (seam / ".env").read_text(encoding="utf-8").count("ADMIN_PASSWORD="), 1)

        os.environ["SL_JACKETT_PASSWORD"] = ""
        with contextlib.redirect_stdout(io.StringIO()):
            eq("a seeded blank really blanks the field",
               setup.gather_config({"JACKETT_PASSWORD": "was"})["JACKETT_PASSWORD"], "")

        with contextlib.redirect_stdout(io.StringIO()):
            setup.seed_vpn_mode("generic")
        import json
        lib = json.loads((seam / "library.json").read_text(encoding="utf-8"))
        eq("fresh install: mode seeded", lib["settings"]["vpn_killswitch"]["mode"], "generic")
        eq("fresh library has the shape the server expects",
           (lib["profiles"], lib["items"]), ([], []))
        lib["profiles"] = [{"id": "p1"}]
        lib["settings"]["vpn_killswitch"]["block_ui"] = False
        (seam / "library.json").write_text(json.dumps(lib), encoding="utf-8")
        import socket
        real_conn = socket.create_connection

        def refused(*a, **k):
            raise OSError("nothing listening")

        def listening(*a, **k):
            class C:
                def __enter__(self): return self
                def __exit__(self, *x): return False
            return C()
        try:
            socket.create_connection = listening
            with contextlib.redirect_stdout(io.StringIO()):
                setup.seed_vpn_mode("off")
            lib = json.loads((seam / "library.json").read_text(encoding="utf-8"))
            eq("a running server's library is not written under it",
               lib["settings"]["vpn_killswitch"]["mode"], "generic")
            socket.create_connection = refused
            with contextlib.redirect_stdout(io.StringIO()):
                setup.seed_vpn_mode("off")
        finally:
            socket.create_connection = real_conn
        lib = json.loads((seam / "library.json").read_text(encoding="utf-8"))
        eq("stopped server: mode changed", lib["settings"]["vpn_killswitch"]["mode"], "off")
        eq("and nothing else in the library was lost",
           (lib["profiles"], lib["settings"]["vpn_killswitch"]["block_ui"]),
           ([{"id": "p1"}], False))
        eq("no temp file left", sorted(x.name for x in seam.iterdir()), [".env", "library.json"])
    finally:
        os.environ.clear()
        os.environ.update(saved)

    # ── ZIP folder names ──────────────────────────────────────────────────
    eq("zip main", s.zip_branch("torrentstreamingtool-main"), "main")
    eq("zip alpha, second download", s.zip_branch("torrentstreamingtool-alpha (1)"), "alpha")
    eq("zip feature branch", s.zip_branch("torrentstreamingtool-graphicalsetup"), "graphicalsetup")
    eq("renamed folder says nothing", s.zip_branch("StreamLink"), "")
    eq("bare repo name says nothing", s.zip_branch("torrentstreamingtool"), "")

    # ── Other apps' files ─────────────────────────────────────────────────
    jdir = tmp / "ProgramData" / "Jackett"
    jdir.mkdir(parents=True)
    paths = s.jackett_config_paths({"ProgramData": str(tmp / "ProgramData"),
                                    "APPDATA": str(tmp / "Roaming"), "HOME": str(tmp)})
    eq("service config is looked at first", paths[0], jdir / "ServerConfig.json")
    eq("no file, no key", s.read_jackett_key(paths), "")
    (jdir / "ServerConfig.json").write_text(
        '\ufeff{"Port": 9117, "APIKey": "abcdefghij0123456789abcdefghij01", "AdminPassword": null}',
        encoding="utf-8")
    eq("key read through a BOM", s.read_jackett_key(paths), "abcdefghij0123456789abcdefghij01")
    (jdir / "ServerConfig.json").write_text('{"APIKey": "<script>"}', encoding="utf-8")
    eq("junk is not a key", s.read_jackett_key(paths), "")
    (jdir / "ServerConfig.json").write_text("{not json", encoding="utf-8")
    eq("corrupt file is not a crash", s.read_jackett_key(paths), "")

    eq("vlc untouched (commented default)",
       s.vlc_first_run_done("[qt]\n#qt-privacy-ask=1\n"), False)
    eq("vlc dialog answered", s.vlc_first_run_done("[qt]\nqt-privacy-ask=0\n"), True)
    eq("vlc dialog still set to ask", s.vlc_first_run_done("qt-privacy-ask=1\n"), False)
    eq("no vlcrc at all", s.vlc_first_run_done(""), False)
    eq("vlcrc on windows", s.vlcrc_path({"APPDATA": "C:\\R"}), Path("C:\\R") / "vlc" / "vlcrc")

    mv = tmp / "Mullvad VPN"
    (mv / "resources").mkdir(parents=True)
    (mv / "Mullvad VPN.exe").write_text("")
    eq("mullvad window found beside the cli",
       s.mullvad_app(str(mv / "resources" / "mullvad.exe"), {}), str(mv / "Mullvad VPN.exe"))

    # ── Jackett's answer ──────────────────────────────────────────────────
    eq("jackett unreachable", s.parse_indexers(0, "refused"), ("down", 0))
    eq("jackett two indexers", s.parse_indexers(
        200, '<indexers><indexer id="a" configured="true"/><indexer id="b"/></indexers>'),
       ("ok", 2))
    eq("jackett no indexers yet", s.parse_indexers(200, "<indexers />"), ("ok", 0))
    eq("jackett bad key (200 + error)", s.parse_indexers(
        200, '<error code="100" description="Invalid API Key" />'), ("badkey", 0))
    eq("jackett bad key (401)", s.parse_indexers(401, ""), ("badkey", 0))
    eq("something else on that port", s.parse_indexers(200, "<html>IIS</html>"), ("down", 0))

    eq("tmdb v3 key", s.looks_like_tmdb_v4("0123456789abcdef0123456789abcdef"), False)
    eq("tmdb read-access token", s.looks_like_tmdb_v4("eyJhbGciOiJIUzI1NiJ9.x.y"), True)

    eq("driver result", s.result_of("  ✓  qBittorrent Web UI already on port 8081\nRESULT:ok\n"), "ok")
    eq("driver crashed", s.result_of("Traceback ...\nNameError: x"), "")
    eq("driver detail", s.last_detail("  ⚠  Mullvad: Disconnected\n  ⚠  Connect to Mullvad VPN\nRESULT:down"),
       "Connect to Mullvad VPN")
    for name in ("VPN_DRIVER", "JACKETT_DRIVER", "QBIT_DRIVER"):
        try:
            compile(getattr(s, name), name, "exec")
            ok_ = True
        except SyntaxError:
            ok_ = False
        eq(name + " compiles", ok_, True)

    # ── A ZIP folder becomes a clone ──────────────────────────────────────
    git = shutil.which("git")
    if not git:
        print("installsteps: git not found, clone tests skipped")
    else:
        genv = dict(os.environ, GIT_CONFIG_GLOBAL=str(tmp / "gitconfig"),
                    GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        os.environ["GIT_CONFIG_GLOBAL"] = genv["GIT_CONFIG_GLOBAL"]   # safe.directory lands here

        def g(cwd, *a):
            subprocess.run([git, *a], cwd=str(cwd), env=genv, check=True, capture_output=True)

        work = tmp / "upstream"
        work.mkdir()
        g(work, "init", "-q", "-b", "main")
        (work / "setup.py").write_text("old\n")
        g(work, "add", "."); g(work, "commit", "-q", "-m", "before the installer")
        g(work, "branch", "old")
        for f in ("installer.py", "installsteps.py"):
            (work / f).write_text("# tip\n")
        (work / "setup.py").write_text("tip\n")
        g(work, "add", "."); g(work, "commit", "-q", "-m", "installer")
        g(work, "branch", "alpha")
        url = str(work)

        log = []
        z = tmp / "torrentstreamingtool-alpha"
        z.mkdir()
        (z / "setup.py").write_text("from the zip, a version behind\n")
        (z / "installer.py").write_text("# zip\n")
        (z / ".env").write_text("ADMIN_PASSWORD=mine\n")
        eq("zip folder is cloned", s.ensure_clone(z, git, log.append, url=url), "cloned")
        head = subprocess.run([git, "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(z),
                              capture_output=True, text=True, env=genv).stdout.strip()
        eq("on the branch the folder was named for", head, "alpha")
        up = subprocess.run([git, "rev-parse", "--abbrev-ref", "@{u}"], cwd=str(z),
                            capture_output=True, text=True, env=genv).stdout.strip()
        eq("tracking origin", up, "origin/alpha")
        eq("files brought to the tip", (z / "setup.py").read_text(), "tip\n")
        eq("the user's .env is not touched", (z / ".env").read_text(), "ADMIN_PASSWORD=mine\n")
        st = subprocess.run([git, "status", "--porcelain"], cwd=str(z),
                            capture_output=True, text=True, env=genv).stdout.split()
        eq("only the untracked .env differs", st, ["??", ".env"])
        eq("folder marked safe for the unelevated service",
           str(z.resolve()).replace("\\", "/") in Path(genv["GIT_CONFIG_GLOBAL"]).read_text(), True)
        eq("second run leaves a clone alone", s.ensure_clone(z, git, log.append, url=url), "already")

        r = tmp / "StreamLink"
        r.mkdir()
        (r / "setup.py").write_text("zip\n")
        eq("renamed folder falls back to main", s.ensure_clone(r, git, [].append, url=url), "cloned")
        head = subprocess.run([git, "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(r),
                              capture_output=True, text=True, env=genv).stdout.strip()
        eq("renamed folder is on main", head, "main")

        o = tmp / "torrentstreamingtool-old"
        o.mkdir()
        (o / "installer.py").write_text("# running wizard\n")
        log = []
        eq("a branch without the installer is passed over for main",
           s.ensure_clone(o, git, log.append, url=url, branch="old"), "cloned")
        eq("and the log says why", any("does not have the installer" in l for l in log), True)

        # Nothing on the server has the installer: leave the folder alone.
        bare = tmp / "pre"
        bare.mkdir()
        g(bare, "init", "-q", "-b", "main")
        (bare / "setup.py").write_text("x\n")
        g(bare, "add", "."); g(bare, "commit", "-q", "-m", "no installer anywhere")
        n = tmp / "torrentstreamingtool-main"
        n.mkdir()
        (n / "installer.py").write_text("# running wizard\n")
        eq("no branch has the installer: skipped", s.ensure_clone(n, git, [].append, url=str(bare)),
           "skipped")
        eq("the running wizard is still on disk", (n / "installer.py").exists(), True)
        eq("no half-made .git is left behind", (n / ".git").exists(), False)

        d = tmp / "offline"
        d.mkdir()
        eq("unreachable server: skipped, not raised",
           s.ensure_clone(d, git, [].append, url=str(tmp / "no-such-remote")), "skipped")
        eq("offline leaves no .git", (d / ".git").exists(), False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("installsteps: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
