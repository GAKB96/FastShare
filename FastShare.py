"""
FastShare - Quick Share-style file sharing between Windows PCs on the same Wi-Fi.

Drop files (or folders) on the window, then click a device to send them.
Any size, straight over your local network, no internet needed.
"""
import os, sys, time, json, queue, threading, traceback, subprocess
import tkinter as tk
from tkinter import filedialog

import fastshare_core as C

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAVE_DND = True
except Exception:
    HAVE_DND = False

FONT = "Segoe UI"
ICONS = "Segoe MDL2 Assets"
IC = dict(eye="\uE7B3", shield="\uEA18", bolt="", upload="", download="", folder="", file="",
          laptop="", pc="", close="", check="", wifi="", add="",
          search="", warn="", info="", error="", open="")

LIGHT = dict(bg="#f3f3f3", card="#ffffff", border="#dcdcdc", text="#1b1b1b", sub="#5c5c5c", muted="#8a8a8a",
             accent="#2563eb", accent_bg="#e7eefc", accent_text="#1d4ed8", on_accent="#ffffff",
             ok="#107c10", ok_bg="#e5f3e5", warn="#9a5b00", warn_bg="#fcf0da", bad="#c42b1c", bad_bg="#fde7e9",
             drop="#f7f9ff", drop_hot="#e2eafc", drop_line="#7ea3ee", track="#e6e6e6", hover="#f5f7fb", toast="#2b2b2b", toast_text="#ffffff")
DARK = dict(bg="#1b1b1b", card="#1f1f1f", border="#383838", text="#f2f2f2", sub="#b9b9b9", muted="#8c8c8c",
            accent="#3b82f6", accent_bg="#0f2647", accent_text="#6ea8ff", on_accent="#ffffff",
            ok="#4cc25f", ok_bg="#12301b", warn="#e8a33d", warn_bg="#3a240f", bad="#ff99a4", bad_bg="#45242a",
            drop="#1b2430", drop_hot="#1f3352", drop_line="#2f5a96", track="#2c2c2c", hover="#2a2a2a", toast="#f2f2f2", toast_text="#1b1b1b")


# ------------------------------------------------------------------ small Windows helpers
def windows_dark():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def app_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, C.APP)
    os.makedirs(d, exist_ok=True)
    return d


def log(msg):
    try:
        p = os.path.join(app_dir(), "fastshare.log")
        if os.path.exists(p) and os.path.getsize(p) > 1_000_000:
            os.replace(p, p + ".old")
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")
    except Exception:
        pass


def load_config():
    try:
        with open(os.path.join(app_dir(), "config.json"), encoding="utf-8") as f:
            c = json.load(f)
            return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    try:
        p = os.path.join(app_dir(), "config.json")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(p + ".tmp", p)
    except Exception as e:
        log(f"config save failed: {e}")


def open_folder(path):
    try:
        os.makedirs(C.L(path), exist_ok=True)
        if C.IS_WIN:
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        log(f"open folder failed: {e}")


def keep_awake(on):
    if C.IS_WIN:
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if on else 0x80000000)
        except Exception:
            pass


def round_rect(c, x0, y0, x1, y1, r, **kw):
    r = max(1, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1,
           x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, **kw)


def wifi_name():
    """Name of the connected Wi-Fi, or None (Ethernet, no Wi-Fi, or Windows hides it)."""
    if not C.IS_WIN:
        return None
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True,
                             timeout=4, creationflags=0x08000000, errors="replace").stdout
        for line in out.splitlines():
            k, _, v = line.partition(":")
            if k.strip() == "SSID" and v.strip():
                return v.strip()
    except Exception:
        pass
    return None


def short(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


def valid_host(s):
    if not s or len(s) > 253 or " " in s:
        return False
    parts = s.split(".")
    if all(p.isdigit() for p in parts):
        return len(parts) == 4 and all(0 <= int(p) <= 255 for p in parts)
    return all(p and all(c.isalnum() or c == "-" for c in p) for p in parts)


# ================================================================== widgets
class Btn(tk.Canvas):
    """Rounded button. kind = primary | secondary | link"""

    def __new__(cls, parent, app, text, command, kind="secondary", icon=None, size=10, bg=None):
        if kind == "link":
            return Link(parent, app, text, command)
        return super().__new__(cls)

    def __init__(self, parent, app, text, command, kind="secondary", icon=None, size=10, bg=None):
        import tkinter.font as tkfont
        self.app, self.P, self.kind, self.command = app, app.P, kind, command
        S = app.S
        f = tkfont.Font(family=FONT, size=size, weight="bold" if kind == "primary" else "normal")
        tw = f.measure(text)
        iw = S(22) if icon else 0
        self.w, self.h = tw + iw + S(32), S(36)
        super().__init__(parent, width=self.w, height=self.h, highlightthickness=0, cursor="hand2",
                         bg=bg or parent["bg"])
        self.shape = round_rect(self, 1, 1, self.w - 1, self.h - 1, S(8), width=1)
        x = S(16)
        self.icon = None
        if icon:
            self.icon = self.create_text(x, self.h / 2, text=icon, font=(ICONS, size), anchor="w")
            x += iw
        self.label = self.create_text(x, self.h / 2, text=text, font=f, anchor="w")
        self.paint(False)
        self.bind("<Enter>", lambda e: self.paint(True))
        self.bind("<Leave>", lambda e: self.paint(False))
        self.bind("<ButtonRelease-1>", lambda e: self.command() if self.command else None)

    def set_bg(self, color):
        self.configure(bg=color)

    def paint(self, hot):
        P = self.P
        if self.kind == "primary":
            fill, fg, line = (P["accent_text"] if hot else P["accent"]), P["on_accent"], ""
        else:
            fill, fg, line = (P["hover"] if hot else P["card"]), P["text"], (P["accent"] if hot else P["border"])
        self.itemconfigure(self.shape, fill=fill, outline=line)
        self.itemconfigure(self.label, fill=fg)
        if self.icon:
            self.itemconfigure(self.icon, fill=fg if self.kind == "primary" else P["accent_text"])


class Link(tk.Label):
    def __init__(self, parent, app, text, command):
        self.P = app.P
        super().__init__(parent, text=text, cursor="hand2", bg=parent["bg"], fg=app.P["accent_text"],
                         font=(FONT, 10), padx=2, pady=0)
        self.bind("<Enter>", lambda e: self.configure(font=(FONT, 10, "underline")))
        self.bind("<Leave>", lambda e: self.configure(font=(FONT, 10)))
        self.bind("<ButtonRelease-1>", lambda e: command() if command else None)


class RoundCard(tk.Canvas):
    """A frame with rounded corners. Put widgets in .inner"""

    def __init__(self, parent, app, fill, outline, radius=12, dash=None, stretch=False):
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, height=app.S(20))
        self.app, self.r, self.fill, self.outline, self.dash, self.stretch = app, app.S(radius), fill, outline, dash, stretch
        self.pad = max(2, int(self.r * 0.32))
        self.inner = tk.Frame(self, bg=fill)
        self.win = self.create_window(self.pad, self.pad, window=self.inner, anchor="nw")
        self.shape = None
        self.bind("<Configure>", self._redraw)
        if not stretch:
            self.inner.bind("<Configure>", self._fit)

    def _fit(self, e=None):
        h = self.inner.winfo_reqheight() + 2 * self.pad
        if int(float(self["height"])) != h:
            self.configure(height=h)

    def _redraw(self, e=None):
        w, h = self.winfo_width(), self.winfo_height()
        if self.shape:
            self.delete(self.shape)
        self.shape = round_rect(self, 1, 1, w - 1, h - 1, self.r, fill=self.fill, outline=self.outline,
                                dash=self.dash, width=1)
        self.tag_lower(self.shape)
        self.itemconfigure(self.win, width=max(1, w - 2 * self.pad))
        if self.stretch:
            self.itemconfigure(self.win, height=max(1, h - 2 * self.pad))

    def set_outline(self, color):
        if color != self.outline:
            self.outline = color
            if self.shape:
                self.itemconfigure(self.shape, outline=color)


class Circle(tk.Canvas):
    def __init__(self, parent, app, size, glyph, gsize=13):
        self.app, self.size = app, app.S(size)
        super().__init__(parent, width=self.size, height=self.size, highlightthickness=0, bg=parent["bg"])
        self.oval = self.create_oval(1, 1, self.size - 1, self.size - 1, outline="")
        self.text = self.create_text(self.size / 2, self.size / 2, text=glyph, font=(ICONS, gsize))

    def set(self, fill, fg, glyph=None, bg=None):
        self.itemconfigure(self.oval, fill=fill)
        self.itemconfigure(self.text, fill=fg)
        if glyph:
            self.itemconfigure(self.text, text=glyph)
        if bg:
            self.configure(bg=bg)


class DeviceCard(RoundCard):
    def __init__(self, app, parent, ip):
        P, S = app.P, app.S
        super().__init__(parent, app, P["card"], P["border"], 12)
        self.configure(cursor="hand2")
        self.app, self.ip, self.hot, self.active = app, ip, False, False
        f = self.inner
        f.columnconfigure(1, weight=1)
        self.icon = Circle(f, app, 46, IC["laptop"], 16)
        self.icon.grid(row=0, column=0, rowspan=2, padx=(S(12), S(12)), pady=S(12))
        self.name = tk.Label(f, bg=P["card"], fg=P["text"], font=(FONT, 12, "bold"), anchor="w")
        self.name.grid(row=0, column=1, sticky="sw", pady=(S(12), 0))
        self.status = tk.Label(f, bg=P["card"], fg=P["sub"], font=(FONT, 10), anchor="w")
        self.status.grid(row=1, column=1, sticky="nw", pady=(0, S(12)))
        self.x = tk.Label(f, text=IC["close"], font=(ICONS, 10), bg=P["card"], fg=P["sub"], cursor="hand2", padx=S(12))
        self.x.bind("<ButtonRelease-1>", lambda e: app.cancel_for(ip))
        self.bar = tk.Canvas(f, height=S(8), bg=P["card"], highlightthickness=0)
        self.track = self.bar.create_line(0, 0, 0, 0, width=S(6), capstyle="round", fill=P["track"])
        self.fill_line = self.bar.create_line(0, 0, 0, 0, width=S(6), capstyle="round", fill=P["accent"])
        self.showing_bar = self.showing_x = False
        for w in (self, f, self.icon, self.name, self.status):
            w.bind("<ButtonRelease-1>", lambda e: app.on_card_click(ip))
            w.bind("<Enter>", lambda e: self.hover(True))
            w.bind("<Leave>", self.leave)
            w.bind("<Button-3>", lambda e: app.card_menu(ip, e))
            if HAVE_DND:
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", lambda e: app.on_drop(e, ip))
                    w.dnd_bind("<<DropEnter>>", lambda e: (self.hover(True), e.action)[1])
                    w.dnd_bind("<<DropLeave>>", lambda e: (self.hover(False), e.action)[1])
                except Exception:
                    pass

    def leave(self, e):
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None and w is not self:
            w = getattr(w, "master", None)
        if w is None:
            self.hover(False)

    def hover(self, on):
        self.hot = on
        self.set_outline(self.app.P["accent"] if (on or self.active) else self.app.P["border"])

    def update_view(self, name, status, fg, circle_bg, glyph, pct=None, cancellable=False):
        S = self.app.S
        if self.name["text"] != short(name, 30):
            self.name.configure(text=short(name, 30))
        if self.status["text"] != status or self.status["fg"] != fg:
            self.status.configure(text=status, fg=fg)
        self.icon.set(circle_bg, fg, glyph)
        if cancellable != self.showing_x:
            self.showing_x = cancellable
            (self.x.grid(row=0, column=2, rowspan=2, sticky="ns") if cancellable else self.x.grid_remove())
        show_bar = pct is not None
        if show_bar != self.active:
            self.active = show_bar
            self.hover(self.hot)
        if show_bar != self.showing_bar:
            self.showing_bar = show_bar
            if show_bar:
                self.bar.grid(row=2, column=0, columnspan=3, sticky="ew", padx=S(14), pady=(0, S(12)))
            else:
                self.bar.grid_remove()
        if show_bar:
            w, y, r = self.bar.winfo_width(), S(4), S(3)
            self.bar.coords(self.track, r, y, max(r, w - r), y)
            self.bar.coords(self.fill_line, r, y, max(r, r + (w - 2 * r) * pct), y)
            self.bar.itemconfigure(self.fill_line, fill=fg)


class Dialog(tk.Toplevel):
    """Rounded-feel modal card used for incoming files, duplicates and questions."""

    def __init__(self, app, heading, sub="", glyph=IC["info"], tone="accent", names=(), note=None,
                 buttons=(("OK", None, "primary"),), on_result=None, close_value=None, timeout=None,
                 timeout_text=None, entry=None):
        P = app.P
        super().__init__(app.root)
        self.app, self.on_result, self.close_value, self.done = app, on_result, close_value, False
        self.kind = heading
        self.withdraw()
        self.title(C.APP)
        self.configure(bg=P["card"])
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", lambda: self.choose(self.close_value))
        self.bind("<Escape>", lambda e: self.choose(self.close_value))
        S = app.S
        body = tk.Frame(self, bg=P["card"], padx=S(22), pady=S(18))
        body.pack(fill="both", expand=True)
        top = tk.Frame(body, bg=P["card"])
        top.pack(fill="x")
        c = Circle(top, app, 44, glyph, 16)
        c.set(P[tone + "_bg"], P[tone if tone != "accent" else "accent_text"])
        c.pack(side="left", padx=(0, S(12)))
        tf = tk.Frame(top, bg=P["card"])
        tf.pack(side="left", fill="x", expand=True)
        tk.Label(tf, text=heading, bg=P["card"], fg=P["text"], font=(FONT, 13, "bold"), anchor="w",
                 justify="left", wraplength=S(360)).pack(anchor="w")
        if sub:
            tk.Label(tf, text=sub, bg=P["card"], fg=P["sub"], font=(FONT, 10), anchor="w", justify="left",
                     wraplength=S(380)).pack(anchor="w")
        if names:
            box = RoundCard(body, app, P["bg"], P["border"], 8)
            box.pack(fill="x", pady=(S(14), 0))
            lst = tk.Frame(box.inner, bg=P["bg"], padx=S(10), pady=S(6))
            lst.pack(fill="x")
            for text, tag in names:
                row = tk.Frame(lst, bg=P["bg"])
                row.pack(fill="x", pady=1)
                tk.Label(row, text=short(text, 48), bg=P["bg"], fg=P["text"], font=(FONT, 10), anchor="w").pack(side="left")
                if tag:
                    tk.Label(row, text=tag, bg=P["warn_bg"], fg=P["warn"], font=(FONT, 9), padx=6).pack(side="right")
        if note:
            nbox = RoundCard(body, app, P["warn_bg"], P["warn_bg"], 8)
            nbox.pack(fill="x", pady=(S(10), 0))
            nf = tk.Frame(nbox.inner, bg=P["warn_bg"], padx=S(10), pady=S(6))
            nf.pack(fill="x")
            tk.Label(nf, text=IC["warn"], font=(ICONS, 10), bg=P["warn_bg"], fg=P["warn"]).pack(side="left", padx=(0, 8))
            tk.Label(nf, text=note, bg=P["warn_bg"], fg=P["warn"], font=(FONT, 10), justify="left",
                     wraplength=S(330)).pack(side="left")
        self.entry_var = None
        if entry is not None:
            self.entry_var = tk.StringVar(value=entry)
            e = tk.Entry(body, textvariable=self.entry_var, font=(FONT, 11), bg=P["bg"], fg=P["text"],
                         insertbackground=P["text"], relief="flat", highlightthickness=1,
                         highlightbackground=P["border"], highlightcolor=P["accent"])
            e.pack(fill="x", pady=(S(14), 0), ipady=S(5))
            e.focus_set()
            e.bind("<Return>", lambda ev: self.choose(buttons[-1][1]))
            self.err = tk.Label(body, text="", bg=P["card"], fg=P["bad"], font=(FONT, 9), anchor="w")
            self.err.pack(fill="x")
            self.entry_var.trace_add("write", lambda *a: self.err.configure(text=""))
        bf = tk.Frame(body, bg=P["card"])
        bf.pack(fill="x", pady=(S(16), 0))
        self.timer = tk.Label(bf, text="", bg=P["card"], fg=P["muted"], font=(FONT, 8))
        self.timer.pack(side="left")
        self.buttons = {}
        for label, value, kind in reversed(buttons):
            b = Btn(bf, app, label, lambda v=value: self.choose(v), kind)
            b.pack(side="right", padx=(S(8), 0))
            self.buttons[label] = value
        self.deadline = time.time() + timeout if timeout else None
        self.timeout_text = timeout_text
        app.dialogs.append(self)
        self.update_idletasks()
        rx, ry, rw, rh = app.root.winfo_rootx(), app.root.winfo_rooty(), app.root.winfo_width(), app.root.winfo_height()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"+{max(0, rx + (rw - w) // 2)}+{max(0, ry + (rh - h) // 3)}")
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        app.dark_titlebar(self)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        if self.deadline:
            self.tick()

    def tick(self):
        if self.done:
            return
        left = int(self.deadline - time.time())
        if left <= 0:
            return self.choose(self.close_value)
        self.timer.configure(text=self.timeout_text.format(left))
        self.after(500, self.tick)

    def choose(self, value):
        if self.done:
            return
        if self.entry_var is not None and value is not None:
            ok = self.on_result(value, self.entry_var.get().strip())
            if ok is not True:
                self.err.configure(text=ok or "")
                return
            self.close()
            return
        self.done = True
        self.close()
        if self.on_result and self.entry_var is None:
            self.on_result(value)

    def close(self):
        self.done = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        if self in self.app.dialogs:
            self.app.dialogs.remove(self)
        try:
            self.destroy()
        except tk.TclError:
            pass


# ================================================================== app
class App:
    def __init__(self, root, theme=None):
        self.root = root
        self.q = queue.Queue()
        self.cfg = load_config()
        self.P = DARK if (theme or ("dark" if windows_dark() else "light")) == "dark" else LIGHT
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.save_dir = self.cfg.get("save_dir") or os.path.join(os.path.expanduser("~"), "Downloads", C.APP)
        self.ask_first = bool(self.cfg.get("ask_first", True))
        self.manual = {k: v for k, v in (self.cfg.get("manual") or {}).items() if isinstance(k, str)}
        self.entries = []
        self.transfers = []
        self.cards = {}
        self.order = []
        self.dialogs = []
        self.speed = {}
        self.names = {}
        self.queue_dirty = True
        self.toast_until = 0
        self.awake = False
        self.after_scan_send = None
        self.scanning = 0

        root.title("FastShare")
        root.configure(bg=self.P["bg"])
        root.geometry(f"{self.S(980)}x{self.S(680)}")
        root.minsize(self.S(840), self.S(600))
        self.ssid = None
        self._ip, self._ip_at = C.local_ip(), time.time()
        root.report_callback_exception = self.on_ui_error
        self.build()
        self.dark_titlebar(root)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.disc = None
        try:
            self.disc = C.Discovery()
            self.disc.start()
        except OSError as e:
            log(f"discovery failed: {e}")
        self.rx = None
        try:
            self.rx = C.Receiver(lambda: self.save_dir, self.decide, self.on_rx_event)
            self.rx.start()
        except OSError as e:
            log(f"receiver failed: {e}")
            self.root.after(500, lambda: self.toast(
                "Can't receive files: another app is using FastShare's network port. Restart your PC and try again.", "bad", 10))
        self.pump()
        self.tick()

    def S(self, n):
        return int(round(n * self.scale))

    # ---------------------------------------------------------------- layout
    def build(self):
        P, S = self.P, self.S
        hdr = tk.Frame(self.root, bg=P["bg"], padx=S(20), pady=S(14))
        hdr.pack(fill="x")
        tk.Label(hdr, text=IC["bolt"], font=(ICONS, 16), bg=P["bg"], fg=P["accent_text"]).pack(side="left")
        tk.Label(hdr, text="FastShare", font=(FONT, 16, "bold"), bg=P["bg"], fg=P["text"]).pack(side="left", padx=(S(8), 0))

        body = tk.Frame(self.root, bg=P["bg"], padx=S(20))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=11, uniform="c")
        body.columnconfigure(1, weight=10, uniform="c")
        body.rowconfigure(0, weight=1)

        # ---- left: drop zone + queue
        left = tk.Frame(body, bg=P["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, S(16)))
        self.drop = tk.Canvas(left, height=S(270), bg=P["bg"], highlightthickness=0, cursor="hand2")
        self.drop.pack(fill="x")
        self.drop_hot = False
        self.drop.bind("<Configure>", lambda e: self.draw_drop())
        self.drop.bind("<ButtonRelease-1>", lambda e: self.pick_files())
        self.b_files = Btn(self.drop, self, "Select files", self.pick_files, icon=IC["file"], size=11, bg=P["drop"])
        self.b_folder = Btn(self.drop, self, "Select folder", self.pick_folder, icon=IC["folder"], size=11, bg=P["drop"])
        if HAVE_DND:
            for w in (self.root, self.drop):
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", lambda e: self.on_drop(e, None))
                    w.dnd_bind("<<DropEnter>>", lambda e: (self.set_drop_hot(True), e.action)[1])
                    w.dnd_bind("<<DropLeave>>", lambda e: (self.set_drop_hot(False), e.action)[1])
                except Exception as ex:
                    log(f"dnd register failed: {ex}")

        qc = RoundCard(left, self, P["card"], P["border"], 12, stretch=True)
        qc.pack(fill="both", expand=True, pady=(S(14), S(14)))
        qcard = qc.inner
        qh = tk.Frame(qcard, bg=P["card"], padx=S(14), pady=S(10))
        qh.pack(fill="x")
        tk.Label(qh, text="Ready to send", font=(FONT, 12, "bold"), bg=P["card"], fg=P["text"]).pack(side="left")
        self.clear_btn = Btn(qh, self, "Clear", self.clear_queue, "link")
        self.clear_btn.pack(side="right")
        self.q_total = tk.Label(qh, text="", font=(FONT, 11), bg=P["card"], fg=P["sub"])
        self.q_total.pack(side="right", padx=S(8))
        self.q_rows = tk.Frame(qcard, bg=P["card"], padx=S(14))
        self.q_rows.pack(fill="both", expand=True, pady=(0, S(8)))

        # ---- right: devices
        right = tk.Frame(body, bg=P["bg"])
        right.grid(row=0, column=1, sticky="nsew")
        rh = tk.Frame(right, bg=P["bg"])
        rh.pack(fill="x", pady=(0, S(8)))
        tk.Label(rh, text="Nearby devices", font=(FONT, 13, "bold"), bg=P["bg"], fg=P["text"]).pack(side="left")
        self.net_lbl = tk.Label(rh, text="", font=(FONT, 11), bg=P["bg"], fg=P["sub"])
        self.net_lbl.pack(side="right")
        tk.Label(rh, text=IC["wifi"], font=(ICONS, 11), bg=P["bg"], fg=P["sub"]).pack(side="right", padx=(0, S(6)))
        self.dev_frame = tk.Frame(right, bg=P["bg"])
        self.dev_frame.pack(fill="x")
        sc = RoundCard(right, self, P["bg"], P["border"], 12, dash=(5, 4))
        sc.pack(fill="x", pady=(0, 0))
        self.search = sc.inner
        self.search_icon = Circle(self.search, self, 46, IC["search"], 15)
        self.search_icon.set(P["track"], P["sub"], bg=P["bg"])
        self.search_icon.pack(side="left", padx=(S(12), S(12)), pady=S(12))
        sf = tk.Frame(self.search, bg=P["bg"])
        sf.pack(side="left", fill="x", expand=True)
        self.search_lbl = tk.Label(sf, text="Looking for devices", font=(FONT, 12), bg=P["bg"], fg=P["sub"], anchor="w")
        self.search_lbl.pack(anchor="w")
        self.search_hint = tk.Label(sf, text="Open FastShare on the other PC. Both need the same Wi-Fi, set to Private.",
                                    font=(FONT, 9), bg=P["bg"], fg=P["muted"], anchor="w", justify="left", wraplength=S(300))
        self.search_hint.pack(anchor="w")
        Btn(right, self, "+ Add a device by IP address", self.add_by_ip, "link").pack(anchor="w", pady=(S(10), 0))

        # ---- footer
        foot = tk.Frame(self.root, bg=P["bg"], padx=S(20), pady=S(12))
        foot.pack(fill="x", side="bottom")
        tk.Frame(self.root, bg=P["border"], height=1).pack(fill="x", side="bottom")

        def group(side, glyph, **kw):
            g = tk.Frame(foot, bg=P["bg"])
            g.pack(side=side, **kw)
            tk.Label(g, text=glyph, font=(ICONS, 11), bg=P["bg"], fg=P["sub"]).pack(side="left", padx=(0, S(6)))
            return g
        g = group("left", IC["eye"])
        tk.Label(g, text="Visible as", font=(FONT, 11), bg=P["bg"], fg=P["sub"]).pack(side="left")
        self.me_lbl = tk.Label(g, text="", font=(FONT, 11, "bold"), bg=P["bg"], fg=P["text"])
        self.me_lbl.pack(side="left", padx=(S(4), 0))
        g = group("left", IC["shield"], padx=(S(36), 0))
        self.ask_var = tk.BooleanVar(value=self.ask_first)
        self.ask_lbl = tk.Label(g, text="", font=(FONT, 11), bg=P["bg"], fg=P["sub"], cursor="hand2")
        self.ask_lbl.pack(side="left")
        self.ask_lbl.bind("<ButtonRelease-1>", lambda e: (self.ask_var.set(not self.ask_var.get()), self.save_settings()))
        g = group("right", IC["download"])
        self.dir_btn = Btn(g, self, "", lambda: open_folder(self.save_dir), "link")
        self.dir_btn.pack(side="left")
        Btn(g, self, "Change", self.change_dir, "link").pack(side="left", padx=(S(10), 0))
        self.update_dir_label()
        self.update_ask_label()

        # ---- toast
        self.toast_frame = tk.Frame(self.root, bg=P["toast"], padx=S(14), pady=S(8))
        self.toast_lbl = tk.Label(self.toast_frame, text="", bg=P["toast"], fg=P["toast_text"], font=(FONT, 10),
                                  wraplength=S(560), justify="left")
        self.toast_lbl.pack(side="left")
        self.toast_act = tk.Label(self.toast_frame, text="", bg=P["toast"], fg=P["toast_text"],
                                  font=(FONT, 10, "underline"), cursor="hand2")
        self.toast_act.pack(side="left", padx=(S(12), 0))

    def draw_drop(self):
        P, S, c = self.P, self.S, self.drop
        c.delete("all")
        w, h, r = c.winfo_width(), c.winfo_height(), S(14)
        if w < 50:
            return
        x0, y0, x1, y1 = 2, 2, w - 2, h - 2
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1,
               x0, y1 - r, x0, y0 + r, x0, y0]
        c.create_polygon(pts, smooth=True, fill=P["drop_hot"] if self.drop_hot else P["drop"],
                         outline=P["accent"] if self.drop_hot else P["drop_line"],
                         dash=(6, 4), width=max(1, S(1.5)))
        cx, cy = w / 2, h / 2 - S(50)
        c.create_oval(cx - S(36), cy - S(36), cx + S(36), cy + S(36), fill=P["accent_bg"], outline="")
        c.create_text(cx, cy, text=IC["download"] if self.drop_hot else IC["upload"], font=(ICONS, 22), fill=P["accent_text"])
        title = "Drop to add" if self.drop_hot else ("Drop files or folders here" if HAVE_DND else "Choose files or folders")
        c.create_text(cx, cy + S(60), text=title, font=(FONT, 15, "bold"), fill=P["text"])
        c.create_text(cx, cy + S(88), text="Any size, straight over your Wi-Fi", font=(FONT, 11), fill=P["sub"])
        bg = P["drop_hot"] if self.drop_hot else P["drop"]
        self.b_files.set_bg(bg)
        self.b_folder.set_bg(bg)
        c.create_window(cx - S(6), cy + S(132), window=self.b_files, anchor="e")
        c.create_window(cx + S(6), cy + S(132), window=self.b_folder, anchor="w")

    def set_drop_hot(self, on):
        if on != self.drop_hot:
            self.drop_hot = on
            self.draw_drop()

    def dark_titlebar(self, win):
        if C.IS_WIN and self.P is DARK:
            try:
                import ctypes
                win.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
                v = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), 4)
            except Exception:
                pass

    # ---------------------------------------------------------------- threads -> UI
    def pump(self):
        try:
            for _ in range(200):
                fn = self.q.get_nowait()
                try:
                    fn()
                except Exception:
                    self.on_ui_error(*sys.exc_info())
        except queue.Empty:
            pass
        self.root.after(40, self.pump)

    def on_ui_error(self, exc, val, tb):
        log("UI error:\n" + "".join(traceback.format_exception(exc, val, tb)))
        self.errors = getattr(self, "errors", []) + ["".join(traceback.format_exception(exc, val, tb))]
        try:
            self.toast("Something went wrong. Details were saved to the FastShare log.", "bad")
        except Exception:
            pass

    def toast(self, text, tone="info", secs=5, action=None):
        P = self.P
        bg = {"bad": P["bad_bg"], "warn": P["warn_bg"], "ok": P["ok_bg"]}.get(tone, P["toast"])
        fg = {"bad": P["bad"], "warn": P["warn"], "ok": P["ok"]}.get(tone, P["toast_text"])
        for w in (self.toast_frame, self.toast_lbl, self.toast_act):
            w.configure(bg=bg)
        self.toast_lbl.configure(text=text, fg=fg)
        if action:
            self.toast_act.configure(text=action[0], fg=fg)
            self.toast_act.bind("<ButtonRelease-1>", lambda e: (action[1](), self.hide_toast()))
            self.toast_act.pack(side="left", padx=(self.S(12), 0))
        else:
            self.toast_act.pack_forget()
        self.toast_frame.place(relx=0.5, y=self.S(14), anchor="n")
        self.toast_frame.lift()
        self.toast_until = time.time() + secs
        self.last_toast = text

    def hide_toast(self):
        self.toast_frame.place_forget()
        self.toast_until = 0

    # ---------------------------------------------------------------- adding files
    def pick_files(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="Choose files to send")
        if paths:
            self.add_paths(list(paths))
        return bool(paths)

    def pick_folder(self):
        d = filedialog.askdirectory(parent=self.root, title="Choose a folder to send")
        if d:
            self.add_paths([d])
        return bool(d)

    def on_drop(self, event, ip):
        self.set_drop_hot(False)
        for card in self.cards.values():
            card.hover(False)
        try:
            paths = [p for p in self.root.tk.splitlist(event.data) if p]
        except tk.TclError:
            paths = []
        if not paths:
            return getattr(event, "action", None)
        if ip:
            self.add_paths(paths, then=lambda new: self.start_send(ip, new))
        else:
            self.add_paths(paths)
        return getattr(event, "action", None)

    def add_paths(self, paths, then=None):
        """Scan in the background (folders can be huge) then add to the queue or send directly."""
        self.scanning += 1
        self.q_total.configure(text="Adding…")

        def work():
            new, errors = [], []
            for p in paths:
                p = os.path.normpath(p)
                try:
                    if os.path.isdir(p):
                        files, dirs = C.collect_folder(p, errors)
                        size = 0
                        for ap, _ in files:
                            try:
                                size += os.path.getsize(C.L(ap))
                            except OSError:
                                errors.append(ap)
                        new.append(dict(path=p, name=os.path.basename(p) or p, kind="folder", files=files, dirs=dirs,
                                        size=size, count=len(files)))
                    elif os.path.isfile(p):
                        new.append(dict(path=p, name=os.path.basename(p), kind="file", files=[(p, os.path.basename(p))],
                                        dirs=[], size=os.path.getsize(C.L(p)), count=1))
                    else:
                        errors.append(p)
                except OSError as e:
                    errors.append(f"{p}: {e}")
            self.q.put(lambda: self._added(new, errors, then))
        threading.Thread(target=work, daemon=True).start()

    def _added(self, new, errors, then):
        self.scanning -= 1
        if errors:
            log("couldn't read: " + "; ".join(errors[:20]))
            self.toast(f"{len(errors)} item(s) couldn't be read and were left out.", "warn")
        if then:
            self.queue_dirty = True
            if new:
                then(new)
            return
        have = {e["path"] for e in self.entries}
        added = [e for e in new if e["path"] not in have]
        self.entries.extend(added)
        self.queue_dirty = True
        if added and self.after_scan_send:
            ip, self.after_scan_send = self.after_scan_send, None
            self.start_send(ip)

    def remove_entry(self, path):
        self.entries = [e for e in self.entries if e["path"] != path]
        self.queue_dirty = True

    def clear_queue(self):
        self.entries = []
        self.queue_dirty = True

    def render_queue(self):
        P, S = self.P, self.S
        for w in self.q_rows.winfo_children():
            w.destroy()
        n = len(self.entries)
        total = sum(e["size"] for e in self.entries)
        files = sum(e["count"] for e in self.entries)
        if self.scanning:
            self.q_total.configure(text="Adding…")
        else:
            self.q_total.configure(text=f"{files} file{'s' if files != 1 else ''} · {C.human(total)}" if n else "")
        if not n:
            tk.Label(self.q_rows, text="Add files, then click a device to send them.\nYou can also drop files straight onto a device.",
                     bg=P["card"], fg=P["muted"], font=(FONT, 10), justify="left").pack(anchor="w", pady=S(6))
            return
        MAXROWS = 6
        for e in self.entries[:MAXROWS]:
            row = tk.Frame(self.q_rows, bg=P["card"])
            row.pack(fill="x", pady=S(3))
            tk.Label(row, text=IC["folder"] if e["kind"] == "folder" else IC["file"], font=(ICONS, 11),
                     bg=P["card"], fg=P["accent_text"]).pack(side="left", padx=(0, S(8)))
            label = e["name"] + (f"  ({e['count']} file{'s' if e['count'] != 1 else ''})" if e["kind"] == "folder" else "")
            tk.Label(row, text=short(label, 36), bg=P["card"], fg=P["text"], font=(FONT, 11), anchor="w").pack(side="left")
            x = tk.Label(row, text=IC["close"], font=(ICONS, 8), bg=P["card"], fg=P["muted"], cursor="hand2", padx=S(4))
            x.pack(side="right")
            x.bind("<ButtonRelease-1>", lambda ev, p=e["path"]: self.remove_entry(p))
            tk.Label(row, text=C.human(e["size"]), bg=P["card"], fg=P["sub"], font=(FONT, 11)).pack(side="right", padx=S(6))
        if n > MAXROWS:
            tk.Label(self.q_rows, text=f"+ {n - MAXROWS} more", bg=P["card"], fg=P["sub"], font=(FONT, 10)).pack(anchor="w", pady=S(2))

    # ---------------------------------------------------------------- sending
    def peer_name(self, ip):
        if self.disc and ip in self.disc.list():
            return self.disc.list()[ip]
        return self.names.get(ip) or self.manual.get(ip) or ip

    def on_card_click(self, ip):
        busy = [t for t in self.transfers if t.ip == ip and not t.finished]
        if busy:
            return self.toast(f"Already busy with {self.peer_name(ip)}. Click the ✕ on its card to cancel.", "warn")
        if self.scanning:
            self.after_scan_send = ip
            return
        if not self.entries:
            self.after_scan_send = ip
            if not self.pick_files():
                self.after_scan_send = None
            return
        self.start_send(ip)

    def start_send(self, ip, entries=None):
        entries = entries if entries is not None else list(self.entries)
        if not entries:
            return
        if [t for t in self.transfers if t.ip == ip and t.direction == "send" and not t.finished]:
            return self.toast(f"Already sending to {self.peer_name(ip)}.", "warn")
        items, dirs = [], []
        for e in entries:
            items.extend(e["files"])
            dirs.extend(e["dirs"])
        tr = C.Transfer("send", ip, self.peer_name(ip))
        self.transfers.append(tr)
        threading.Thread(target=C.send_files, args=(ip, items, dirs, tr), daemon=True).start()

    def cancel_for(self, ip):
        for t in self.transfers:
            if t.ip == ip and not t.finished:
                t.cancel()

    # ---------------------------------------------------------------- receiving (called from receiver threads)
    def on_rx_event(self, kind, tr):
        if kind == "new":
            self.q.put(lambda: (self.transfers.append(tr), self.names.__setitem__(tr.ip, tr.peer)))

    def decide(self, tr, man, dups):
        ask = self.ask_first
        if not ask and not dups:
            p = C.Pending(("accept", set()))
            p.event.set()
            return p
        p = C.Pending(("decline", None) if ask else ("accept", set(dups)))
        self.q.put(lambda: self.show_incoming(tr, man, dups, p, ask))
        return p

    def show_incoming(self, tr, man, dups, pend, ask):
        if pend.event.is_set():
            return
        files = man["files"]
        n, k = len(files), len(dups)
        dupset = set(dups)
        names = [(f["path"], "Already downloaded" if i in dupset else "") for i, f in enumerate(files[:6])]
        if n > 6:
            names.append((f"+ {n - 6} more", ""))
        accept_all, skip_dups, decline = ("accept", frozenset()), ("accept", frozenset(dups)), ("decline", None)
        heading = f"{tr.peer} wants to send you {n} file{'s' if n != 1 else ''}" if n else f"{tr.peer} wants to send you folders"
        sub = f"{C.human(man['total'])}  ·  from {tr.ip}"
        note = None
        if k == n and n:
            note = "You've already downloaded " + ("this file." if n == 1 else "all of these files.") + " They're in your FastShare folder."
        elif k:
            note = f"{k} of these {'is' if k == 1 else 'are'} already downloaded."
        if not k:
            buttons = (("Decline", decline, "secondary"), ("Accept", accept_all, "primary"))
        elif k == n:
            buttons = ((("Decline", decline, "secondary"),) if ask else ()) + \
                      (("Download again", accept_all, "secondary"), ("Skip", skip_dups, "primary"))
        else:
            buttons = ((("Decline", decline, "secondary"),) if ask else ()) + \
                      (("Download all again", accept_all, "secondary"), ("Accept new only", skip_dups, "primary"))
        close_value = pend.result
        d = Dialog(self, heading, sub, IC["download"], "warn" if k else "accent", names, note, buttons,
                   on_result=lambda v: pend.answer((v[0], set(v[1] or ())) if v else close_value),
                   close_value=close_value, timeout=C.ACCEPT_WAIT,
                   timeout_text=("Declines automatically in {}s" if ask else "Skips duplicates automatically in {}s"))
        pend.close = lambda: self.q.put(d.close)

        def watch():
            if d.done:
                return
            if pend.event.is_set() or tr.finished:
                d.close()
            else:
                d.after(300, watch)
        watch()
        self.flash()

    def flash(self):
        if C.IS_WIN:
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                ctypes.windll.user32.FlashWindow(hwnd, True)
            except Exception:
                pass
        try:
            self.root.deiconify()
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- results
    def announce(self, t):
        t.announced = True
        peer = t.peer
        if t.direction == "send":
            if t.state == "done":
                if t.skipped:
                    sent = t.files - len(t.skipped)
                    one = len(t.skipped) == 1
                    heading = (f"{peer} already has " + ("this file" if one else "these files")) if not sent else \
                        f"Sent {sent} file{'s' if sent != 1 else ''}. {len(t.skipped)} {'was' if one else 'were'} already downloaded"
                    names = [(os.path.basename(p) or p, "Already downloaded") for p in t.skipped[:6]]
                    if len(t.skipped) > 6:
                        names.append((f"+ {len(t.skipped) - 6} more", ""))
                    Dialog(self, heading, "Files that are already on the other PC are skipped, so nothing is copied twice.",
                           IC["info"], "warn", names, buttons=(("OK", None, "primary"),))
                else:
                    dt = max(0.001, t.end - t.start)
                    self.toast(f"Sent to {peer}  ·  {C.human(t.total)} in {C.human_time(dt)} "
                               f"({C.human(t.total / dt)}/s)", "ok")
            elif t.state == "declined":
                self.toast(t.msg or f"{peer} declined.", "warn")
            elif t.state == "error":
                self.toast(t.msg, "bad", 8)
        else:
            if t.state == "done":
                got, sk = t.files - len(t.skipped), len(t.skipped)
                if got:
                    extra = f" ({sk} already downloaded, skipped)" if sk else ""
                    text = f"Received {got} file{'s' if got != 1 else ''} from {peer}{extra}"
                else:
                    text = f"Skipped {sk} file{'s' if sk != 1 else ''} from {peer}: you already have {'it' if sk == 1 else 'them'}"
                self.toast(text, "ok", 7, ("Open folder", lambda d=t.save_dir: open_folder(d)))
            elif t.state == "error":
                self.toast(t.msg, "bad", 8)
            elif t.state == "cancelled" and t.msg and t.msg != "Cancelled":
                self.toast(t.msg, "warn")

    def card_info(self, ip, online):
        """-> (status text, colour key, circle bg key, pct or None, cancellable)"""
        P = self.P
        trs = [t for t in self.transfers if t.ip == ip]
        active = [t for t in trs if not t.finished]
        now = time.time()
        if active:
            rank = {"active": 3, "waiting": 2}
            t = max(active, key=lambda x: (rank.get(x.state, 1), x.id))
            if t.state == "preparing":
                return "Getting files ready…", "sub", "track", None, True
            if t.state == "connecting":
                return "Connecting…", "sub", "track", None, True
            if t.state == "waiting":
                if t.direction == "send":
                    return "Waiting for them to accept…", "warn", "warn_bg", None, True
                return f"Wants to send you {t.files} file{'s' if t.files != 1 else ''}", "warn", "warn_bg", None, True
            pct = t.done / t.total if t.total else 1.0
            last = self.speed.get(t.id)
            if last is None or now - last[0] >= 0.5:
                inst = (t.done - last[1]) / (now - last[0]) if last else 0
                ema = inst if not last or last[2] == 0 else last[2] * 0.6 + inst * 0.4
                self.speed[t.id] = (now, t.done, ema)
            sp = self.speed[t.id][2]
            eta = f" · {C.human_time((t.total - t.done) / sp)} left" if sp > 0 and t.done < t.total else ""
            verb = "Sending" if t.direction == "send" else "Receiving"
            return f"{verb} {int(pct * 100)}% · {C.human(sp)}/s{eta}", "accent_text", "accent_bg", pct, True
        recent = [t for t in trs if t.finished and now - t.end < 10]
        if recent:
            t = recent[-1]
            if t.state == "done":
                if t.direction == "send":
                    msg = "Already had these files" if t.skipped and t.total == 0 else "Sent ✓"
                else:
                    msg = "Received ✓"
                return msg, "ok", "ok_bg", None, False
            if t.state == "declined":
                return ("Declined" if t.direction == "send" else "You declined"), "bad", "bad_bg", None, False
            if t.state == "cancelled":
                return "Cancelled", "sub", "track", None, False
            return short(t.msg or "Failed", 46), "bad", "bad_bg", None, False
        if online:
            return "Ready · click to send", "ok", "ok_bg", None, False
        return "Not found right now · click to try", "muted", "track", None, False

    # ---------------------------------------------------------------- periodic refresh
    def tick(self):
        try:
            self._tick()
        except Exception:
            self.on_ui_error(*sys.exc_info())
        self.root.after(150, self.tick)

    def _tick(self):
        P = self.P
        now = time.time()
        online = self.disc.list() if self.disc else {}
        for ip, nm in online.items():
            self.names[ip] = nm
        recent_ips = [t.ip for t in self.transfers if not t.finished or now - t.end < 60]
        ips = set(online) | set(self.manual) | set(recent_ips)
        order = sorted(ips, key=lambda ip: (ip not in online, self.peer_name(ip).lower()))
        if order != self.order:
            for ip in list(self.cards):
                if ip not in ips:
                    self.cards.pop(ip).destroy()
            for ip in order:
                if ip not in self.cards:
                    self.cards[ip] = DeviceCard(self, self.dev_frame, ip)
                self.cards[ip].pack_forget()
            for ip in order:
                self.cards[ip].pack(fill="x", pady=(0, self.S(8)))
            self.order = order
        for ip, card in self.cards.items():
            status, fg, cbg, pct, canc = self.card_info(ip, ip in online)
            glyph = IC["laptop"]
            card.update_view(self.peer_name(ip), status, P[fg], P[cbg], glyph, pct, canc)
        dots = "." * (int(now * 2) % 4)
        self.search_lbl.configure(text=("Looking for more devices" if online else "Looking for devices") + dots)
        self.search_hint.configure(text="Open FastShare on the other PC. Both need the same Wi-Fi, set to Private." if not online
                                   else "Drop files on a device to send them right away.")

        for t in self.transfers:
            if t.finished and not t.announced:
                self.announce(t)
        self.transfers = [t for t in self.transfers if not t.finished or now - t.end < 120]
        live = {t.id for t in self.transfers}
        self.speed = {k: v for k, v in self.speed.items() if k in live}

        busy = any(not t.finished for t in self.transfers)
        if busy != self.awake:
            self.awake = busy
            keep_awake(busy)
        if self.queue_dirty:
            self.queue_dirty = False
            self.render_queue()
        if self.toast_until and now > self.toast_until:
            self.hide_toast()
        if now - self._ip_at > 15:
            self._ip_at = now

            def refresh():
                ip, name = C.local_ip(), wifi_name()
                self.q.put(lambda: (setattr(self, "_ip", ip), setattr(self, "ssid", name)))
            threading.Thread(target=refresh, daemon=True).start()
        ip = self._ip
        offline = ip.startswith("127.")
        self.me_lbl.configure(text=f"{C.HOSTNAME}  ({ip})" if not offline else C.HOSTNAME)
        self.net_lbl.configure(text="Not connected" if offline else (self.ssid or "Local network"))

    # ---------------------------------------------------------------- settings / misc
    def update_ask_label(self):
        self.ask_lbl.configure(text=f"Ask before receiving: {'on' if self.ask_var.get() else 'off'}")

    def save_settings(self):
        self.ask_first = bool(self.ask_var.get())
        self.update_ask_label()
        self.cfg.update(save_dir=self.save_dir, ask_first=self.ask_first, manual=self.manual)
        save_config(self.cfg)

    def update_dir_label(self):
        d = self.save_dir
        home = os.path.expanduser("~")
        shown = d.replace(home, "~", 1) if d.startswith(home) else d
        self.dir_btn.configure(text=short(shown, 34))

    def change_dir(self):
        d = filedialog.askdirectory(parent=self.root, initialdir=self.save_dir, title="Where should received files go?")
        if d:
            test = os.path.join(d, ".fastshare_write_test")
            try:
                with open(C.L(test), "w") as f:
                    f.write("x")
                os.remove(C.L(test))
            except OSError:
                return self.toast("FastShare can't save files in that folder. Pick another one.", "bad")
            self.save_dir = os.path.normpath(d)
            self.update_dir_label()
            self.save_settings()

    def add_by_ip(self):
        def done(_, text):
            if not valid_host(text):
                return "That doesn't look like an IP address, e.g. 192.168.1.20"
            if text in C.local_ips() and text not in ("127.0.0.1",):
                return "That's this PC's own address."
            self.manual[text] = self.manual.get(text) or text
            self.save_settings()
            return True
        Dialog(self, "Add a device by IP address", "Find it on the other PC: FastShare shows it at the top right.",
               IC["add"], "accent", buttons=(("Cancel", None, "secondary"), ("Add", "add", "primary")),
               on_result=done, entry="")

    def card_menu(self, ip, e):
        P = self.P
        m = tk.Menu(self.root, tearoff=0, bg=P["card"], fg=P["text"], activebackground=P["accent"], activeforeground="#fff")
        m.add_command(label="Send files…", command=lambda: self.on_card_click(ip))
        if [t for t in self.transfers if t.ip == ip and not t.finished]:
            m.add_command(label="Cancel transfer", command=lambda: self.cancel_for(ip))
        if ip in self.manual:
            m.add_command(label="Remove device", command=lambda: (self.manual.pop(ip, None), self.save_settings()))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def on_close(self, force=False):
        busy = [t for t in self.transfers if not t.finished]
        if busy and not force:
            return Dialog(self, "Transfers are still running", "If you quit now they'll stop and unfinished files are removed.",
                          IC["warn"], "warn", buttons=(("Keep open", None, "secondary"), ("Quit anyway", "quit", "primary")),
                          on_result=lambda v: self.on_close(True) if v == "quit" else None)
        for t in busy:
            t.cancel()
        try:
            if self.disc:
                self.disc.bye()
            if self.rx:
                self.rx.stop()
        except Exception:
            pass
        keep_awake(False)
        time.sleep(0.2 if busy else 0)
        self.root.destroy()


# ================================================================== entry point
def single_instance():
    if not C.IS_WIN or os.environ.get("FASTSHARE_SELFTEST"):
        return True
    try:
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\FastShareSingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:
            ctypes.windll.user32.MessageBoxW(None, "FastShare is already open. Look for it on the taskbar.", "FastShare", 0x40)
            return False
    except Exception:
        pass
    return True


def main():
    if not single_instance():
        return
    if C.IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
    app = App(root, os.environ.get("FASTSHARE_THEME"))
    if os.environ.get("FASTSHARE_SELFTEST"):
        import selftest
        selftest.run(app, os.environ["FASTSHARE_SELFTEST"], os.environ.get("FASTSHARE_SHOTS"))
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("fatal:\n" + traceback.format_exc())
        raise
