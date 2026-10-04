#!/usr/bin/env python3
"""StreamLink graphical first-install wizard.

Started by ``install.bat``, which finds or installs a Python that is safe to
build the venv from. This file is only the window: every decision it makes
lives in ``installsteps.py`` (tested in ``tests/test_installsteps.py``), and
every install step is still ``setup.py``, driven through its env-var seam.

Order, and why
--------------
Welcome → Settings → Install → (VPN) → qBittorrent → VLC → Jackett → TMDb → Finish

* **Settings come first** because two of them change what gets installed: the
  VPN choice decides whether Mullvad is installed at all.
* **The steps that need a person come after the install**, when the apps they
  are about exist. Each one *checks first* and only shows instructions when the
  check fails, so a step that already works is one click.
* **StreamLink itself starts last**, on Finish. The server reads ``.env`` once
  at startup; started any earlier it would never see the Jackett or TMDb key,
  and its watchdog would kill a qBittorrent the user had been told to open.

Runs under the **system** Python, standard library only, like ``setup.py``.
See docs/INSTALLER.md.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, ttk
except Exception as exc:  # pragma: no cover - surfaced through install.bat
    print("Tkinter is not available in this Python install.")
    print("Install Python from python.org (it bundles Tk) and run install.bat again.")
    print(f"Detail: {exc}")
    sys.exit(1)

import installsteps as steps

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
LOG_PATH = HERE / "logs" / "installer.log"
IS_WIN = os.name == "nt"

# Metro-flat palette, matching the dashboard.
BG, PANEL, FIELD, LINE = "#141414", "#1d1d1d", "#262626", "#333333"
TXT, MUTED = "#f2f2f2", "#9a9a9a"
ACCENT, ACCENT2 = "#2f7fed", "#256ad1"
OK_GRN, ERR_RED, AMBER = "#3ec46d", "#e0564f", "#e0a44f"
# "warn" shows the page's how-to; "note" is amber without it (nothing to fix here).
STATE_COLOR = {"ok": OK_GRN, "fail": ERR_RED, "warn": AMBER, "note": AMBER,
               "busy": AMBER, "idle": MUTED}

CATEGORY_CHOICES = [("All content", "0"), ("Movies only", "2000"), ("TV only", "5000")]
VPN_CHOICES = [
    ("mullvad", "Mullvad",
     "Installs Mullvad. Downloads run only while it is connected."),
    ("generic", "Another VPN",
     "Any VPN app. Downloads run only while a VPN connection is up."),
    ("off", "No kill switch",
     "Downloads always run. Only if you are protected another way, "
     "such as a VPN on your router."),
]


def shell_open(target: str) -> bool:
    """Open a URL, a folder or an app the way a double-click would.

    On Windows this goes through Explorer on purpose: the wizard is elevated,
    and anything it starts directly inherits that. An elevated VLC is one the
    unelevated StreamLink service cannot restart; Explorer starts it as the user.
    """
    try:
        if IS_WIN:
            subprocess.Popen(["explorer.exe", target])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return True
    except OSError:
        return False


class Wizard(tk.Tk):
    def __init__(self) -> None:
        if IS_WIN:
            # Without this Windows bitmap-stretches the window on a scaled
            # display (a 4K TV at 200-300%), and everything is a blur.
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        super().__init__()
        self.title("StreamLink Installer")
        self.configure(bg=BG)
        self.scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        w = min(self.px(900), self.winfo_screenwidth() - self.px(40))
        h = min(self.px(780), self.winfo_screenheight() - self.px(90))
        self.geometry(f"{w}x{h}")
        self.minsize(min(w, self.px(760)), min(h, self.px(600)))
        self.wrap = w - self.px(120)

        self._fonts()
        self._state()

        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=self.px(28), pady=(self.px(20), self.px(8)))
        tk.Label(head, text="STREAMLINK", font=self.f_title, fg=TXT, bg=BG).pack(side="left")
        self._tag = tk.Label(head, text="", font=self.f_small, fg=MUTED, bg=BG)
        self._tag.pack(side="right", anchor="s")
        tk.Frame(self, bg=LINE, height=1).pack(fill="x", padx=self.px(28))

        # Nav is packed before the body so a tall page can never push it off.
        self._navbar = tk.Frame(self, bg=BG)
        self._navbar.pack(side="bottom", fill="x", padx=self.px(28), pady=self.px(16))
        self._btn_next = self._button(self._navbar, "Next", lambda: None)
        self._btn_next.pack(side="right")
        self._btn_back = self._button(self._navbar, "Back", lambda: None, primary=False)
        self._btn_extra = self._button(self._navbar, "", lambda: None, primary=False)

        # The page area scrolls when a page is taller than the window. Text
        # height differs per machine (font, display scaling, a 768-line
        # laptop), so no fixed window size can promise that every page fits.
        mid = tk.Frame(self, bg=PANEL)
        mid.pack(fill="both", expand=True, padx=self.px(28), pady=(self.px(16), 0))
        self._vsb = tk.Scrollbar(mid, orient="vertical")
        self._canvas = tk.Canvas(mid, bg=PANEL, highlightthickness=0, bd=0,
                                 yscrollcommand=self._vsb.set)
        self._vsb.configure(command=self._canvas.yview)
        self._canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self._canvas, bg=PANEL)
        self._body_id = self._canvas.create_window(0, 0, window=self.body, anchor="nw")
        self._canvas.bind("<Configure>", lambda _: self._fit())
        self.bind_all("<MouseWheel>", self._wheel)

        self.pages: dict[str, tk.Frame] = {}
        self.enter: dict[str, object] = {}
        self.checks: dict[str, dict] = {}
        self._build_welcome()
        self._build_settings()
        self._build_advanced()
        self._build_install()
        self._build_vpn()
        self._build_qbit()
        self._build_vlc()
        self._build_jackett()
        self._build_tmdb()
        self._build_finish()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, self._pump)
        self._show("welcome")

    # ── plumbing ──────────────────────────────────────────────────────────
    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    def _fit(self) -> None:
        """Size the page to the window, or let it scroll if it needs more."""
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        page = self.pages.get(self.current)
        need = page.winfo_reqheight() if page else 0
        h = max(ch, need)
        self._canvas.itemconfigure(self._body_id, width=cw, height=h)
        self._canvas.configure(scrollregion=(0, 0, cw, h))
        overflow = need > ch + 2 and ch > 1
        if overflow and not self._vsb.winfo_ismapped():
            self._vsb.pack(side="right", fill="y")
        elif not overflow and self._vsb.winfo_ismapped():
            self._vsb.pack_forget()

    def _wheel(self, event) -> None:
        if self._vsb.winfo_ismapped() and event.widget is not self._log:
            self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _fonts(self) -> None:
        base = "Segoe UI" if IS_WIN else "Helvetica"
        self.f_title = tkfont.Font(family=base, size=20, weight="bold")
        self.f_h     = tkfont.Font(family=base, size=15, weight="bold")
        self.f_body  = tkfont.Font(family=base, size=11)
        self.f_bold  = tkfont.Font(family=base, size=11, weight="bold")
        self.f_small = tkfont.Font(family=base, size=10)
        self.f_mono  = tkfont.Font(family="Consolas" if IS_WIN else "Menlo", size=10)

    def _state(self) -> None:
        self.existing = steps.read_env(ENV_PATH)
        self.was_serving = steps.port_open(80)
        self.v = {k: tk.StringVar(value=self.existing.get(k, d))
                  for k, d in steps.DEFAULTS.items()}
        self.v_tmdb = tk.StringVar(value=self.existing.get("TMDB_API_KEY", ""))
        cat = self.v["INDEXER_CATEGORIES"].get()
        self.v_category = tk.StringVar(
            value=next((lbl for lbl, code in CATEGORY_CHOICES if code == cat), cat))
        mode = "mullvad"
        try:
            import vpncheck
            mode = vpncheck.vpn_mode()
        except Exception:
            pass
        self.v_vpn = tk.StringVar(value=mode)
        self.v_service = tk.BooleanVar(value=True)
        self.v_showpw = tk.BooleanVar(value=False)
        self.flow: list[str] = []
        self.current = ""
        self._proc: subprocess.Popen | None = None
        self._installing = False
        self._ui_q: "queue.Queue" = queue.Queue()

    def _pump(self) -> None:
        """Run what worker threads queued. Tk is only ever touched from here."""
        try:
            while True:
                fn = self._ui_q.get_nowait()
                try:
                    fn()
                except tk.TclError:
                    pass
        except queue.Empty:
            pass
        self.after(60, self._pump)

    def _ui(self, fn) -> None:
        self._ui_q.put(fn)

    def _bg(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _button(self, parent, text, cmd, primary=True):
        bg = ACCENT if primary else FIELD
        hov = ACCENT2 if primary else LINE
        b = tk.Button(parent, text=text.upper(), command=cmd, font=self.f_bold, bg=bg,
                      fg="#ffffff" if primary else TXT, activebackground=hov,
                      activeforeground="#ffffff", disabledforeground=MUTED,
                      relief="flat", bd=0, cursor="hand2",
                      padx=self.px(18), pady=self.px(8))
        b.bind("<Enter>", lambda _: b["state"] != "disabled" and b.configure(bg=hov))
        b.bind("<Leave>", lambda _: b.configure(bg=bg))
        return b

    def _page(self, name: str, title: str, intro: str = "") -> tk.Frame:
        outer = tk.Frame(self.body, bg=PANEL)
        self.pages[name] = outer
        p = tk.Frame(outer, bg=PANEL)
        p.pack(fill="both", expand=True, padx=self.px(26), pady=self.px(20))
        tk.Label(p, text=title.upper(), font=self.f_h, fg=TXT, bg=PANEL).pack(anchor="w")
        if intro:
            self._text(p, intro).pack(anchor="w", pady=(self.px(6), self.px(10)))
        return p

    def _text(self, parent, text, color=TXT, font=None):
        return tk.Label(parent, text=text, font=font or self.f_body, fg=color, bg=PANEL,
                        justify="left", anchor="w", wraplength=self.wrap)

    def _field(self, parent, label, var, secret=False, browse=False):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=(self.px(5), 0))
        tk.Label(row, text=label, font=self.f_small, fg=MUTED, bg=PANEL,
                 width=22, anchor="w").pack(side="left")
        e = tk.Entry(row, textvariable=var, font=self.f_body, bg=FIELD, fg=TXT,
                     insertbackground=TXT, relief="flat", bd=0, show="•" if secret else "")
        e.pack(side="left", fill="x", expand=True, ipady=self.px(5), ipadx=self.px(6))
        if browse:
            b = self._button(row, "Browse", lambda: self._browse(var), primary=False)
            b.configure(padx=self.px(10), pady=self.px(3))
            b.pack(side="left", padx=(self.px(8), 0))
        return e

    def _browse(self, var) -> None:
        d = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if d:
            var.set(os.path.normpath(d))

    def _steps(self, parent, items) -> None:
        for i, txt in enumerate(items, 1):
            row = tk.Frame(parent, bg=PANEL)
            row.pack(fill="x", pady=self.px(2))
            tk.Label(row, text=str(i), font=self.f_bold, fg="#ffffff", bg=ACCENT,
                     width=3).pack(side="left", anchor="n", padx=(0, self.px(10)))
            tk.Label(row, text=txt, font=self.f_body, fg=TXT, bg=PANEL, justify="left",
                     anchor="w", wraplength=self.wrap - self.px(50)).pack(side="left")

    def _infobox(self, parent, rows) -> None:
        box = tk.Frame(parent, bg=FIELD)
        box.pack(fill="x", pady=(self.px(8), 0))
        for label, var in rows:
            r = tk.Frame(box, bg=FIELD)
            r.pack(fill="x", padx=self.px(12), pady=self.px(3))
            tk.Label(r, text=label, font=self.f_small, fg=MUTED, bg=FIELD,
                     width=14, anchor="w").pack(side="left")
            tk.Label(r, textvariable=var, font=self.f_mono, fg=TXT, bg=FIELD,
                     anchor="w").pack(side="left")

    def _nav(self, back=None, next_text="Next", next_cmd=None, extra=None) -> None:
        self._btn_back.pack_forget()
        self._btn_extra.pack_forget()
        if next_cmd is None:
            self._btn_next.pack_forget()
        else:
            self._btn_next.configure(text=next_text.upper(), command=next_cmd, state="normal")
            self._btn_next.pack(side="right")
        if extra:
            self._btn_extra.configure(text=extra[0].upper(), command=extra[1], state="normal")
            self._btn_extra.pack(side="right", padx=(0, self.px(10)))
        if back:
            self._btn_back.configure(command=back, state="normal")
            self._btn_back.pack(side="left")

    def _show(self, name: str) -> None:
        for f in self.pages.values():
            f.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        self.current = name
        self._canvas.yview_moveto(0)
        self.after_idle(self._fit)
        if name in self.flow and name != "finish":
            self._tag.configure(
                text=f"STEP {self.flow.index(name) + 3} OF {len(self.flow) + 1}")
        else:
            self._tag.configure(text={"settings": "STEP 1", "advanced": "STEP 1",
                                      "install": "STEP 2"}.get(name, ""))
        fn = self.enter.get(name)
        if fn:
            fn()

    def _go(self, delta: int) -> None:
        i = self.flow.index(self.current) + delta
        if 0 <= i < len(self.flow):
            self._show(self.flow[i])

    def _on_close(self) -> None:
        if self._installing:
            if not messagebox.askyesno(
                    "StreamLink Installer",
                    "The install is still running. Stop it and close?"):
                return
            self._kill_proc()
        self.destroy()

    def _kill_proc(self) -> None:
        p = self._proc
        if not p or p.poll() is not None:
            return
        try:
            if IS_WIN:      # /T: setup.py's own children (winget, pip) go too
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                p.kill()
        except OSError:
            pass

    # ── welcome ───────────────────────────────────────────────────────────
    def _build_welcome(self) -> None:
        p = self._page("welcome", "Welcome")
        blocker = ""
        me = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        desk = steps.session_user()
        if steps.wrong_account(me, desk):
            blocker = (
                f"This installer is running as “{me}”, but “{desk}” is the account signed "
                "in to Windows. That happens when the administrator prompt is approved "
                "with a different account's password, and it would put StreamLink's "
                "settings in the wrong account.\n\n"
                f"Sign in to Windows as an administrator (or make “{desk}” one), then "
                "run install.bat again.")
        elif IS_WIN and steps.is_per_user_python(sys.executable):
            blocker = (
                "This is a per-user copy of Python, which StreamLink's background "
                f"service can't use:\n{sys.executable}\n\n"
                "Close this window and double-click install.bat instead. It installs "
                "a copy of Python that works.")
        if blocker:
            self._text(p, blocker, color=ERR_RED).pack(anchor="w", pady=(self.px(12), 0))
            self.enter["welcome"] = lambda: self._nav(next_text="Close", next_cmd=self.destroy)
            return

        self._text(p, (
            "This sets up StreamLink on this PC and then checks, one at a time, the "
            "few things that need you.\n\n"
            "It installs on its own:\n"
            "   •  VLC, qBittorrent and Jackett (and Mullvad, if that is your VPN)\n"
            "   •  ffmpeg and the Smart Skip tools\n"
            "   •  StreamLink's own Python packages, in a private folder\n"
            "   •  the HTTPS certificate for the admin panel\n\n"
            "Close qBittorrent first if it is open. Allow about ten minutes, most of "
            "it downloading."
        )).pack(anchor="w", pady=(self.px(10), 0))
        if self.was_serving:
            self._text(p, (
                "StreamLink is already running on this PC. You can go on to repair or "
                "change the install; your current settings are filled in, and changes "
                "apply the next time StreamLink starts."), color=AMBER).pack(
                    anchor="w", pady=(self.px(14), 0))
        if not (HERE / ".git").exists():
            self._text(p, (
                "This folder came from a ZIP download. The installer will connect it "
                "to GitHub so StreamLink can update itself later."), color=MUTED,
                font=self.f_small).pack(anchor="w", pady=(self.px(14), 0))
        self._text(p, f"Python {sys.version.split()[0]}  ·  {sys.executable}",
                   color=MUTED, font=self.f_small).pack(anchor="w", side="bottom")
        self.enter["welcome"] = lambda: self._nav(
            next_text="Get started", next_cmd=lambda: self._show("settings"))

    # ── settings ──────────────────────────────────────────────────────────
    def _build_settings(self) -> None:
        p = self._page("settings", "Settings",
                       "Everything here can be changed later in StreamLink's admin panel.")
        self._field(p, "Download folder", self.v["QBIT_DOWNLOAD_PATH"], browse=True)

        self._pw_entry = self._field(p, "Admin password", self.v["ADMIN_PASSWORD"], secret=True)
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x")
        tk.Label(row, text="", bg=PANEL, font=self.f_small, width=22).pack(side="left")
        tk.Checkbutton(row, text="Show", variable=self.v_showpw, font=self.f_small, fg=MUTED,
                       bg=PANEL, activebackground=PANEL, activeforeground=TXT,
                       selectcolor=FIELD, bd=0, highlightthickness=0,
                       command=lambda: self._pw_entry.configure(
                           show="" if self.v_showpw.get() else "•")).pack(side="left")
        tk.Label(row, text="Protects the settings panel. You choose it; there is no default.",
                 font=self.f_small, fg=MUTED, bg=PANEL).pack(side="left", padx=(self.px(8), 0))

        tk.Frame(p, bg=LINE, height=1).pack(fill="x", pady=(self.px(12), self.px(8)))
        tk.Label(p, text="VPN KILL SWITCH", font=self.f_bold, fg=TXT, bg=PANEL).pack(anchor="w")
        locked = self.was_serving and (HERE / "library.json").exists()
        for mode, label, desc in VPN_CHOICES:
            r = tk.Frame(p, bg=PANEL)
            r.pack(fill="x", pady=(self.px(3), 0))
            tk.Radiobutton(r, text=label, value=mode, variable=self.v_vpn, font=self.f_bold,
                           fg=TXT, bg=PANEL, activebackground=PANEL, activeforeground=TXT,
                           selectcolor=FIELD, bd=0, highlightthickness=0, width=14, anchor="w",
                           state="disabled" if locked else "normal").pack(side="left", anchor="n")
            tk.Label(r, text=desc, font=self.f_small, fg=MUTED, bg=PANEL, justify="left",
                     anchor="w", wraplength=self.wrap - self.px(190)).pack(side="left")
        if locked:
            self._text(p, "StreamLink is running, so this is changed in Admin → VPN Kill "
                          "Switch instead.", color=MUTED, font=self.f_small).pack(anchor="w")

        tk.Frame(p, bg=LINE, height=1).pack(fill="x", pady=(self.px(12), self.px(8)))
        tk.Checkbutton(p, text="Start StreamLink automatically when I sign in to Windows"
                               if IS_WIN else "Start StreamLink automatically at login",
                       variable=self.v_service, font=self.f_body, fg=TXT, bg=PANEL,
                       activebackground=PANEL, activeforeground=TXT, selectcolor=FIELD,
                       bd=0, highlightthickness=0, anchor="w").pack(fill="x")

        self._warn = self._text(p, "", color=ERR_RED, font=self.f_small)
        self._warn.pack(anchor="w", pady=(self.px(8), 0))
        self.enter["settings"] = lambda: self._nav(
            back=lambda: self._show("welcome"), next_text="Install",
            next_cmd=self._start_install,
            extra=("Ports & passwords", lambda: self._show("advanced")))

    def _build_advanced(self) -> None:
        p = self._page("advanced", "Ports & passwords",
                       "The defaults work. Change these only if a port is already taken "
                       "on this PC, or Jackett or qBittorrent runs on another machine.")
        self._field(p, "Jackett address", self.v["INDEXER_URL"])
        self._field(p, "qBittorrent address", self.v["QBIT_URL"])
        self._field(p, "qBittorrent username", self.v["QBIT_USERNAME"])
        self._field(p, "qBittorrent password", self.v["QBIT_PASSWORD"])
        self._field(p, "VLC address", self.v["VLC_URL"])
        self._field(p, "VLC password", self.v["VLC_PASSWORD"])
        self._field(p, "Start playing after (MB)", self.v["BUFFER_MIN_MB"])
        self._field(p, "…or after (%)", self.v["BUFFER_MIN_PCT"])
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", pady=(self.px(5), 0))
        tk.Label(row, text="Search for", font=self.f_small, fg=MUTED, bg=PANEL,
                 width=22, anchor="w").pack(side="left")
        ttk.Combobox(row, textvariable=self.v_category, state="readonly", font=self.f_body,
                     values=[c[0] for c in CATEGORY_CHOICES]).pack(side="left", fill="x",
                                                                   expand=True)
        self.enter["advanced"] = lambda: self._nav(
            next_text="Done", next_cmd=lambda: self._show("settings"))

    def _values(self) -> dict:
        vals = {k: var.get().strip() if k != "ADMIN_PASSWORD" else var.get()
                for k, var in self.v.items()}
        vals["INDEXER_CATEGORIES"] = next(
            (code for lbl, code in CATEGORY_CHOICES if lbl == self.v_category.get()),
            self.v_category.get() or "0")
        return vals

    # ── install ───────────────────────────────────────────────────────────
    def _build_install(self) -> None:
        p = self._page("install", "Installing")
        self._inst_status = self._text(p, "Starting…", color=MUTED, font=self.f_small)
        self._inst_status.pack(anchor="w", pady=(self.px(2), self.px(8)))
        self._bar = ttk.Progressbar(p, mode="indeterminate")
        self._bar.pack(fill="x", pady=(0, self.px(10)))
        wrap = tk.Frame(p, bg=LINE)
        wrap.pack(fill="both", expand=True)
        self._log = tk.Text(wrap, font=self.f_mono, bg="#0c0c0c", fg="#d6d6d6", relief="flat",
                            bd=0, wrap="word", padx=self.px(10), pady=self.px(8),
                            state="disabled", height=6)
        sb = tk.Scrollbar(wrap, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        self._log.tag_configure("ok", foreground=OK_GRN)
        self._log.tag_configure("warn", foreground=AMBER)
        self._log.tag_configure("err", foreground=ERR_RED)

    def _log_line(self, text: str) -> None:
        """Queue a line for the install log. Safe from any thread."""
        try:
            LOG_PATH.parent.mkdir(exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass
        self._ui(lambda: self._append(text))

    def _append(self, text: str) -> None:
        s = text.strip()
        tag = ("ok",) if s.startswith("✓") else ("warn",) if s.startswith("⚠") \
            else ("err",) if s.startswith("✗") or s.startswith("[ERROR]") else ()
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n", tag)
        self._log.see("end")
        self._log.configure(state="disabled")

    def _start_install(self) -> None:
        vals = self._values()
        err = steps.validate_settings(vals)
        self._warn.configure(text=err or "")
        if err:
            return
        self._show("install")
        self._nav()
        self._installing = True
        self._inst_status.configure(text="This takes a few minutes. Leave this window open.",
                                    fg=MUTED)
        self._bar.configure(mode="indeterminate")
        self._bar.start(12)
        self._bg(lambda: self._run_install(vals, self.v_vpn.get()))

    def _stream(self, cmd: list, env: dict) -> int:
        """Run `cmd`, copying its output to the log line by line."""
        kw: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
        try:
            # UTF-8 with errors=replace, never the locale default: see
            # installsteps.child_env for the hang this prevents.
            self._proc = subprocess.Popen(
                cmd, cwd=str(HERE), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", bufsize=1, **kw)
        except OSError as exc:
            self._log_line(f"[ERROR] Could not start {Path(str(cmd[0])).name}: {exc}")
            return 1
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = steps.clean_line(raw)
            if line or not raw.strip():
                self._log_line(line)
        return self._proc.wait()

    def _run_install(self, vals: dict, vpn_mode: str) -> None:
        self._log_line(f"── StreamLink installer, Python {sys.version.split()[0]} ──")
        try:
            self._connect_updates()
        except Exception as exc:           # never let this step cost the install
            self._log_line(f"Could not set up updates: {exc}")
        rc = self._stream([sys.executable, str(HERE / "setup.py")],
                          steps.setup_env(vals, vpn_mode))
        self._ui(lambda: self._install_done(rc, vpn_mode))

    def _connect_updates(self) -> None:
        if (HERE / ".git").exists():
            return
        git = steps.find_git()
        if not git and IS_WIN:
            import shutil
            winget = shutil.which("winget")
            if winget:
                self._log_line("Installing Git (StreamLink updates itself through it) …")
                self._stream([winget, *steps.WINGET_GIT], steps.child_env())
                git = steps.find_git()
        if not git:
            self._log_line("Git is not installed, so StreamLink won't be able to update "
                           "itself. Everything else works.")
            return
        steps.ensure_clone(HERE, git, self._log_line)

    def _install_done(self, rc: int, vpn_mode: str) -> None:
        self._installing = False
        self._bar.stop()
        self._bar.configure(mode="determinate", value=100 if rc == 0 else 0)
        if rc != 0:
            self._inst_status.configure(
                text=f"Setup stopped (code {rc}). The log says why; it is also saved to "
                     f"logs{os.sep}installer.log.", fg=ERR_RED)
            self._append(f"[ERROR] Setup stopped with code {rc}.")
            self._nav(back=lambda: self._show("settings"), next_text="Try again",
                      next_cmd=self._start_install, extra=("Close", self.destroy))
            return
        self._inst_status.configure(text="Installed. Next: a few quick checks.", fg=OK_GRN)
        self.existing = steps.read_env(ENV_PATH)
        self.flow = ([] if vpn_mode == "off" else ["vpn"]) + \
            ["qbit", "vlc", "jackett", "tmdb", "finish"]
        self._vpn_mode = vpn_mode
        self._nav(next_text="Next", next_cmd=lambda: self._show(self.flow[0]))

    # ── the check pages ───────────────────────────────────────────────────
    def _check_page(self, name, title, intro, runner, help_builder=None,
                    actions=(), above=None, auto=True) -> tk.Frame:
        """A page that tests one thing and shows how to fix it only if it fails.

        `runner(snap)` runs on a worker thread and returns (state, message),
        state being "ok" / "warn" / "note" / "fail". `snap` is the form's values,
        read on the Tk thread before the worker starts.
        """
        p = self._page(name, title, intro)
        if above:
            above(p)
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", pady=(self.px(6), 0))
        dot = tk.Frame(row, bg=MUTED, width=self.px(14), height=self.px(14))
        dot.pack(side="left", anchor="n", pady=self.px(4), padx=(0, self.px(10)))
        msg = tk.Label(row, text="", font=self.f_bold, fg=TXT, bg=PANEL, justify="left",
                       anchor="w", wraplength=self.wrap - self.px(40))
        msg.pack(side="left", fill="x", expand=True)

        bar = tk.Frame(p, bg=PANEL)
        bar.pack(fill="x", pady=(self.px(10), 0))
        again = self._button(bar, "Check again", lambda: self._run_check(name), primary=False)
        again.configure(padx=self.px(12), pady=self.px(4))
        again.pack(side="left")
        for label, cmd in actions:
            b = self._button(bar, label, cmd, primary=False)
            b.configure(padx=self.px(12), pady=self.px(4))
            b.pack(side="left", padx=(self.px(8), 0))

        helpf = tk.Frame(p, bg=PANEL)
        if help_builder:
            help_builder(helpf)
        self.checks[name] = {"dot": dot, "msg": msg, "again": again, "help": helpf,
                             "runner": runner, "state": "idle", "title": title}

        def enter():
            self._nav(back=(lambda: self._go(-1)) if self.flow.index(name) else None,
                      next_text="Skip for now", next_cmd=lambda: self._leave_check(name))
            if auto and self.checks[name]["state"] != "ok":
                self._run_check(name)
            else:
                self._paint_check(name, self.checks[name]["state"],
                                  self.checks[name]["msg"].cget("text"))
        self.enter[name] = enter
        return p

    def _run_check(self, name: str) -> None:
        c = self.checks[name]
        if c["state"] == "busy":
            return
        self._paint_check(name, "busy", "Checking…")
        snap = self._values()
        snap["TMDB_API_KEY"] = self.v_tmdb.get().strip()

        def work():
            try:
                state, text = c["runner"](snap)
            except Exception as exc:
                state, text = "fail", f"The check itself failed: {exc}"
            self._ui(lambda: self._paint_check(name, state, text))
        self._bg(work)

    def _paint_check(self, name: str, state: str, text: str) -> None:
        c = self.checks[name]
        c["state"] = state
        c["dot"].configure(bg=STATE_COLOR[state])
        c["msg"].configure(text=text)
        c["again"].configure(state="disabled" if state == "busy" else "normal")
        if state in ("fail", "warn"):
            c["help"].pack(fill="x", pady=(self.px(12), 0))
        else:
            c["help"].pack_forget()
        self.after_idle(self._fit)
        if self.current == name:
            self._btn_next.configure(text="NEXT" if state == "ok" else "SKIP FOR NOW",
                                     state="disabled" if state == "busy" else "normal")

    def _leave_check(self, name: str) -> None:
        if name in ("jackett", "tmdb"):
            self._save_keys()
        self._go(+1)

    # VPN
    def _build_vpn(self) -> None:
        def run(snap):
            out = steps.run_driver(HERE, steps.VPN_DRIVER)
            res = steps.result_of(out)
            if res == "ok":
                return "ok", "Your VPN is connected."
            if res == "novenv":
                return "fail", "Can't check: the install didn't finish."
            return "fail", "Your VPN is not connected.  " + steps.last_detail(out)

        def helpf(f):
            self._vpn_help = f

        def open_mullvad():
            app = steps.mullvad_app(steps.read_env(ENV_PATH).get("_MULLVAD_BIN", ""))
            if not (app and shell_open(app)):
                self._paint_check("vpn", "fail", "Couldn't find the Mullvad app. Open "
                                                 "“Mullvad VPN” from the Start menu.")

        self._check_page(
            "vpn", "Connect your VPN",
            "StreamLink only downloads while your VPN is connected. If it drops, "
            "qBittorrent is stopped until it is back.",
            run, helpf,
            actions=[("Open Mullvad", open_mullvad),
                     ("Get a Mullvad account",
                      lambda: shell_open("https://mullvad.net/account"))])
        inner = self.enter["vpn"]

        def enter():
            # The steps depend on a choice made after this page was built.
            for w in self._vpn_help.winfo_children():
                w.destroy()
            if getattr(self, "_vpn_mode", "mullvad") == "mullvad":
                self._steps(self._vpn_help, [
                    "Press Open Mullvad and enter your account number.",
                    "Press Connect and wait for “Connected”.",
                    "Come back here and press Check again."])
            else:
                self._steps(self._vpn_help, [
                    "Open your VPN app and connect.",
                    "Come back here and press Check again."])
                self._text(self._vpn_help,
                           "StreamLink looks for a VPN network adapter (WireGuard, "
                           "OpenVPN, NordLynx and similar). The two Mullvad buttons "
                           "above don't apply to you.", color=MUTED,
                           font=self.f_small).pack(anchor="w", pady=(self.px(6), 0))
            inner()
        self.enter["vpn"] = enter

    # qBittorrent
    def _build_qbit(self) -> None:
        port = tk.StringVar()
        user, pw, folder = (self.v["QBIT_USERNAME"], self.v["QBIT_PASSWORD"],
                            self.v["QBIT_DOWNLOAD_PATH"])

        def run(snap):
            self._ui(lambda: port.set(str(steps.port_of(snap["QBIT_URL"], 8081))))
            out = steps.run_driver(HERE, steps.QBIT_DRIVER)
            res = steps.result_of(out)
            if res == "ok":
                return "ok", "qBittorrent's remote control answered and accepted the login."
            if res == "vpn":
                return "note", ("Not checked: your VPN isn't connected, and StreamLink never "
                                "starts qBittorrent without it. Go back and connect, or "
                                "skip this; StreamLink reports it on its own home page.")
            if res == "login":
                return "fail", "qBittorrent answered, but refused the username or password."
            if res == "nostart":
                return "fail", ("qBittorrent started, but its remote control (Web UI) "
                                "never came up.")
            if res == "noweb":
                return "fail", "qBittorrent's remote control isn't answering."
            if res == "novenv":
                return "fail", "Can't check: the install didn't finish."
            if res == "timeout":
                return "fail", "qBittorrent took too long to answer."
            return "fail", "The check crashed.  " + steps.last_detail(out)

        def helpf(f):
            self._steps(f, [
                "Open qBittorrent from the Start menu. The first time, it shows a legal "
                "notice: press I Agree.",
                "Go to Tools → Preferences → Web UI and tick “Web User Interface "
                "(Remote control)”.",
                "Set the port, username and password to the values below. Tick “Bypass "
                "authentication for clients on localhost”.",
                "Press OK, close qBittorrent completely (File → Exit), then press "
                "Check again."])
            self._infobox(f, [("Port", port), ("Username", user), ("Password", pw),
                              ("Save files to", folder)])

        self._check_page(
            "qbit", "qBittorrent",
            "StreamLink drives qBittorrent by remote control. Setup already wrote the "
            "settings for that; this checks that qBittorrent took them.", run, helpf)

    # VLC
    def _build_vlc(self) -> None:
        def run(snap):
            if not steps.read_env(ENV_PATH).get("_VLC_BIN"):
                return "note", ("VLC wasn't found. StreamLink still plays in the browser; "
                                "only the VLC fallback player is unavailable.")
            try:
                text = steps.vlcrc_path().read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if steps.vlc_first_run_done(text):
                return "ok", "VLC is ready."
            return "fail", "VLC is still waiting to ask its first-run question."

        def helpf(f):
            self._steps(f, [
                "Press Open VLC.",
                "VLC asks about “Privacy and Network Access”. Choose what you like "
                "and press Continue.",
                "Close VLC, then press Check again."])
            self._text(f, "Until that question is answered once, it comes up every time "
                          "VLC starts and blocks StreamLink's control of it.",
                       color=MUTED, font=self.f_small).pack(anchor="w", pady=(self.px(6), 0))

        def open_vlc():
            path = steps.read_env(ENV_PATH).get("_VLC_BIN", "")
            if not (path and Path(path).exists() and shell_open(path)):
                self._paint_check("vlc", "fail", "Couldn't open VLC. Open it from the "
                                                 "Start menu.")

        self._check_page(
            "vlc", "VLC",
            "VLC is the fallback player on the TV, for files the built-in player can't "
            "handle and for 5.1 or HDR passthrough.", run, helpf,
            actions=[("Open VLC", open_vlc)])

    # Jackett
    def _build_jackett(self) -> None:
        def run(snap):
            url, key = snap["INDEXER_URL"], snap["INDEXER_API_KEY"]
            if steps.is_local_url(url):
                steps.run_driver(HERE, steps.JACKETT_DRIVER)      # start it if needed
                found = steps.read_jackett_key(steps.jackett_config_paths())
                if found and found != key:
                    key = found
                    self._ui(lambda: self.v["INDEXER_API_KEY"].set(found))
            if not key:
                return "fail", ("Jackett's API key wasn't found. Copy it from the top right "
                                "of Jackett's page into the box above.")
            state, count = steps.check_jackett(url, key)
            if state == "down":
                return "fail", f"Jackett isn't answering at {url}."
            if state == "badkey":
                return "fail", "Jackett refused that API key."
            self._ui(self._save_keys)
            if count == 0:
                return "warn", ("Jackett is running and the key works, but it has no search "
                                "sites yet. StreamLink finds nothing until you add one.")
            return "ok", (f"Jackett is ready: {count} search site"
                          f"{'' if count == 1 else 's'} set up.")

        def above(p):
            self._field(p, "Jackett API key", self.v["INDEXER_API_KEY"])
            self._field(p, "Jackett admin password", self.v["JACKETT_PASSWORD"], secret=True)
            self._text(p, "The key is read from Jackett for you. The password is only "
                          "needed if you set one inside Jackett; most people haven't.",
                       color=MUTED, font=self.f_small).pack(anchor="w", pady=(self.px(4), 0))

        def helpf(f):
            self._steps(f, [
                "Press Open Jackett. It opens in your browser.",
                "Press “+ Add indexer”, find the sites you want, and press the + beside "
                "each one.",
                "Come back here and press Check again."])

        self._check_page(
            "jackett", "Search sites (Jackett)",
            "Jackett is what StreamLink searches through. It needs at least one site "
            "(an “indexer”) added.", run, helpf, above=above,
            actions=[("Open Jackett",
                      lambda: shell_open(self.v["INDEXER_URL"].get().strip()))])

    # TMDb
    def _build_tmdb(self) -> None:
        def run(snap):
            key = snap["TMDB_API_KEY"]
            if not key:
                return "warn", ("No key entered. StreamLink works without one, but with "
                                "no posters, no Explore tab and no episode names.")
            if steps.looks_like_tmdb_v4(key):
                return "fail", ("That is the long “API Read Access Token”. Copy the "
                                "shorter “API Key” shown beside it instead.")
            res = steps.check_tmdb(key)
            if res == "ok":
                self._ui(self._save_keys)
                return "ok", "TMDb accepted the key."
            if res == "badkey":
                return "fail", "TMDb refused that key."
            self._ui(self._save_keys)
            return "note", ("Couldn't reach TMDb to check the key. It is saved anyway; "
                            "StreamLink will try it when it starts.")

        def above(p):
            self._field(p, "TMDb API key", self.v_tmdb)

        def helpf(f):
            self._steps(f, [
                "Press Get a free key and sign up (or log in) at themoviedb.org.",
                "On the API page, request a key for personal use.",
                "Copy the “API Key” into the box above and press Check again."])
            self._text(f, "You can also add it later in Admin → Indexers → TMDb Metadata.",
                       color=MUTED, font=self.f_small).pack(anchor="w", pady=(self.px(6), 0))

        self._check_page(
            "tmdb", "Posters & episode names (TMDb)",
            "A free TMDb key gives StreamLink posters, the Explore tab, and real "
            "episode names and numbering. Optional, and worth the two minutes.",
            run, helpf, above=above,
            actions=[("Get a free key",
                      lambda: shell_open("https://www.themoviedb.org/settings/api"))])

    def _save_keys(self) -> None:
        """Write the keys found or typed after setup ran. Blank never overwrites."""
        updates = {}
        for env_key, var in (("INDEXER_API_KEY", self.v["INDEXER_API_KEY"]),
                             ("JACKETT_PASSWORD", self.v["JACKETT_PASSWORD"]),
                             ("TMDB_API_KEY", self.v_tmdb)):
            val = var.get().strip()
            if val and steps.read_env(ENV_PATH).get(env_key) != val:
                updates[env_key] = val
        if updates:
            try:
                steps.set_env_keys(ENV_PATH, updates)
            except OSError as exc:
                messagebox.showerror("StreamLink Installer", f"Couldn't write .env: {exc}")

    # ── finish ────────────────────────────────────────────────────────────
    def _build_finish(self) -> None:
        p = self._page("finish", "Starting StreamLink")
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", pady=(self.px(10), 0))
        self._fin_dot = tk.Frame(row, bg=MUTED, width=self.px(14), height=self.px(14))
        self._fin_dot.pack(side="left", anchor="n", pady=self.px(4), padx=(0, self.px(10)))
        self._fin_msg = tk.Label(row, text="", font=self.f_bold, fg=TXT, bg=PANEL,
                                 justify="left", anchor="w",
                                 wraplength=self.wrap - self.px(40))
        self._fin_msg.pack(side="left", fill="x", expand=True)
        self._fin_body = self._text(p, "")
        self._fin_body.pack(anchor="w", pady=(self.px(14), 0))
        self._fin_started = False
        self.enter["finish"] = self._enter_finish

    def _fin(self, state: str, text: str) -> None:
        self._fin_dot.configure(bg=STATE_COLOR[state])
        self._fin_msg.configure(text=text)

    def _enter_finish(self) -> None:
        self._save_keys()
        self._tag.configure(text="")
        ip = steps.lan_ip()
        skipped = [self.checks[n]["title"] for n in self.flow
                   if n in self.checks and self.checks[n]["state"] != "ok"]
        body = ("On this PC:   http://localhost\n"
                + (f"On your phone or another device:   http://{ip}\n" if ip else "")
                + "Settings:   https://localhost/admin   (your browser will warn about the "
                  "certificate once; that is expected)")
        if skipped:
            body += ("\n\nNot finished yet: " + ", ".join(skipped) + ". StreamLink's home "
                     "page lists what still needs doing, or use Back.")
        if self.was_serving:
            body += ("\n\nStreamLink was already running, so it is still using its old "
                     "settings. Restart this PC to apply the new ones.")
        self._fin_body.configure(text=body)
        if self._fin_started:
            return
        back = lambda: self._go(-1)
        if self.v_service.get():
            self._fin_started = True
            self._nav()
            self._fin("busy", "Setting StreamLink to start automatically, and starting it…")
            self._bg(self._install_service)
        else:
            self._fin("idle", "StreamLink is installed. It isn't running yet.")
            self._nav(back=back, next_text="Start StreamLink", next_cmd=self._launch_now,
                      extra=("Close", self.destroy))

    def _install_service(self) -> None:
        py = steps.venv_python(HERE)
        rc = 1
        if py:
            rc = self._stream([str(py), str(HERE / "run.py"), "--install"], steps.child_env())
        if rc != 0:
            self._ui(lambda: (
                self._fin("fail", "Couldn't set StreamLink to start automatically. The "
                                  f"details are in logs{os.sep}installer.log."),
                self._nav(back=lambda: self._go(-1), next_text="Start it now",
                          next_cmd=self._launch_now, extra=("Close", self.destroy))))
            return
        self._wait_for_dashboard()

    def _launch_now(self) -> None:
        py = steps.venv_python(HERE)
        if not py:
            self._fin("fail", "Can't start: the install didn't finish.")
            return
        self._fin_started = True
        self._nav()
        self._fin("busy", "Starting StreamLink…")
        try:
            kw: dict = {"creationflags": subprocess.CREATE_NEW_CONSOLE} if IS_WIN else {}
            subprocess.Popen([str(py), str(HERE / "run.py")], cwd=str(HERE), **kw)
        except OSError as exc:
            self._fin("fail", f"Couldn't start StreamLink: {exc}")
            self._nav(next_text="Close", next_cmd=self.destroy)
            return
        self._bg(self._wait_for_dashboard)

    def _wait_for_dashboard(self) -> None:
        import time
        up = False
        for _ in range(90):
            if steps.port_open(80):
                up = True
                break
            time.sleep(1)

        def done():
            if up:
                self._fin("ok", "StreamLink is running.")
                self._nav(next_text="Open StreamLink",
                          next_cmd=lambda: shell_open("http://localhost"),
                          extra=("Close", self.destroy))
            else:
                self._fin("warn", "StreamLink was started but isn't answering yet. Give it "
                                  "a minute, then open http://localhost. If it never "
                                  f"appears, look in logs{os.sep}streamlink_service.log.")
                self._nav(next_text="Open StreamLink",
                          next_cmd=lambda: shell_open("http://localhost"),
                          extra=("Close", self.destroy))
        self._ui(done)


def main() -> None:
    if sys.version_info < (3, 9):
        print("Python 3.9+ is required. Run install.bat; it installs one.")
        sys.exit(1)
    Wizard().mainloop()


if __name__ == "__main__":
    main()
