"""
FastShare - send files of any size between PCs (and phones) on the same Wi-Fi,
at the full speed of your network. No internet, no cloud, no size limit.

PC <-> PC : both PCs run FastShare. They find each other automatically.
Phone     : turn on "Phone / browser sharing" and open the address shown
            in the phone's browser to upload or download.

Pure Python standard library - no extra packages needed.
"""
import os, sys, json, socket, struct, threading, time, queue, uuid, html, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

APP = "FastShare"
VERSION = "1.0"
TCP_PORT = 52525          # PC-to-PC transfers
UDP_PORT = 52526          # auto-discovery
WEB_PORT = 8080           # phone / browser page
MAGIC = b"FSHR1"
CHUNK = 4 * 1024 * 1024   # 4 MB blocks
SOCKBUF = 8 * 1024 * 1024 # large socket buffers for full speed
INSTANCE = uuid.uuid4().hex[:12]
HOSTNAME = socket.gethostname()


# ----------------------------------------------------------------- helpers
def config_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP, "config.json")

def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(config_path()), exist_ok=True)
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

def default_save_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads", APP)

def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # no packet is actually sent
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(HOSTNAME)
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()

def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024

def safe_rel(p):
    """Turn an incoming path into a safe relative Windows path (no .., no drive letters)."""
    p = str(p).replace("\\", "/")
    bad = '<>:"|?*'
    parts = []
    for x in p.split("/"):
        if x in ("", ".", ".."):
            continue
        x = "".join("_" if (c in bad or ord(c) < 32) else c for c in x).rstrip(". ")
        parts.append(x or "_")
    return os.path.join(*parts) if parts else "file"

def unique_path(path):
    if not os.path.exists(path) and not os.path.exists(path + ".part"):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}") or os.path.exists(f"{base} ({i}){ext}.part"):
        i += 1
    return f"{base} ({i}){ext}"

def tune(sock):
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, SOCKBUF)
        except OSError:
            pass

def recv_exact(sock, n):
    buf = bytearray(n)
    mv = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(mv[got:])
        if not r:
            raise ConnectionError("Connection closed by the other device")
        got += r
    return bytes(buf)

def collect_folder(folder):
    """Return [(abs_path, relative_name)] for every file inside a folder (keeps the folder name)."""
    out = []
    root_name = os.path.basename(os.path.normpath(folder))
    for dirpath, _, files in os.walk(folder):
        for fn in files:
            ap = os.path.join(dirpath, fn)
            rel = os.path.join(root_name, os.path.relpath(ap, folder))
            out.append((ap, rel))
    return out


# ------------------------------------------------------- PC -> PC sending
def send_files(host, items, cb=None, port=TCP_PORT):
    """items = [(abs_path, relative_name)]. cb(done_bytes, total_bytes, current_name)."""
    files = [{"path": rel.replace("\\", "/"), "size": os.path.getsize(p)} for p, rel in items]
    total = sum(f["size"] for f in files)
    manifest = json.dumps({"sender": HOSTNAME, "files": files, "total": total}).encode("utf-8")

    with socket.create_connection((host, port), timeout=10) as s:
        tune(s)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(MAGIC + struct.pack("!I", len(manifest)) + manifest)
        s.settimeout(150)                       # wait for the other person to click Accept
        if recv_exact(s, 1) != b"Y":
            raise PermissionError("The other device declined the transfer.")
        s.settimeout(60)
        buf = bytearray(CHUNK)
        mv = memoryview(buf)
        done = 0
        for (p, rel), f in zip(items, files):
            left = f["size"]
            with open(p, "rb", buffering=0) as fh:
                while left > 0:
                    n = fh.readinto(mv[:min(CHUNK, left)])
                    if not n:
                        raise IOError(f"{rel} got smaller while sending")
                    s.sendall(mv[:n])
                    left -= n
                    done += n
                    if cb:
                        cb(done, total, rel)
        s.settimeout(120)
        if recv_exact(s, 1) != b"K":
            raise ConnectionError("Receiver did not confirm.")
    return total


# ------------------------------------------------------ PC -> PC receiving
class Receiver(threading.Thread):
    """Listens for incoming transfers from other FastShare PCs."""

    def __init__(self, get_save_dir, ask, cb=None, log=print, port=TCP_PORT):
        super().__init__(daemon=True)
        self.get_save_dir, self.ask, self.cb, self.log, self.port = get_save_dir, ask, cb, log, port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tune(self.sock)
        self.sock.bind(("0.0.0.0", port))
        self.sock.listen(8)

    def run(self):
        while True:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn, addr), daemon=True).start()

    def handle(self, conn, addr):
        tmp = None
        try:
            with conn:
                tune(conn)
                conn.settimeout(30)
                if recv_exact(conn, len(MAGIC)) != MAGIC:
                    return
                ln = struct.unpack("!I", recv_exact(conn, 4))[0]
                if ln > 64 * 1024 * 1024:
                    return
                man = json.loads(recv_exact(conn, ln).decode("utf-8"))
                ok = self.ask(man, addr[0])
                conn.sendall(b"Y" if ok else b"N")
                if not ok:
                    self.log(f"Declined {len(man['files'])} file(s) from {man.get('sender', addr[0])}")
                    return
                save = self.get_save_dir()
                os.makedirs(save, exist_ok=True)
                total = int(man["total"])
                done = 0
                buf = bytearray(CHUNK)
                mv = memoryview(buf)
                conn.settimeout(60)
                for f in man["files"]:
                    rel = safe_rel(f["path"])
                    size = int(f["size"])
                    dest = unique_path(os.path.join(save, rel))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    tmp = dest + ".part"
                    with open(tmp, "wb", buffering=0) as out:
                        left = size
                        while left > 0:
                            n = conn.recv_into(mv[:min(CHUNK, left)])
                            if not n:
                                raise ConnectionError("Sender disconnected")
                            out.write(mv[:n])
                            left -= n
                            done += n
                            if self.cb:
                                self.cb(done, total, rel)
                    os.replace(tmp, dest)
                    tmp = None
                conn.sendall(b"K")
                self.log(f"Received {len(man['files'])} file(s), {human(total)} from {man.get('sender', addr[0])}")
        except Exception as e:
            self.log(f"Receive failed from {addr[0]}: {e}")
            if self.cb:
                self.cb(-1, 0, str(e))
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


# ------------------------------------------------------------- discovery
class Discovery(threading.Thread):
    """Announces this PC on the network and keeps a list of other FastShare PCs."""

    def __init__(self, port=UDP_PORT):
        super().__init__(daemon=True)
        self.port = port
        self.peers = {}   # ip -> (name, last_seen)
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("", port))

    def announce(self):
        msg = f"FSHR|{INSTANCE}|{HOSTNAME}|{TCP_PORT}".encode("utf-8")
        targets = {"255.255.255.255"}
        ip = local_ip()
        if ip.count(".") == 3:
            targets.add(".".join(ip.split(".")[:3]) + ".255")
        for t in targets:
            try:
                self.sock.sendto(msg, (t, self.port))
            except OSError:
                pass

    def _announcer(self):
        while True:
            self.announce()
            time.sleep(2)

    def run(self):
        threading.Thread(target=self._announcer, daemon=True).start()
        while True:
            try:
                data, addr = self.sock.recvfrom(1024)
            except OSError:
                continue
            try:
                tag, inst, name, _ = data.decode("utf-8").split("|", 3)
            except ValueError:
                continue
            if tag != "FSHR" or inst == INSTANCE:
                continue
            first = addr[0] not in self.peers
            with self.lock:
                self.peers[addr[0]] = (name, time.time())
            if first:          # answer straight away so new PCs show up instantly
                self.announce()

    def list(self):
        now = time.time()
        with self.lock:
            return sorted(((ip, n) for ip, (n, t) in self.peers.items() if now - t < 7), key=lambda x: x[1].lower())


# ------------------------------------------------- phone / browser sharing
WEB_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FastShare - __HOST__</title>
<style>
body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e8e8ea;margin:0;padding:18px;max-width:640px;margin:auto}
h1{font-size:22px;margin:4px 0 2px}.sub{color:#8a8f98;font-size:13px;margin-bottom:18px}
.card{background:#181b22;border:1px solid #262a33;border-radius:14px;padding:16px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 12px;color:#9fb4ff}
label.btn,button{display:inline-block;background:#3d63ff;color:#fff;border:0;border-radius:10px;padding:12px 18px;font-size:15px;cursor:pointer}
input[type=file]{display:none}
.bar{height:10px;background:#262a33;border-radius:6px;overflow:hidden;margin:12px 0 6px}
.bar div{height:100%;width:0;background:linear-gradient(90deg,#3d63ff,#7c4dff);transition:width .15s}
#st{font-size:13px;color:#b9bec8;min-height:18px}
a.f{display:flex;justify-content:space-between;gap:10px;padding:12px 4px;border-bottom:1px solid #262a33;color:#e8e8ea;text-decoration:none}
a.f span:last-child{color:#8a8f98;white-space:nowrap}.empty{color:#8a8f98;font-size:14px}
</style></head><body>
<h1>FastShare</h1><div class="sub">Connected to <b>__HOST__</b></div>
<div class="card"><h2>Send to this PC</h2>
<label class="btn">Choose files<input id="f" type="file" multiple onchange="up()"></label>
<div class="bar"><div id="b"></div></div><div id="st"></div></div>
<div class="card"><h2>Download from this PC</h2><div id="list" class="empty">Loading...</div>
<div style="margin-top:12px"><button onclick="load()">Refresh</button></div></div>
<script>
const K=location.pathname.split('/')[1], st=document.getElementById('st'), bar=document.getElementById('b');
function hs(n){const u=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return (i?n.toFixed(1):n)+' '+u[i]}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){try{const r=await fetch('/'+K+'/list');const j=await r.json();const L=document.getElementById('list');
 if(!j.length){L.className='empty';L.textContent='Nothing shared yet. Add files in FastShare on the PC.';return}
 L.className='';L.innerHTML=j.map((f,i)=>'<a class="f" href="/'+K+'/dl/'+i+'"><span>'+esc(f.name)+'</span><span>'+hs(f.size)+'</span></a>').join('')}catch(e){}}
function up(){const fs=[...document.getElementById('f').files];if(!fs.length)return;
 const total=fs.reduce((a,f)=>a+f.size,0);let base=0,i=0;const t0=Date.now();
 (function next(){if(i>=fs.length){bar.style.width='100%';st.textContent='Done - '+fs.length+' file(s) sent ✓';document.getElementById('f').value='';return}
  const f=fs[i],x=new XMLHttpRequest();x.open('PUT','/'+K+'/upload?name='+encodeURIComponent(f.name));
  x.upload.onprogress=e=>{const d=base+e.loaded,s=d/Math.max(.001,(Date.now()-t0)/1000);bar.style.width=(100*d/total).toFixed(1)+'%';
   st.textContent='Sending '+(i+1)+'/'+fs.length+' • '+hs(d)+' of '+hs(total)+' • '+hs(s)+'/s'};
  x.onload=()=>{if(x.status!==200){st.textContent='Failed: '+x.responseText;return}base+=f.size;i++;next()};
  x.onerror=()=>{st.textContent='Connection lost. Is FastShare still open on the PC?'};x.send(f)})()}
load();
</script></body></html>"""


class WebShare:
    """Small web server so phones/any browser can upload to and download from this PC."""

    def __init__(self, key, get_shared, get_save_dir, cb=None, log=print, port=WEB_PORT):
        app = self
        self.key, self.get_shared, self.get_save_dir, self.cb, self.log = key, get_shared, get_save_dir, cb, log

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _route(self):
                u = urllib.parse.urlsplit(self.path)
                parts = u.path.strip("/").split("/")
                if not parts or parts[0] != app.key:
                    return None, None
                return parts[1:], urllib.parse.parse_qs(u.query)

            def _send(self, code, body, ctype="text/plain; charset=utf-8"):
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                rest, _ = self._route()
                if rest is None:
                    return self._send(404, "Not found. Use the full address shown in FastShare.")
                shared = app.get_shared()
                if rest in ([], [""]):
                    return self._send(200, WEB_PAGE.replace("__HOST__", html.escape(HOSTNAME)), "text/html; charset=utf-8")
                if rest == ["list"]:
                    data = []
                    for p, rel in shared:
                        try:
                            data.append({"name": rel.replace("\\", "/"), "size": os.path.getsize(p)})
                        except OSError:
                            data.append({"name": rel, "size": 0})
                    return self._send(200, json.dumps(data), "application/json")
                if len(rest) == 2 and rest[0] == "dl" and rest[1].isdigit() and int(rest[1]) < len(shared):
                    p, rel = shared[int(rest[1])]
                    try:
                        size = os.path.getsize(p)
                        fh = open(p, "rb")
                    except OSError:
                        return self._send(404, "File no longer available")
                    name = os.path.basename(rel)
                    with fh:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(size))
                        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
                        self.end_headers()
                        tune(self.connection)
                        self.connection.sendfile(fh)
                    app.log(f"Phone/browser {self.client_address[0]} downloaded {name}")
                    return
                return self._send(404, "Not found")

            def do_PUT(self):
                rest, q = self._route()
                if rest != ["upload"]:
                    return self._send(404, "Not found")
                name = os.path.basename(safe_rel((q.get("name") or ["upload.bin"])[0]))
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    length = -1
                if length < 0:
                    return self._send(411, "Content-Length required")
                save = app.get_save_dir()
                os.makedirs(save, exist_ok=True)
                dest = unique_path(os.path.join(save, name))
                tmp = dest + ".part"
                left, done = length, 0
                try:
                    with open(tmp, "wb", buffering=0) as out:
                        while left > 0:
                            chunk = self.rfile.read(min(CHUNK, left))
                            if not chunk:
                                raise ConnectionError("upload interrupted")
                            out.write(chunk)
                            left -= len(chunk)
                            done += len(chunk)
                            if app.cb:
                                app.cb(done, length, name)
                    os.replace(tmp, dest)
                except Exception as e:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    app.log(f"Phone/browser upload failed: {e}")
                    self.close_connection = True
                    return
                app.log(f"Received {name} ({human(length)}) from phone/browser {self.client_address[0]}")
                return self._send(200, "ok")

        self.server = ThreadingHTTPServer(("0.0.0.0", port), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ===================================================================== GUI
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import secrets

    try:  # sharp text on high-DPI laptop screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    cfg = load_config()

    class App:
        def __init__(self, root):
            self.root = root
            self.q = queue.Queue()
            self.items = []                 # [(abs_path, rel_name)]
            self.shared = ()                # snapshot read by web server threads
            self.save_dir = cfg.get("save_dir") or default_save_dir()
            self.ask_first = cfg.get("ask_first", True)
            self.web = None
            self.web_key = cfg.get("web_key") or secrets.token_hex(3)
            self.prog = {"done": 0, "total": 0, "name": "", "mode": "", "start": 0, "err": None, "end": 0}
            self.plock = threading.Lock()
            self.sending = False

            root.title(f"{APP} - fast Wi-Fi file sharing")
            root.geometry("900x640")
            root.minsize(760, 560)
            self.build()

            try:
                self.disc = Discovery()
                self.disc.start()
            except OSError as e:
                self.disc = None
                self.log(f"Auto-discovery unavailable ({e}). You can still type the other PC's IP.")
            try:
                self.recv = Receiver(lambda: self.save_dir, self.ask, self.recv_cb, self.log_threadsafe)
                self.recv.start()
            except OSError as e:
                self.log(f"Could not start receiver on port {TCP_PORT}: {e}. Is FastShare already open?")
            self.log(f"Ready. This PC is '{HOSTNAME}' at {local_ip()}. Open FastShare on the other PC too.")
            if cfg.get("web_on"):
                self.web_var.set(True)
                self.toggle_web()
            self.pump()
            self.tick()

        # ---------- layout
        def build(self):
            st = ttk.Style()
            try:
                st.theme_use("vista")
            except tk.TclError:
                pass
            st.configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=10)
            st.configure("H.TLabel", font=("Segoe UI", 15, "bold"))

            top = ttk.Frame(self.root, padding=(12, 10, 12, 0))
            top.pack(fill="x")
            ttk.Label(top, text="FastShare", style="H.TLabel").pack(side="left")
            self.me_lbl = ttk.Label(top, text="")
            self.me_lbl.pack(side="right")

            mid = ttk.Frame(self.root, padding=12)
            mid.pack(fill="both", expand=True)
            mid.columnconfigure(0, weight=1)
            mid.columnconfigure(1, weight=3)
            mid.rowconfigure(0, weight=1)

            # devices
            dev = ttk.LabelFrame(mid, text=" 1. Choose a device ", padding=8)
            dev.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
            self.peer_list = tk.Listbox(dev, activestyle="none", font=("Segoe UI", 10), exportselection=False)
            self.peer_list.pack(fill="both", expand=True)
            self.peer_ips = []
            ttk.Label(dev, text="...or type its IP address:").pack(anchor="w", pady=(8, 2))
            self.ip_var = tk.StringVar(value=cfg.get("last_ip", ""))
            ttk.Entry(dev, textvariable=self.ip_var).pack(fill="x")
            self.peer_list.bind("<<ListboxSelect>>", lambda e: self.ip_var.set(""))

            # files
            fr = ttk.LabelFrame(mid, text=" 2. Add files or folders ", padding=8)
            fr.grid(row=0, column=1, sticky="nsew")
            btns = ttk.Frame(fr)
            btns.pack(fill="x", pady=(0, 6))
            ttk.Button(btns, text="+ Files", command=self.add_files).pack(side="left")
            ttk.Button(btns, text="+ Folder", command=self.add_folder).pack(side="left", padx=6)
            ttk.Button(btns, text="Remove", command=self.remove_sel).pack(side="left")
            ttk.Button(btns, text="Clear", command=self.clear).pack(side="left", padx=6)
            self.total_lbl = ttk.Label(btns, text="")
            self.total_lbl.pack(side="right")
            tvf = ttk.Frame(fr)
            tvf.pack(fill="both", expand=True)
            self.tree = ttk.Treeview(tvf, columns=("size",), show="tree headings", selectmode="extended")
            self.tree.heading("#0", text="Name")
            self.tree.heading("size", text="Size")
            self.tree.column("size", width=100, anchor="e", stretch=False)
            sb = ttk.Scrollbar(tvf, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=sb.set)
            self.tree.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")

            # send + progress
            sp = ttk.Frame(self.root, padding=(12, 0, 12, 6))
            sp.pack(fill="x")
            self.send_btn = ttk.Button(sp, text="3. Send  ▶", style="Big.TButton", command=self.send)
            self.send_btn.pack(side="left")
            pf = ttk.Frame(sp)
            pf.pack(side="left", fill="x", expand=True, padx=(12, 0))
            self.status = ttk.Label(pf, text="Idle")
            self.status.pack(anchor="w")
            self.pbar = ttk.Progressbar(pf, maximum=1000)
            self.pbar.pack(fill="x", pady=(4, 0))

            # settings
            sf = ttk.LabelFrame(self.root, text=" Settings ", padding=8)
            sf.pack(fill="x", padx=12, pady=(0, 6))
            r1 = ttk.Frame(sf)
            r1.pack(fill="x")
            ttk.Label(r1, text="Received files go to:").pack(side="left")
            self.dir_lbl = ttk.Label(r1, text=self.save_dir, foreground="#2457d6")
            self.dir_lbl.pack(side="left", padx=6)
            ttk.Button(r1, text="Change", command=self.change_dir).pack(side="right")
            ttk.Button(r1, text="Open folder", command=self.open_dir).pack(side="right", padx=6)
            r2 = ttk.Frame(sf)
            r2.pack(fill="x", pady=(6, 0))
            self.ask_var = tk.BooleanVar(value=self.ask_first)
            ttk.Checkbutton(r2, text="Ask me before receiving", variable=self.ask_var, command=self.save_settings).pack(side="left")
            self.web_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(r2, text="Phone / browser sharing", variable=self.web_var, command=self.toggle_web).pack(side="left", padx=(18, 6))
            self.url_var = tk.StringVar(value="(off)")
            ttk.Entry(r2, textvariable=self.url_var, state="readonly", width=34).pack(side="left")
            ttk.Button(r2, text="Copy", command=self.copy_url).pack(side="left", padx=4)

            # log
            lf = ttk.Frame(self.root, padding=(12, 0, 12, 10))
            lf.pack(fill="x")
            self.logbox = tk.Text(lf, height=5, font=("Consolas", 9), state="disabled", relief="flat", background="#f4f5f7")
            self.logbox.pack(fill="x")

        # ---------- thread helpers
        def pump(self):
            try:
                while True:
                    self.q.get_nowait()()
            except queue.Empty:
                pass
            self.root.after(50, self.pump)

        def log(self, msg):
            self.logbox.configure(state="normal")
            self.logbox.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")

        def log_threadsafe(self, msg):
            self.q.put(lambda: self.log(msg))

        def ask(self, man, ip):
            if not self.ask_first:
                return True
            ev, res = threading.Event(), {}
            n = len(man.get("files", []))
            names = "\n".join("  • " + f["path"] for f in man["files"][:8]) + ("\n  ..." if n > 8 else "")

            def dlg():
                self.root.deiconify()
                self.root.lift()
                self.root.attributes("-topmost", True)
                res["ok"] = messagebox.askyesno(
                    "Incoming files",
                    f"{man.get('sender', ip)} ({ip}) wants to send you {n} file(s), {human(man.get('total', 0))}:\n\n{names}\n\nAccept?",
                    parent=self.root)
                self.root.attributes("-topmost", False)
                ev.set()
            self.q.put(dlg)
            ev.wait(140)
            return res.get("ok", False)

        def _prog(self, mode, done, total, name):
            with self.plock:
                p = self.prog
                if done < 0:
                    p.update(err=name, end=time.time(), mode="")
                    return
                if p["mode"] != mode or done < p["done"] or total != p["total"]:
                    p.update(mode=mode, start=time.time(), err=None)
                p.update(done=done, total=total, name=name)
                if done >= total:
                    p.update(end=time.time())

        def recv_cb(self, done, total, name):
            self._prog("Receiving", done, total, name)

        def web_cb(self, done, total, name):
            self._prog("Receiving from phone", done, total, name)

        def tick(self):
            with self.plock:
                p = dict(self.prog)
            if p["err"]:
                self.status.configure(text="Failed: " + p["err"])
                with self.plock:
                    self.prog["err"] = None
            elif p["mode"] and p["total"]:
                el = max(0.001, (p["end"] or time.time()) - p["start"]) if p["done"] >= p["total"] else max(0.001, time.time() - p["start"])
                speed = p["done"] / el
                frac = p["done"] / p["total"]
                self.pbar["value"] = frac * 1000
                if p["done"] >= p["total"]:
                    self.status.configure(text=f"{p['mode']} done ✓  {human(p['total'])} in {el:.1f}s  (avg {human(speed)}/s, {speed*8/1e6:.0f} Mbps)")
                    with self.plock:
                        self.prog["mode"] = ""
                else:
                    eta = (p["total"] - p["done"]) / speed if speed > 0 else 0
                    self.status.configure(text=f"{p['mode']}: {os.path.basename(p['name'])}  •  {human(p['done'])} / {human(p['total'])}  •  {human(speed)}/s ({speed*8/1e6:.0f} Mbps)  •  {int(eta)}s left")
            # refresh device list
            peers = self.disc.list() if self.disc else []
            ips = [ip for ip, _ in peers]
            if ips != self.peer_ips:
                sel = self.selected_peer()
                self.peer_list.delete(0, "end")
                for ip, name in peers:
                    self.peer_list.insert("end", f"  \U0001F4BB  {name}   ({ip})")
                self.peer_ips = ips
                if sel in ips:
                    self.peer_list.selection_set(ips.index(sel))
                if not ips:
                    self.peer_list.insert("end", "  Searching... open FastShare on the other PC")
            self.me_lbl.configure(text=f"This PC: {HOSTNAME}   •   {local_ip()}")
            self.root.after(250, self.tick)

        # ---------- files
        def refresh_files(self):
            self.tree.delete(*self.tree.get_children())
            total = 0
            for i, (p, rel) in enumerate(self.items):
                try:
                    s = os.path.getsize(p)
                except OSError:
                    s = 0
                total += s
                self.tree.insert("", "end", iid=str(i), text=rel, values=(human(s),))
            self.total_lbl.configure(text=f"{len(self.items)} file(s) • {human(total)}" if self.items else "")
            self.shared = tuple(self.items)

        def add_files(self):
            for p in filedialog.askopenfilenames(parent=self.root, title="Choose files to send"):
                self.items.append((p, os.path.basename(p)))
            self.refresh_files()

        def add_folder(self):
            d = filedialog.askdirectory(parent=self.root, title="Choose a folder to send")
            if d:
                found = collect_folder(d)
                if not found:
                    messagebox.showinfo(APP, "That folder is empty.", parent=self.root)
                self.items.extend(found)
                self.refresh_files()

        def remove_sel(self):
            idx = sorted((int(i) for i in self.tree.selection()), reverse=True)
            for i in idx:
                del self.items[i]
            self.refresh_files()

        def clear(self):
            self.items = []
            self.refresh_files()

        # ---------- sending
        def selected_peer(self):
            sel = self.peer_list.curselection()
            if sel and sel[0] < len(self.peer_ips):
                return self.peer_ips[sel[0]]
            return None

        def send(self):
            if self.sending:
                return
            target = self.ip_var.get().strip() or self.selected_peer()
            if not target:
                return messagebox.showwarning(APP, "Pick a device on the left (or type its IP address) first.", parent=self.root)
            if not self.items:
                return messagebox.showwarning(APP, "Add some files or a folder first.", parent=self.root)
            missing = [r for p, r in self.items if not os.path.isfile(p)]
            if missing:
                return messagebox.showerror(APP, "These files no longer exist:\n" + "\n".join(missing[:10]), parent=self.root)
            cfg["last_ip"] = self.ip_var.get().strip()
            save_config(cfg)
            items = list(self.items)
            self.sending = True
            self.send_btn.configure(state="disabled")
            self.status.configure(text=f"Waiting for {target} to accept...")
            self.pbar["value"] = 0

            def work():
                try:
                    total = send_files(target, items, lambda d, t, n: self._prog("Sending", d, t, n))
                    self.log_threadsafe(f"Sent {len(items)} file(s), {human(total)} to {target}")
                except Exception as e:
                    msg = str(e)
                    if isinstance(e, (ConnectionRefusedError, socket.timeout, TimeoutError)):
                        msg = f"Could not reach {target}. Make sure FastShare is open on it and both are on the same Wi-Fi."
                    self._prog("", -1, 0, msg)
                    self.log_threadsafe("Send failed: " + msg)
                finally:
                    def done():
                        self.sending = False
                        self.send_btn.configure(state="normal")
                    self.q.put(done)
            threading.Thread(target=work, daemon=True).start()

        # ---------- settings
        def save_settings(self):
            self.ask_first = self.ask_var.get()
            cfg.update(save_dir=self.save_dir, ask_first=self.ask_first, web_on=self.web_var.get(), web_key=self.web_key)
            save_config(cfg)

        def change_dir(self):
            d = filedialog.askdirectory(parent=self.root, initialdir=self.save_dir, title="Where should received files go?")
            if d:
                self.save_dir = os.path.normpath(d)
                self.dir_lbl.configure(text=self.save_dir)
                self.save_settings()

        def open_dir(self):
            os.makedirs(self.save_dir, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(self.save_dir)
            else:
                import subprocess
                subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", self.save_dir])

        def toggle_web(self):
            if self.web_var.get() and not self.web:
                try:
                    self.web = WebShare(self.web_key, lambda: self.shared, lambda: self.save_dir, self.web_cb, self.log_threadsafe)
                    url = f"http://{local_ip()}:{WEB_PORT}/{self.web_key}"
                    self.url_var.set(url)
                    self.log(f"Phone sharing ON - open {url} in your phone's browser (same Wi-Fi).")
                except OSError as e:
                    self.web_var.set(False)
                    messagebox.showerror(APP, f"Could not start phone sharing on port {WEB_PORT}:\n{e}", parent=self.root)
            elif not self.web_var.get() and self.web:
                self.web.stop()
                self.web = None
                self.url_var.set("(off)")
                self.log("Phone sharing OFF.")
            self.save_settings()

        def copy_url(self):
            self.root.clipboard_clear()
            self.root.clipboard_append(self.url_var.get())

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
