#!/usr/bin/env python3
"""StreamLink graphical first-install wizard.

Launched by ``install.bat`` (which guarantees a usable Python first). This is a
friendly, click-through front-end for ``setup.py`` — it collects the handful of
optional settings, drives ``setup.py`` non-interactively, and then **walks the
user through the manual external steps** (connecting Mullvad, adding a Jackett
indexer + API key) at the moment they make sense: *after* those apps have been
installed and started, so the links/buttons actually work and the pasted API key
can be written straight into ``.env``.

Page flow
---------
Welcome → Configure (pre-install) → Install (runs setup.py) →
Connect VPN (Mullvad) → Add a source (Jackett, with API-key field) → Finish.

How it drives setup.py
----------------------
``setup.py`` already returns prompt defaults when there is no usable stdin, so we
run it with ``stdin`` closed and pass the user's choices through env vars:

  * ``STREAMLINK_WIZARD=1``            — rewrite .env + qBittorrent.ini, never reuse
  * ``SL_<ENV_KEY>=value``            — pre-seed each .env value
  * ``STREAMLINK_INSTALL_STT=0/1``    — AI auto-subtitle deps (whisper.cpp)
  * ``STREAMLINK_INSTALL_SERVICE=0/1``— install the boot/login system service

The Jackett API key is collected *after* the install and written to ``.env``
directly (Jackett has to be running before the key exists).

This file must run under the **system** Python (same as setup.py), so it uses
only the standard library (tkinter ships with the python.org Windows build).
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, ttk
except Exception as exc:  # pragma: no cover - surfaced to the user via the .bat
    print("Tkinter is not available in this Python install.")
    print("Install Python from python.org (it bundles Tk) and re-run install.bat.")
    print(f"Detail: {exc}")
    sys.exit(1)


HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
IS_WIN = sys.platform.startswith("win")

# ── Metro-flat palette (matches the dashboard aesthetic) ──────────────────
BG      = "#141414"   # window background
PANEL   = "#1d1d1d"   # card background
FIELD   = "#262626"   # entry background
LINE    = "#333333"   # hairline borders
TXT     = "#f2f2f2"   # primary text
MUTED   = "#9a9a9a"   # secondary text
ACCENT  = "#2f7fed"   # primary action / links
ACCENT2 = "#256ad1"   # primary action (hover)
OK_GRN  = "#3ec46d"
ERR_RED = "#e0564f"
AMBER   = "#e0a44f"   # in-progress / checking

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Factory defaults — kept in lock-step with setup.py's gather_config().
DEFAULTS = {
    "QBIT_DOWNLOAD_PATH": str(Path.home() / "Downloads" / "StreamLink"),
    "ADMIN_PASSWORD":     "adminadmin",
    "INDEXER_URL":        "http://localhost:9117",
    "INDEXER_API_KEY":    "",
    "INDEXER_CATEGORIES": "0",
    "JACKETT_PASSWORD":   "",
    "QBIT_URL":           "http://localhost:8081",
    "QBIT_USERNAME":      "admin",
    "QBIT_PASSWORD":      "adminadmin",
    "VLC_URL":            "http://localhost:8080",
    "VLC_PASSWORD":       "vlcpassword",
    "BUFFER_MIN_MB":      "15.0",
    "BUFFER_MIN_PCT":     "1.0",
}

# Values the wizard pre-seeds into setup.py at install time (SL_*). The Jackett
# API key + admin password are collected AFTER install (the guided pages) and
# written to .env directly, so they're excluded here.
INSTALL_KEYS = [k for k in DEFAULTS if k not in ("INDEXER_API_KEY", "JACKETT_PASSWORD")]

CATEGORY_CHOICES = [("All content", "0"), ("Movies only", "2000"), ("TV only", "5000")]


class Wizard(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("StreamLink — Installer")
        self.configure(bg=BG)
        self.geometry("800x660")
        self.minsize(740, 600)
        try:
            self.tk.call("tk", "scaling", 1.25)
        except Exception:
            pass

        self._fonts()
        self._vars()

        # Header bar
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=28, pady=(22, 8))
        tk.Label(head, text="STREAMLINK", font=self.f_title, fg=TXT, bg=BG).pack(anchor="w")
        self._steptag = tk.Label(head, text="First-time setup", font=self.f_sub,
                                 fg=MUTED, bg=BG)
        self._steptag.pack(anchor="w")
        tk.Frame(self, bg=LINE, height=1).pack(fill="x", padx=28)

        # Page container
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=28, pady=18)

        self.pages: dict[str, tk.Frame] = {}
        self._build_welcome()
        self._build_configure()
        self._build_install()
        self._build_mullvad()
        self._build_qbit()
        self._build_vlc()
        self._build_jackett()
        self._build_finish()

        self._proc: subprocess.Popen | None = None
        self._log_q: "queue.Queue[str]" = queue.Queue()
        self._show("welcome", "First-time setup")

    # ── styling helpers ───────────────────────────────────────────────────
    def _fonts(self) -> None:
        base = "Segoe UI" if IS_WIN else "Helvetica"
        self.f_title = tkfont.Font(family=base, size=22, weight="bold")
        self.f_sub   = tkfont.Font(family=base, size=11)
        self.f_h     = tkfont.Font(family=base, size=15, weight="bold")
        self.f_body  = tkfont.Font(family=base, size=11)
        self.f_small = tkfont.Font(family=base, size=10)
        self.f_link  = tkfont.Font(family=base, size=11, weight="bold", underline=True)
        self.f_btn   = tkfont.Font(family=base, size=11, weight="bold")
        self.f_mono  = tkfont.Font(family="Consolas" if IS_WIN else "Menlo", size=10)

    def _vars(self) -> None:
        self.v = {k: tk.StringVar(value=val) for k, val in DEFAULTS.items()}
        self.v_category = tk.StringVar(value=CATEGORY_CHOICES[0][0])
        self.var_stt = tk.BooleanVar(value=True)
        self.var_service = tk.BooleanVar(value=True)
        self.var_launch = tk.BooleanVar(value=True)
        self.show_advanced = tk.BooleanVar(value=False)
        # Read-only "match these in the app" display values, refreshed post-install.
        self.d_qbit_port = tk.StringVar(value="8081")
        self.d_qbit_user = tk.StringVar(value="admin")
        self.d_qbit_pass = tk.StringVar(value="adminadmin")
        self.d_qbit_path = tk.StringVar(value=DEFAULTS["QBIT_DOWNLOAD_PATH"])
        self.d_vlc_port = tk.StringVar(value="8080")
        self.d_vlc_pass = tk.StringVar(value="vlcpassword")

    def _refresh_match_values(self) -> None:
        self.d_qbit_port.set(self._port_of(self.v["QBIT_URL"].get(), "8081"))
        self.d_qbit_user.set(self.v["QBIT_USERNAME"].get())
        self.d_qbit_pass.set(self.v["QBIT_PASSWORD"].get())
        self.d_qbit_path.set(self.v["QBIT_DOWNLOAD_PATH"].get())
        self.d_vlc_port.set(self._port_of(self.v["VLC_URL"].get(), "8080"))
        self.d_vlc_pass.set(self.v["VLC_PASSWORD"].get())

    def _button(self, parent, text, cmd, primary=True, **kw):
        bg = ACCENT if primary else FIELD
        fg = "#ffffff" if primary else TXT
        kw.setdefault("padx", 22)
        kw.setdefault("pady", 10)
        b = tk.Button(
            parent, text=text, command=cmd, font=self.f_btn,
            bg=bg, fg=fg, activebackground=ACCENT2 if primary else LINE,
            activeforeground="#ffffff", relief="flat", bd=0,
            cursor="hand2", **kw,
        )
        b.bind("<Enter>", lambda _: b.configure(bg=ACCENT2 if primary else LINE))
        b.bind("<Leave>", lambda _: b.configure(bg=bg))
        return b

    def _link(self, parent, text, url, bg=PANEL):
        lbl = tk.Label(parent, text=text, font=self.f_link, fg=ACCENT, bg=bg,
                       cursor="hand2")
        lbl.bind("<Button-1>", lambda _: self._open(url))
        return lbl

    def _open(self, url: str) -> None:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def _field(self, parent, label, var, secret=False, browse=False, bg=PANEL):
        row = tk.Frame(parent, bg=bg)
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, text=label, font=self.f_small, fg=MUTED, bg=bg,
                 width=20, anchor="w").pack(side="left", padx=(0, 8))
        e = tk.Entry(row, textvariable=var, font=self.f_body, bg=FIELD, fg=TXT,
                     insertbackground=TXT, relief="flat", bd=0,
                     show="•" if secret else "")
        e.pack(side="left", fill="x", expand=True, ipady=5, ipadx=6)
        if browse:
            self._button(row, "Browse", lambda: self._browse(var),
                         primary=False, padx=12, pady=4).pack(side="left", padx=(8, 0))
        return e

    def _steps(self, parent, items, bg=PANEL):
        for i, txt in enumerate(items, 1):
            row = tk.Frame(parent, bg=bg)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"{i}", font=self.f_btn, fg="#ffffff", bg=ACCENT,
                     width=3).pack(side="left", anchor="n", padx=(0, 10), ipady=1)
            tk.Label(row, text=txt, font=self.f_body, fg=TXT, bg=bg,
                     justify="left", anchor="w", wraplength=600).pack(
                         side="left", fill="x", expand=True)

    def _infobox(self, parent, rows, bg=PANEL):
        """A read-only “match these values” panel (label: value pairs)."""
        box = tk.Frame(parent, bg=FIELD)
        box.pack(fill="x", pady=(4, 0))
        for label, var in rows:
            r = tk.Frame(box, bg=FIELD)
            r.pack(fill="x", padx=12, pady=4)
            tk.Label(r, text=label, font=self.f_small, fg=MUTED, bg=FIELD,
                     width=16, anchor="w").pack(side="left")
            tk.Label(r, textvariable=var, font=self.f_mono, fg=TXT, bg=FIELD,
                     anchor="w", justify="left", wraplength=460).pack(
                         side="left", fill="x", expand=True)
        return box

    def _port_of(self, url: str, fallback: str) -> str:
        m = re.search(r":(\d+)", url)
        return m.group(1) if m else fallback

    def _read_env_value(self, key: str) -> str:
        if not ENV_PATH.exists():
            return ""
        for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and s.split("=", 1)[0].strip() == key:
                return s.split("=", 1)[1].strip()
        return ""

    def _launch_app(self, env_key: str, label: str, status_lbl) -> None:
        """Best-effort launch of an installed app via its _*_BIN path in .env."""
        path = self._read_env_value(env_key)
        if not path or not Path(path).exists():
            status_lbl.configure(
                text=f"Couldn't find {label} automatically — open it from the Start menu.",
                fg=ERR_RED)
            return
        try:
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
            subprocess.Popen([path], cwd=str(Path(path).parent), **kwargs)
            status_lbl.configure(text=f"Opened {label}.", fg=OK_GRN)
        except Exception as exc:
            status_lbl.configure(text=f"Couldn't open {label}: {exc}", fg=ERR_RED)

    def _venv_py(self):
        """Path to the venv's Python, or None if setup hasn't built it."""
        cand = HERE / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
        return cand if cand.exists() else None

    def _run_check(self, call_expr: str, label: str, status_lbl, button) -> None:
        """Live-test a service by running run.py's real start/check helper under
        the venv Python (which is where its deps live and where it won't re-exec).
        ``call_expr`` is e.g. ``start_vlc()`` — evaluated as ``run.<call_expr>``.
        """
        vpy = self._venv_py()
        if not vpy:
            status_lbl.configure(
                text="Can't test yet — the .venv isn't ready (did setup finish?).",
                fg=ERR_RED)
            return
        button.configure(state="disabled")
        status_lbl.configure(text=f"Checking {label}… (this can take ~20s)", fg=AMBER)

        def work():
            driver = (
                "import sys, run\n"
                "try:\n"
                f"    res = bool(run.{call_expr})\n"
                "except Exception as ex:\n"
                "    print('ERROR:', ex)\n"
                "    sys.exit(2)\n"
                "sys.exit(0 if res else 1)\n"
            )
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
            try:
                r = subprocess.run([str(vpy), "-c", driver], cwd=str(HERE),
                                   capture_output=True, text=True, timeout=120, **kwargs)
                out = _ANSI.sub("", (r.stdout or "") + (r.stderr or ""))
                lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
                detail = lines[-1] if lines else ""
                rc = r.returncode
            except Exception as ex:
                rc, detail = 99, str(ex)
            self.after(0, lambda: self._check_done(rc, label, status_lbl, button, detail))

        threading.Thread(target=work, daemon=True).start()

    def _check_done(self, rc, label, status_lbl, button, detail):
        button.configure(state="normal")
        if rc == 0:
            status_lbl.configure(text=f"✓ {label} is working.", fg=OK_GRN)
        else:
            msg = f"✗ {label} isn't responding yet."
            if detail:
                msg += f"  {detail}"
            status_lbl.configure(text=msg, fg=ERR_RED)

    def _check(self, parent, text, var, bg=PANEL):
        cb = tk.Checkbutton(parent, text=text, variable=var, font=self.f_body,
                            fg=TXT, bg=bg, activebackground=bg,
                            activeforeground=TXT, selectcolor=FIELD,
                            anchor="w", relief="flat", bd=0,
                            highlightthickness=0, cursor="hand2")
        cb.pack(fill="x", pady=2)
        return cb

    def _browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if d:
            var.set(d)

    def _show(self, name: str, tag: str | None = None) -> None:
        for frame in self.pages.values():
            frame.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        if tag is not None:
            self._steptag.configure(text=tag)

    # ── page: welcome ─────────────────────────────────────────────────────
    def _build_welcome(self) -> None:
        p = tk.Frame(self.body, bg=BG)
        self.pages["welcome"] = p
        card = tk.Frame(p, bg=PANEL)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=26, pady=24)

        tk.Label(inner, text="Welcome", font=self.f_h, fg=TXT, bg=PANEL).pack(anchor="w")
        msg = (
            "This installer sets up everything StreamLink needs on this PC, then\n"
            "walks you through the steps that need you — connecting your VPN,\n"
            "enabling the qBittorrent and VLC controls, and adding a search source.\n\n"
            "It will automatically:\n"
            "   •  install the Python packages into a private .venv\n"
            "   •  install VLC, qBittorrent, Jackett and Mullvad VPN (via winget)\n"
            "   •  download the Smart-Skip tools (ffmpeg + Chromaprint)\n"
            "   •  write your config, create the download folder, and make the\n"
            "      HTTPS certificate for the admin panel\n\n"
            "First you'll pick a few settings — sensible defaults are filled in, so\n"
            "you can just click through if you're unsure. A couple of Windows\n"
            "security prompts (UAC) may appear during the install — click Yes."
        )
        tk.Label(inner, text=msg, font=self.f_body, fg=TXT, bg=PANEL,
                 justify="left", anchor="w").pack(anchor="w", pady=(12, 0))

        tk.Label(inner,
                 text=f"Using Python {sys.version_info.major}.{sys.version_info.minor}."
                      f"{sys.version_info.micro}  ({sys.executable})",
                 font=self.f_small, fg=MUTED, bg=PANEL, justify="left", anchor="w",
                 wraplength=680).pack(anchor="w", pady=(18, 0))

        nav = tk.Frame(p, bg=BG)
        nav.pack(fill="x", pady=(16, 0))
        self._button(nav, "Get started  →",
                     lambda: self._show("configure", "Step 1 — Settings")).pack(side="right")

    # ── page: configure (pre-install) ─────────────────────────────────────
    def _build_configure(self) -> None:
        p = tk.Frame(self.body, bg=BG)
        self.pages["configure"] = p

        card = tk.Frame(p, bg=PANEL)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=26, pady=22)

        tk.Label(inner, text="Settings", font=self.f_h, fg=TXT, bg=PANEL).pack(anchor="w")
        tk.Label(inner, text="Defaults are ready to go — change only what you want. "
                             "You'll add your VPN and search source after install.",
                 font=self.f_small, fg=MUTED, bg=PANEL,
                 wraplength=680, justify="left").pack(anchor="w", pady=(2, 12))

        self._field(inner, "Download folder", self.v["QBIT_DOWNLOAD_PATH"], browse=True)
        self._field(inner, "Admin panel password", self.v["ADMIN_PASSWORD"], secret=True)

        catrow = tk.Frame(inner, bg=PANEL)
        catrow.pack(fill="x", pady=(6, 0))
        tk.Label(catrow, text="Search content", font=self.f_small, fg=MUTED, bg=PANEL,
                 width=20, anchor="w").pack(side="left", padx=(0, 8))
        ttk.Combobox(catrow, textvariable=self.v_category, state="readonly",
                     values=[c[0] for c in CATEGORY_CHOICES], font=self.f_body).pack(
                         side="left", fill="x", expand=True, ipady=2)

        tk.Frame(inner, bg=LINE, height=1).pack(fill="x", pady=(16, 12))
        self._check(inner, "Install AI auto-subtitles (whisper.cpp, ~180 MB download)",
                    self.var_stt)
        self._check(inner, "Install as a service so it starts automatically on login "
                           "(recommended)", self.var_service)

        adv_toggle = tk.Frame(inner, bg=PANEL)
        adv_toggle.pack(fill="x", pady=(14, 0))
        self._adv_btn = tk.Label(adv_toggle,
                                 text="▸  Advanced settings (ports & credentials)",
                                 font=self.f_small, fg=ACCENT, bg=PANEL, cursor="hand2")
        self._adv_btn.pack(anchor="w")
        self._adv_btn.bind("<Button-1>", lambda _: self._toggle_advanced())

        self._adv = tk.Frame(inner, bg=PANEL)
        self._field(self._adv, "Jackett URL", self.v["INDEXER_URL"])
        self._field(self._adv, "qBittorrent URL", self.v["QBIT_URL"])
        self._field(self._adv, "qBittorrent user", self.v["QBIT_USERNAME"])
        self._field(self._adv, "qBittorrent password", self.v["QBIT_PASSWORD"], secret=True)
        self._field(self._adv, "VLC URL", self.v["VLC_URL"])
        self._field(self._adv, "VLC password", self.v["VLC_PASSWORD"], secret=True)
        self._field(self._adv, "Buffer start (MB)", self.v["BUFFER_MIN_MB"])
        self._field(self._adv, "Buffer start (%)", self.v["BUFFER_MIN_PCT"])

        self._warn = tk.Label(inner, text="", font=self.f_small, fg=ERR_RED, bg=PANEL,
                              justify="left", anchor="w", wraplength=680)
        self._warn.pack(anchor="w", pady=(10, 0))

        nav = tk.Frame(p, bg=BG)
        nav.pack(fill="x", pady=(16, 0))
        self._button(nav, "Install  →", self._start_install).pack(side="right")
        self._button(nav, "←  Back",
                     lambda: self._show("welcome", "First-time setup"),
                     primary=False).pack(side="right", padx=(0, 10))

    def _toggle_advanced(self):
        if self.show_advanced.get():
            self._adv.pack_forget()
            self.show_advanced.set(False)
            self._adv_btn.configure(text="▸  Advanced settings (ports & credentials)")
        else:
            self._adv.pack(fill="x", pady=(8, 0))
            self.show_advanced.set(True)
            self._adv_btn.configure(text="▾  Advanced settings (ports & credentials)")

    def _validate(self) -> str | None:
        def port(url, fallback):
            m = re.search(r":(\d+)", url)
            return m.group(1) if m else fallback
        if port(self.v["QBIT_URL"].get(), "8081") == port(self.v["VLC_URL"].get(), "8080"):
            return ("qBittorrent and VLC are set to the same port — give them "
                    "different ports under Advanced settings.")
        for k in ("BUFFER_MIN_MB", "BUFFER_MIN_PCT"):
            try:
                float(self.v[k].get())
            except ValueError:
                return f"{k.replace('_', ' ').title()} must be a number."
        return None

    # ── page: install ─────────────────────────────────────────────────────
    def _build_install(self) -> None:
        p = tk.Frame(self.body, bg=BG)
        self.pages["install"] = p
        tk.Label(p, text="Installing…", font=self.f_h, fg=TXT, bg=BG).pack(anchor="w")
        self._status = tk.Label(p, text="Starting…", font=self.f_small, fg=MUTED, bg=BG)
        self._status.pack(anchor="w", pady=(2, 8))

        self._bar = ttk.Progressbar(p, mode="indeterminate")
        self._bar.pack(fill="x", pady=(0, 10))

        wrap = tk.Frame(p, bg=LINE)
        wrap.pack(fill="both", expand=True)
        self._log = tk.Text(wrap, font=self.f_mono, bg="#0c0c0c", fg="#d6d6d6",
                            relief="flat", bd=0, wrap="word", padx=10, pady=8,
                            state="disabled", insertbackground=TXT)
        sb = tk.Scrollbar(wrap, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        self._log.tag_configure("ok", foreground=OK_GRN)
        self._log.tag_configure("err", foreground=ERR_RED)

        self._nav_install = tk.Frame(p, bg=BG)
        self._nav_install.pack(fill="x", pady=(14, 0))
        self._next_btn = self._button(self._nav_install, "Next  →", self._after_install)

    # ── page: Mullvad (post-install) ──────────────────────────────────────
    def _build_mullvad(self) -> None:
        p = tk.Frame(self.body, bg=BG)
        self.pages["mullvad"] = p
        card = tk.Frame(p, bg=PANEL)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=26, pady=22)

        tk.Label(inner, text="Connect your VPN (Mullvad)", font=self.f_h,
                 fg=TXT, bg=PANEL).pack(anchor="w")
        tk.Label(inner,
                 text=("Mullvad is now installed. StreamLink uses it as a kill-switch: "
                       "if the VPN isn't connected it refuses to download anything. Log "
                       "in and connect once, here:"),
                 font=self.f_body, fg=TXT, bg=PANEL, justify="left", anchor="w",
                 wraplength=680).pack(anchor="w", pady=(8, 12))

        self._steps(inner, [
            "Open the Mullvad VPN app from the Start menu (it was just installed).",
            "Enter your account number and click Log in. No account yet? Get one "
            "at the link below — it's a paid service.",
            "Click Connect. You should see a green “Secure connection”.",
        ])

        links = tk.Frame(inner, bg=PANEL)
        links.pack(anchor="w", pady=(14, 0))
        self._link(links, "Get a Mullvad account →", "https://mullvad.net/en/account").pack(
            side="left")
        if IS_WIN:
            self._button(links, "Open Mullvad", self._open_mullvad,
                         primary=False, padx=12, pady=4).pack(side="left", padx=(16, 0))
        self._mullvad_test_btn = self._button(
            links, "Test VPN connection",
            lambda: self._run_check("check_mullvad()", "Mullvad VPN",
                                    self._mullvad_status, self._mullvad_test_btn),
            primary=False, padx=12, pady=4)
        self._mullvad_test_btn.pack(side="left", padx=(16, 0))

        self._mullvad_status = tk.Label(inner, text="", font=self.f_small, bg=PANEL,
                                        anchor="w", justify="left", wraplength=680)
        self._mullvad_status.pack(anchor="w", pady=(10, 0))

        tk.Label(inner,
                 text="You can do this later too — but searches and downloads won't run "
                      "until Mullvad is connected.",
                 font=self.f_small, fg=MUTED, bg=PANEL, justify="left", anchor="w",
                 wraplength=680).pack(anchor="w", pady=(12, 0))

        nav = tk.Frame(p, bg=BG)
        nav.pack(fill="x", pady=(16, 0))
        self._button(nav, "Next  →",
                     lambda: self._show("qbit", "Step 3 — qBittorrent Web UI")).pack(
                         side="right")

    def _open_mullvad(self) -> None:
        # Best-effort: launch the installed Mullvad GUI; fall back to the website.
        for cmd in (["cmd", "/c", "start", "", "mullvad-gui"],
                    ["cmd", "/c", "start", "", "mullvad vpn"]):
            try:
                subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return
            except Exception:
                continue
        self._open("https://mullvad.net/en/account")

    # ── page: qBittorrent Web UI (post-install) ───────────────────────────
    def _build_qbit(self) -> None:
        p = tk.Frame(self.body, bg=BG)
        self.pages["qbit"] = p
        card = tk.Frame(p, bg=PANEL)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=26, pady=22)

        tk.Label(inner, text="Turn on qBittorrent's Web UI", font=self.f_h,
                 fg=TXT, bg=PANEL).pack(anchor="w")
        tk.Label(inner,
                 text=("StreamLink controls downloads through qBittorrent's Web UI. The "
                       "installer already wrote these settings, but qBittorrent can "
                       "overwrite them if it was open during install — so open it once "
                       "and make sure they match."),
                 font=self.f_body, fg=TXT, bg=PANEL, justify="left", anchor="w",
                 wraplength=680).pack(anchor="w", pady=(8, 12))

        self._steps(inner, [
            "Click “Open qBittorrent” below, then go to Tools → Preferences → Web UI.",
            "Tick “Web User Interface (Remote control)”.",
            "Set the Port, Username and Password to exactly the values shown below.",
            "Tick “Bypass authentication for clients on localhost”, and untick "
            "“Enable Cross-Site Request Forgery (CSRF) protection”.",
            "Under Downloads, set the default save path to the folder shown below. "
            "Click Apply, then restart qBittorrent.",
        ])

        self._infobox(inner, [
            ("Port", self.d_qbit_port),
            ("Username", self.d_qbit_user),
            ("Password", self.d_qbit_pass),
            ("Save path", self.d_qbit_path),
        ])

        ctl = tk.Frame(inner, bg=PANEL)
        ctl.pack(anchor="w", pady=(12, 0))
        self._qbit_status = tk.Label(inner, text="", font=self.f_small, bg=PANEL,
                                     anchor="w", justify="left", wraplength=680)
        self._button(ctl, "Open qBittorrent",
                     lambda: self._launch_app("_QBIT_BIN", "qBittorrent", self._qbit_status),
                     primary=False, padx=12, pady=4).pack(side="left")
        self._qbit_test_btn = self._button(
            ctl, "Test Web UI",
            lambda: self._run_check("start_qbittorrent()", "qBittorrent Web UI",
                                    self._qbit_status, self._qbit_test_btn),
            primary=False, padx=12, pady=4)
        self._qbit_test_btn.pack(side="left", padx=(10, 0))
        self._qbit_status.pack(anchor="w", pady=(8, 0))

        nav = tk.Frame(p, bg=BG)
        nav.pack(fill="x", pady=(16, 0))
        self._button(nav, "Next  →",
                     lambda: self._show("vlc", "Step 4 — Allow VLC remote control")).pack(
                         side="right")
        self._button(nav, "←  Back",
                     lambda: self._show("mullvad", "Step 2 — Connect your VPN"),
                     primary=False).pack(side="left")

    # ── page: VLC web interface (post-install) ────────────────────────────
    def _build_vlc(self) -> None:
        p = tk.Frame(self.body, bg=BG)
        self.pages["vlc"] = p
        card = tk.Frame(p, bg=PANEL)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=26, pady=22)

        tk.Label(inner, text="Allow VLC remote control", font=self.f_h,
                 fg=TXT, bg=PANEL).pack(anchor="w")
        tk.Label(inner,
                 text=("StreamLink plays media by sending it to VLC's web (Lua HTTP) "
                       "control. StreamLink launches VLC with the right port and password "
                       "automatically — but on a brand-new VLC you must dismiss its "
                       "one-time privacy dialog first, or the web control stays blocked."),
                 font=self.f_body, fg=TXT, bg=PANEL, justify="left", anchor="w",
                 wraplength=680).pack(anchor="w", pady=(8, 12))

        self._steps(inner, [
            "Click “Open VLC” below.",
            "If VLC shows a Privacy / Network Access dialog on first run, click "
            "Continue / OK to dismiss it.",
            "Close VLC again — StreamLink will relaunch it with the web control on the "
            "port and password shown below.",
        ])

        self._infobox(inner, [
            ("Web port", self.d_vlc_port),
            ("Password", self.d_vlc_pass),
        ])

        ctl = tk.Frame(inner, bg=PANEL)
        ctl.pack(anchor="w", pady=(12, 0))
        self._vlc_status = tk.Label(inner, text="", font=self.f_small, bg=PANEL,
                                    anchor="w", justify="left", wraplength=680)
        self._button(ctl, "Open VLC",
                     lambda: self._launch_app("_VLC_BIN", "VLC", self._vlc_status),
                     primary=False, padx=12, pady=4).pack(side="left")
        self._vlc_test_btn = self._button(
            ctl, "Test web control",
            lambda: self._run_check("start_vlc()", "VLC web control",
                                    self._vlc_status, self._vlc_test_btn),
            primary=False, padx=12, pady=4)
        self._vlc_test_btn.pack(side="left", padx=(10, 0))
        self._vlc_status.pack(anchor="w", pady=(8, 0))
        tk.Label(inner,
                 text="“Test web control” opens VLC (fullscreen) with the right settings "
                      "and confirms StreamLink can reach it — dismiss VLC's first-run "
                      "dialog if it appears, then test again.",
                 font=self.f_small, fg=MUTED, bg=PANEL, justify="left", anchor="w",
                 wraplength=680).pack(anchor="w", pady=(6, 0))

        nav = tk.Frame(p, bg=BG)
        nav.pack(fill="x", pady=(16, 0))
        self._button(nav, "Next  →",
                     lambda: self._show("jackett", "Step 5 — Add a search source")).pack(
                         side="right")
        self._button(nav, "←  Back",
                     lambda: self._show("qbit", "Step 3 — qBittorrent Web UI"),
                     primary=False).pack(side="left")

    # ── page: Jackett (post-install, collects the API key) ────────────────
    def _build_jackett(self) -> None:
        p = tk.Frame(self.body, bg=BG)
        self.pages["jackett"] = p
        card = tk.Frame(p, bg=PANEL)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=26, pady=22)

        tk.Label(inner, text="Add a search source (Jackett)", font=self.f_h,
                 fg=TXT, bg=PANEL).pack(anchor="w")
        tk.Label(inner,
                 text=("Jackett is now installed and running. It's where StreamLink "
                       "searches. Add at least one indexer, then copy Jackett's API key "
                       "and paste it below."),
                 font=self.f_body, fg=TXT, bg=PANEL, justify="left", anchor="w",
                 wraplength=680).pack(anchor="w", pady=(8, 12))

        self._steps(inner, [
            "Click “Open Jackett” below (opens its dashboard in your browser).",
            "Click “+ Add indexer”, search for the sources you want, and add them.",
            "Copy the API Key shown at the top-right of the Jackett dashboard.",
            "Paste it into the API key box below.",
        ])

        links = tk.Frame(inner, bg=PANEL)
        links.pack(anchor="w", pady=(12, 4))
        self._button(links, "Open Jackett",
                     lambda: self._open(self.v["INDEXER_URL"].get()),
                     primary=False, padx=12, pady=4).pack(side="left")
        self._jk_test_btn = self._button(
            links, "Test Jackett",
            lambda: self._run_check("start_jackett()", "Jackett",
                                    self._jk_status, self._jk_test_btn),
            primary=False, padx=12, pady=4)
        self._jk_test_btn.pack(side="left", padx=(10, 0))
        self._link(links, "Jackett help →",
                   "https://github.com/Jackett/Jackett#configuration").pack(
                       side="left", padx=(16, 0))

        tk.Frame(inner, bg=LINE, height=1).pack(fill="x", pady=(12, 6))
        self._field(inner, "Jackett API key", self.v["INDEXER_API_KEY"])
        self._field(inner, "Jackett admin password", self.v["JACKETT_PASSWORD"],
                    secret=True)
        tk.Label(inner,
                 text="The admin password is only needed if you set one inside Jackett "
                      "itself — most people leave it blank.",
                 font=self.f_small, fg=MUTED, bg=PANEL, justify="left", anchor="w",
                 wraplength=680).pack(anchor="w", pady=(6, 0))

        self._jk_status = tk.Label(inner, text="", font=self.f_small, fg=OK_GRN,
                                   bg=PANEL, anchor="w")
        self._jk_status.pack(anchor="w", pady=(8, 0))

        nav = tk.Frame(p, bg=BG)
        nav.pack(fill="x", pady=(16, 0))
        self._button(nav, "Save & continue  →", self._save_jackett).pack(side="right")
        self._button(nav, "Skip for now",
                     lambda: self._show("finish", "Done"),
                     primary=False).pack(side="right", padx=(0, 10))
        self._button(nav, "←  Back",
                     lambda: self._show("vlc", "Step 4 — Allow VLC remote control"),
                     primary=False).pack(side="left")

    def _save_jackett(self) -> None:
        updates = {}
        api = self.v["INDEXER_API_KEY"].get().strip()
        pw = self.v["JACKETT_PASSWORD"].get().strip()
        if api:
            updates["INDEXER_API_KEY"] = api
        if pw:
            updates["JACKETT_PASSWORD"] = pw
        if updates:
            try:
                self._set_env_keys(updates)
            except Exception as exc:
                self._jk_status.configure(text=f"Couldn't write .env: {exc}", fg=ERR_RED)
                return
        self._show("finish", "Done")

    def _set_env_keys(self, updates: dict) -> None:
        """Replace (or append) KEY=value lines in .env, preserving the rest."""
        lines = []
        if ENV_PATH.exists():
            lines = ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        remaining = dict(updates)
        out = []
        for line in lines:
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k = s.split("=", 1)[0].strip()
                if k in remaining:
                    out.append(f"{k}={remaining.pop(k)}")
                    continue
            out.append(line)
        for k, val in remaining.items():
            out.append(f"{k}={val}")
        ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")

    # ── page: finish ──────────────────────────────────────────────────────
    def _build_finish(self) -> None:
        p = tk.Frame(self.body, bg=BG)
        self.pages["finish"] = p
        card = tk.Frame(p, bg=PANEL)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=26, pady=24)

        self._finish_title = tk.Label(inner, text="All done!", font=self.f_h,
                                      fg=OK_GRN, bg=PANEL)
        self._finish_title.pack(anchor="w")
        self._finish_msg = tk.Label(
            inner, font=self.f_body, fg=TXT, bg=PANEL, justify="left", anchor="w",
            wraplength=680,
            text=(
                "StreamLink is installed and configured.\n\n"
                "Open the dashboard once it's running:\n"
                "   •  http://localhost           (this PC)\n"
                "   •  http://<this-pc-ip>         (phones/TVs on your network)\n"
                "Admin panel:  https://localhost/admin\n\n"
                "Launching now starts VLC, qBittorrent, Jackett and the dashboard. "
                "If you skipped a step, you can redo it any time."
            ))
        self._finish_msg.pack(anchor="w", pady=(12, 16))

        self._launch_cb = self._check(inner, "Launch StreamLink now", self.var_launch)

        nav = tk.Frame(p, bg=BG)
        nav.pack(fill="x", pady=(16, 0))
        self._button(nav, "Finish", self._finish).pack(side="right")

    def _update_finish(self) -> None:
        """Tailor the Finish page to whether StreamLink was installed as a service.

        When the boot service is installed, `daemon.install()` already started it
        during setup (it's serving the dashboard now), so a separate "Launch now"
        would just hit the single-instance guard — hide it, and point out the
        auto-login requirement for surviving a full reboot.
        """
        if self.var_service.get():
            self.var_launch.set(False)
            self._launch_cb.pack_forget()
            self._finish_msg.configure(text=(
                "StreamLink is installed as a system service — it started during "
                "setup and will start automatically every time you log in.\n\n"
                "Open the dashboard:\n"
                "   •  http://localhost           (this PC)\n"
                "   •  http://<this-pc-ip>         (phones/TVs on your network)\n"
                "Admin panel:  https://localhost/admin\n\n"
                "Tip: to have it come back after a full reboot (not just login), turn "
                "on Windows auto-login — see README.md → “Unattended restarts”."))
        else:
            self.var_launch.set(True)
            self._launch_cb.pack(fill="x", pady=2)
            self._finish_msg.configure(text=(
                "StreamLink is installed and configured. You chose not to start it on "
                "boot, so launch it yourself with “Launch StreamLink now” (or run "
                "`python run.py`), or install the service later with "
                "`python run.py --install`.\n\n"
                "Open the dashboard once it's running:\n"
                "   •  http://localhost           (this PC)\n"
                "   •  http://<this-pc-ip>         (phones/TVs on your network)\n"
                "Admin panel:  https://localhost/admin"))

    # ── install orchestration ─────────────────────────────────────────────
    def _start_install(self) -> None:
        err = self._validate()
        if err:
            self._warn.configure(text=err)
            return
        self._warn.configure(text="")
        self._show("install", "Step 2 — Installing")
        self._next_btn.pack_forget()
        self._bar.start(12)
        self._append("Launching setup…\n")
        threading.Thread(target=self._run_setup, daemon=True).start()
        self.after(80, self._drain_log)

    def _build_env(self) -> dict:
        env = dict(os.environ)
        env["STREAMLINK_WIZARD"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["STREAMLINK_INSTALL_STT"] = "1" if self.var_stt.get() else "0"
        env["STREAMLINK_INSTALL_SERVICE"] = "1" if self.var_service.get() else "0"
        for label, code in CATEGORY_CHOICES:
            if label == self.v_category.get():
                self.v["INDEXER_CATEGORIES"].set(code)
                break
        for key in INSTALL_KEYS:
            env["SL_" + key] = self.v[key].get()
        return env

    def _run_setup(self) -> None:
        kwargs: dict = {}
        if IS_WIN:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(
                [sys.executable, str(HERE / "setup.py")],
                cwd=str(HERE), env=self._build_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, **kwargs,
            )
        except Exception as exc:
            self._log_q.put(f"\n[ERROR] Could not start setup.py: {exc}\n")
            self._log_q.put("__DONE__1")
            return
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._log_q.put(_ANSI.sub("", line))
        rc = self._proc.wait()
        self._log_q.put(f"__DONE__{rc}")

    def _drain_log(self) -> None:
        done_rc = None
        try:
            while True:
                item = self._log_q.get_nowait()
                if item.startswith("__DONE__"):
                    done_rc = int(item[len("__DONE__"):] or "0")
                    break
                self._append(item)
        except queue.Empty:
            pass
        if done_rc is None:
            self.after(80, self._drain_log)
        else:
            self._on_done(done_rc)

    def _append(self, text: str) -> None:
        tag = ()
        low = text.lower()
        if text.strip().startswith("✓") or " ✓ " in text:
            tag = ("ok",)
        elif "[error]" in low or text.strip().startswith("✗"):
            tag = ("err",)
        self._log.configure(state="normal")
        self._log.insert("end", text, tag)
        self._log.see("end")
        self._log.configure(state="disabled")

    def _on_done(self, rc: int) -> None:
        self._bar.stop()
        self._bar.configure(mode="determinate", value=100)
        self._install_ok = rc == 0
        if rc == 0:
            self._status.configure(text="Setup complete.", fg=OK_GRN)
            self._append("\n✓ Setup finished — now let's connect your VPN and a source.\n")
        else:
            self._status.configure(text=f"Setup exited with code {rc}.", fg=ERR_RED)
            self._append(f"\n[ERROR] Setup exited with code {rc}. Review the log above.\n")
        self._next_btn.pack(side="right")

    def _after_install(self) -> None:
        if getattr(self, "_install_ok", False):
            self._refresh_match_values()
            self._update_finish()
            self._show("mullvad", "Step 2 — Connect your VPN")
        else:
            self._finish_title.configure(text="Setup didn't finish cleanly", fg=ERR_RED)
            self._finish_msg.configure(
                text=("setup.py exited with an error (see the install log). Close this "
                      "window and run `python setup.py` in a terminal to see the full "
                      "output, then `python run.py` to start."))
            self.var_launch.set(False)
            self._show("finish", "Setup incomplete")

    def _finish(self) -> None:
        if self.var_launch.get():
            try:
                kwargs: dict = {}
                if IS_WIN:
                    kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
                subprocess.Popen([sys.executable, str(HERE / "run.py")],
                                 cwd=str(HERE), **kwargs)
            except Exception as exc:
                self._append(f"[ERROR] Could not launch run.py: {exc}\n")
        self.destroy()


def main() -> None:
    if sys.version_info < (3, 9):
        print("Python 3.9+ is required. Re-run install.bat to install it.")
        sys.exit(1)
    Wizard().mainloop()


if __name__ == "__main__":
    main()
