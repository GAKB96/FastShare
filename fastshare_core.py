"""
FastShare core: discovery + PC-to-PC transfer engine (no UI code here).

Protocol v2 (TCP):
  sender  -> b"FSHR2" + uint32 len + manifest JSON
             manifest = {v, sender, id, files:[{path,size,fp}], dirs:[...], total}
  receiver-> uint32 len + reply JSON {accept: bool, skip: [file indexes], reason: str}
  sender  -> raw bytes of every non-skipped file, in manifest order
  receiver-> b"K" when everything is safely written
"""
import os, sys, json, socket, struct, threading, time, uuid, hashlib, shutil, itertools, select, errno

APP = "FastShare"
VERSION = "2.0"
TCP_PORT = int(os.environ.get("FASTSHARE_TCP_PORT", "52525"))
UDP_PORT = int(os.environ.get("FASTSHARE_UDP_PORT", "52526"))
MAGIC = b"FSHR2"
MB = 1024 * 1024
CHUNK = 4 * MB
SOCKBUF = 8 * MB
MAX_MANIFEST = 64 * MB
MAX_FILES = 500_000
MAX_SIZE = 1 << 50
ACCEPT_WAIT = 120          # seconds the receiver has to answer
IDLE_TIMEOUT = 30          # seconds without data before a transfer is treated as dropped
SPACE_MARGIN = 20 * MB
INSTANCE = uuid.uuid4().hex[:12]
HOSTNAME = (socket.gethostname() or "PC")[:60]
IS_WIN = os.name == "nt"
RESERVED = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}


class Cancelled(Exception):
    pass


class TransferError(Exception):
    pass


# ------------------------------------------------------------------ helpers
def L(p):
    """Long-path-safe version of a path on Windows (paths over 260 chars)."""
    if IS_WIN:
        p = os.path.abspath(p)
        if len(p) >= 240 and not p.startswith("\\\\?\\"):
            return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return p


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_time(s):
    s = int(max(0, s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def safe_rel(p):
    """Turn an incoming path into a safe relative path: no '..', no drive letters, no reserved names."""
    p = str(p).replace("\\", "/")
    parts = []
    for x in p.split("/"):
        if x in ("", ".", ".."):
            continue
        x = "".join("_" if (c in '<>:"|?*' or ord(c) < 32) else c for c in x).rstrip(". ")
        if not x:
            x = "_"
        if x.split(".")[0].upper() in RESERVED:
            x = "_" + x
        parts.append(x[:200])
    return os.path.join(*parts) if parts else "file"


def unique_path(path):
    if not os.path.exists(L(path)) and not os.path.exists(L(path + ".part")):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(L(f"{base} ({i}){ext}")) or os.path.exists(L(f"{base} ({i}){ext}.part")):
        i += 1
    return f"{base} ({i}){ext}"


def fingerprint(path, size):
    """Fast content fingerprint: whole file if small, else start/middle/end samples."""
    h = hashlib.sha256(str(size).encode())
    with open(L(path), "rb") as f:
        if size <= 3 * MB:
            h.update(f.read())
        else:
            for off in (0, size // 2 - MB // 2, size - MB):
                f.seek(off)
                h.update(f.read(MB))
    return h.hexdigest()[:32]


def disk_free(path):
    p = path
    while p and not os.path.exists(L(p)):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return shutil.disk_usage(L(p) if p else ".").free
    except OSError:
        return 1 << 62


def tune(sock):
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, SOCKBUF)
        except OSError:
            pass
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


def recv_exact(sock, n):
    buf = bytearray(n)
    mv = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(mv[got:])
        if not r:
            raise ConnectionError("closed")
        got += r
    return bytes(buf)


def peer_closed(conn):
    """True if the other side hung up (used while waiting for the user to click Accept)."""
    try:
        r, _, _ = select.select([conn], [], [], 0)
        if not r:
            return False
        return conn.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def local_ips():
    ips = set()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("0.0.0.0")
    return sorted(ips, key=lambda ip: (ip.startswith("127.") or ip.startswith("169.254."), ip))


def local_ip():
    ips = local_ips()
    return ips[0] if ips else "127.0.0.1"


def collect_folder(folder, errors=None):
    """-> (files [(abs_path, rel_name)], empty_dirs [rel_name]). Keeps the folder's own name."""
    root_name = os.path.basename(os.path.normpath(folder)) or "Folder"
    files, dirs = [], []

    def onerr(e):
        if errors is not None:
            errors.append(str(e))
    for dp, dns, fns in os.walk(folder, onerror=onerr):
        rel_dp = root_name if os.path.normpath(dp) == os.path.normpath(folder) else os.path.join(root_name, os.path.relpath(dp, folder))
        if not dns and not fns:
            dirs.append(rel_dp)
        for fn in fns:
            files.append((os.path.join(dp, fn), os.path.join(rel_dp, fn)))
    return files, dirs


def validate_manifest(m):
    if not isinstance(m, dict) or m.get("v") != 2:
        raise ValueError("bad manifest")
    files = m.get("files")
    dirs = m.get("dirs", [])
    if not isinstance(files, list) or not isinstance(dirs, list) or len(files) + len(dirs) > MAX_FILES:
        raise ValueError("bad file list")
    out = []
    for f in files:
        if not isinstance(f, dict):
            raise ValueError("bad entry")
        p, s, fp = f.get("path"), f.get("size"), f.get("fp", "")
        if not isinstance(p, str) or not p or len(p) > 4096:
            raise ValueError("bad path")
        if not isinstance(s, int) or isinstance(s, bool) or s < 0 or s > MAX_SIZE:
            raise ValueError("bad size")
        if not isinstance(fp, str) or len(fp) > 64:
            raise ValueError("bad fingerprint")
        out.append({"path": p, "size": s, "fp": fp})
    clean_dirs = [d for d in dirs if isinstance(d, str) and 0 < len(d) <= 4096]
    sender = m.get("sender")
    sender = sender.strip()[:60] if isinstance(sender, str) and sender.strip() else "Unknown device"
    return {"sender": sender, "id": str(m.get("id", ""))[:32], "files": out, "dirs": clean_dirs,
            "total": sum(f["size"] for f in out)}


def find_duplicates(save_dir, files):
    """Indexes of incoming files that already exist in save_dir with identical content."""
    dups = []
    for i, f in enumerate(files):
        p = os.path.join(save_dir, safe_rel(f["path"]))
        try:
            if os.path.isfile(L(p)) and os.path.getsize(L(p)) == f["size"] and f["fp"] and fingerprint(p, f["size"]) == f["fp"]:
                dups.append(i)
        except OSError:
            pass
    return dups


# ------------------------------------------------------------------ transfer state
class Transfer:
    _ids = itertools.count(1)

    def __init__(self, direction, ip, peer=""):
        self.id = next(Transfer._ids)
        self.direction = direction      # "send" | "recv"
        self.ip = ip
        self.peer = peer or ip
        self.state = "connecting"       # preparing, connecting, waiting, active, done, error, cancelled, declined
        self.total = 0
        self.done = 0
        self.files = 0
        self.current = ""
        self.created = time.time()
        self.start = 0.0
        self.end = 0.0
        self.msg = ""
        self.skipped = []               # names skipped because they were already downloaded
        self.save_dir = ""
        self.cancel_ev = threading.Event()
        self._sock = None
        self.announced = False

    def cancel(self):
        self.cancel_ev.set()
        s = self._sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    @property
    def finished(self):
        return self.state in ("done", "error", "cancelled", "declined")


# ------------------------------------------------------------------ sending
def send_files(host, items, dirs=(), tr=None, port=None):
    """items = [(abs_path, rel_name)]. Runs to completion; result is in tr.state / tr.msg."""
    port = port or TCP_PORT
    tr = tr or Transfer("send", host)
    try:
        tr.state = "preparing"
        files = []
        for p, rel in items:
            if tr.cancel_ev.is_set():
                raise Cancelled()
            name = os.path.basename(p)
            try:
                size = os.path.getsize(L(p))
                fp = fingerprint(p, size)
            except FileNotFoundError:
                raise TransferError(f"'{name}' can't be found. It may have been moved or deleted.")
            except PermissionError:
                raise TransferError(f"Windows won't let FastShare read '{name}'. It may be open in another app.")
            except OSError as e:
                raise TransferError(f"Couldn't read '{name}': {e.strerror or e}")
            files.append({"path": rel.replace("\\", "/"), "size": size, "fp": fp})
        if not files and not dirs:
            raise TransferError("There's nothing to send.")
        tr.files = len(files)
        man = {"v": 2, "sender": HOSTNAME, "id": INSTANCE, "files": files,
               "dirs": [d.replace("\\", "/") for d in dirs], "total": sum(f["size"] for f in files)}
        data = json.dumps(man).encode("utf-8")
        if len(data) > MAX_MANIFEST:
            raise TransferError("Too many files in one go. Try sending fewer at a time.")

        tr.state = "connecting"
        try:
            s = socket.create_connection((host, port), timeout=8)
        except OSError:
            raise TransferError(f"Couldn't reach {tr.peer}. Check FastShare is open there and you're on the same Wi-Fi.")
        with s:
            tr._sock = s
            if tr.cancel_ev.is_set():
                raise Cancelled()
            tune(s)
            s.settimeout(IDLE_TIMEOUT)
            s.sendall(MAGIC + struct.pack("!I", len(data)) + data)
            tr.state = "waiting"
            reply = _wait_reply(s, tr)
            if not reply.get("accept"):
                tr.state = "declined"
                tr.msg = str(reply.get("reason") or f"{tr.peer} declined.")[:300]
                return tr
            skip = {int(i) for i in reply.get("skip", []) if isinstance(i, int) and 0 <= i < len(files)}
            tr.skipped = [files[i]["path"] for i in sorted(skip)]
            todo = [(i, items[i][0], files[i]["size"]) for i in range(len(files)) if i not in skip]
            tr.total = sum(t[2] for t in todo)
            tr.done = 0
            tr.start = time.time()
            tr.state = "active"
            buf = bytearray(CHUNK)
            mv = memoryview(buf)
            for i, p, size in todo:
                tr.current = files[i]["path"]
                left = size
                try:
                    fh = open(L(p), "rb", buffering=0)
                except OSError:
                    raise TransferError(f"'{os.path.basename(p)}' disappeared before it could be sent.")
                with fh:
                    while left > 0:
                        if tr.cancel_ev.is_set():
                            raise Cancelled()
                        n = fh.readinto(mv[:min(CHUNK, left)])
                        if not n:
                            raise TransferError(f"'{os.path.basename(p)}' changed while it was being sent.")
                        s.sendall(mv[:n])
                        left -= n
                        tr.done += n
            s.settimeout(max(IDLE_TIMEOUT, 120))
            if recv_exact(s, 1) != b"K":
                raise TransferError(f"{tr.peer} didn't confirm the files arrived.")
            tr.state = "done"
    except Cancelled:
        tr.state, tr.msg = "cancelled", "Cancelled"
    except TransferError as e:
        tr.state, tr.msg = ("cancelled", "Cancelled") if tr.cancel_ev.is_set() else ("error", str(e))
    except Exception:
        if tr.cancel_ev.is_set():
            tr.state, tr.msg = "cancelled", "Cancelled"
        else:
            tr.state = "error"
            tr.msg = f"Transfer stopped. {tr.peer} may have cancelled, run out of space, or the Wi-Fi dropped."
    finally:
        tr._sock = None
        tr.end = time.time()
    return tr


def _wait_reply(s, tr):
    s.settimeout(0.5)
    deadline = time.time() + ACCEPT_WAIT + 20
    hdr = b""
    while len(hdr) < 4:
        if tr.cancel_ev.is_set():
            raise Cancelled()
        if time.time() > deadline:
            raise TransferError(f"{tr.peer} didn't answer in time.")
        try:
            chunk = s.recv(4 - len(hdr))
        except socket.timeout:
            continue
        if not chunk:
            raise TransferError(f"{tr.peer} closed the connection.")
        hdr += chunk
    n = struct.unpack("!I", hdr)[0]
    if n > MB:
        raise TransferError("Got a broken reply.")
    s.settimeout(IDLE_TIMEOUT)
    try:
        reply = json.loads(recv_exact(s, n).decode("utf-8"))
    except ValueError:
        raise TransferError("Got a broken reply.")
    if not isinstance(reply, dict):
        raise TransferError("Got a broken reply.")
    return reply


# ------------------------------------------------------------------ receiving
class Pending:
    """A question waiting for the user. result = ("accept", skip_set) or ("decline", None)."""

    def __init__(self, default):
        self.event = threading.Event()
        self.result = default
        self.close = lambda: None      # UI sets this to close its dialog

    def answer(self, result):
        if not self.event.is_set():
            self.result = result
            self.event.set()


def auto_decide(tr, man, dups):
    p = Pending(("accept", set(dups)))
    p.event.set()
    return p


class Receiver(threading.Thread):
    """Listens for transfers from other FastShare PCs.
    decide(tr, manifest, dup_indexes) -> Pending ; on_event(kind, tr) with kind in new/update/end."""

    def __init__(self, get_save_dir, decide=auto_decide, on_event=None, port=None, host="0.0.0.0"):
        super().__init__(daemon=True)
        self.get_save_dir = get_save_dir
        self.decide = decide
        self.on_event = on_event or (lambda kind, tr: None)
        self.port = port or TCP_PORT
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if IS_WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        tune(self.sock)
        try:
            self.sock.bind((host, self.port))
            self.sock.listen(16)
        except OSError:
            self.sock.close()
            raise
        self.active = set()

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def run(self):
        while True:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn, addr), daemon=True).start()

    def handle(self, conn, addr):
        tr = Transfer("recv", addr[0])
        tmp = None
        announced = False
        try:
            with conn:
                conn.settimeout(15)
                tune(conn)
                try:
                    if recv_exact(conn, len(MAGIC)) != MAGIC:
                        return
                    n = struct.unpack("!I", recv_exact(conn, 4))[0]
                    if n > MAX_MANIFEST:
                        return
                    man = validate_manifest(json.loads(recv_exact(conn, n).decode("utf-8")))
                except (ValueError, ConnectionError, OSError, UnicodeDecodeError):
                    return                                    # junk / port scan / old version: ignore quietly
                tr._sock = conn
                tr.peer = man["sender"]
                tr.files = len(man["files"])
                save = self.get_save_dir()
                tr.save_dir = save
                tr.state = "waiting"
                dups = find_duplicates(save, man["files"])
                self.active.add(tr)
                self.on_event("new", tr)
                announced = True

                pend = self.decide(tr, man, dups)
                deadline = time.time() + ACCEPT_WAIT
                while not pend.event.wait(0.3):
                    if tr.cancel_ev.is_set():
                        pend.answer(("decline", None))
                    elif peer_closed(conn):
                        pend.close()
                        tr.state, tr.msg = "cancelled", f"{tr.peer} cancelled before you answered"
                        return
                    elif time.time() > deadline:
                        pend.close()
                        pend.event.set()           # keep the default answer
                verdict, skip = pend.result
                skip = set(skip or ())
                if verdict != "accept":
                    _reply(conn, {"accept": False, "reason": f"{HOSTNAME} declined."})
                    tr.state, tr.msg = "declined", "You declined"
                    return
                need = sum(f["size"] for i, f in enumerate(man["files"]) if i not in skip)
                free = disk_free(save)
                if need + SPACE_MARGIN > free:
                    _reply(conn, {"accept": False,
                                  "reason": f"{HOSTNAME} doesn't have enough free space (needs {human(need)}, has {human(free)})."})
                    tr.state, tr.msg = "error", f"Not enough free space for {human(need)}"
                    return
                _reply(conn, {"accept": True, "skip": sorted(skip)})
                tr.skipped = [man["files"][i]["path"] for i in sorted(skip)]
                os.makedirs(L(save), exist_ok=True)
                for d in man["dirs"]:
                    os.makedirs(L(os.path.join(save, safe_rel(d))), exist_ok=True)
                tr.total = need
                tr.start = time.time()
                tr.state = "active"
                conn.settimeout(IDLE_TIMEOUT)
                buf = bytearray(CHUNK)
                mv = memoryview(buf)
                for i, f in enumerate(man["files"]):
                    if i in skip:
                        continue
                    rel = safe_rel(f["path"])
                    tr.current = rel
                    dest = unique_path(os.path.join(save, rel))
                    os.makedirs(L(os.path.dirname(dest)), exist_ok=True)
                    tmp = dest + ".part"
                    with open(L(tmp), "wb") as out:
                        left = f["size"]
                        while left > 0:
                            if tr.cancel_ev.is_set():
                                raise Cancelled()
                            r = conn.recv_into(mv[:min(CHUNK, left)])
                            if not r:
                                raise ConnectionError("sender gone")
                            out.write(mv[:r])
                            left -= r
                            tr.done += r
                    os.replace(L(tmp), L(dest))
                    tmp = None
                conn.sendall(b"K")
                tr.state = "done"
        except Cancelled:
            tr.state, tr.msg = "cancelled", "Cancelled"
        except OSError as e:
            if tr.cancel_ev.is_set():
                tr.state, tr.msg = "cancelled", "Cancelled"
            elif getattr(e, "errno", None) == errno.ENOSPC or getattr(e, "winerror", None) in (112, 39):
                tr.state, tr.msg = "error", "Your disk is full. The unfinished file was removed."
            elif isinstance(e, (ConnectionError, socket.timeout, TimeoutError)) or getattr(e, "winerror", None) in (10053, 10054):
                tr.state, tr.msg = "error", f"{tr.peer} stopped sending (cancelled or Wi-Fi dropped)."
            else:
                tr.state, tr.msg = "error", f"Couldn't save the files: {e.strerror or e}"
        except Exception as e:
            tr.state, tr.msg = "error", f"Something went wrong: {e}"
        finally:
            tr._sock = None
            if tmp:
                try:
                    os.remove(L(tmp))
                except OSError:
                    pass
            tr.end = time.time()
            self.active.discard(tr)
            if announced:
                self.on_event("end", tr)


def _reply(conn, obj):
    data = json.dumps(obj).encode("utf-8")
    conn.sendall(struct.pack("!I", len(data)) + data)


# ------------------------------------------------------------------ discovery
class Discovery(threading.Thread):
    """Announces this PC on the Wi-Fi and keeps a list of other FastShare PCs."""
    STALE = 7

    def __init__(self, port=None):
        super().__init__(daemon=True)
        self.port = port or UDP_PORT
        self.peers = {}                  # ip -> [name, last_seen, instance]
        self.lock = threading.Lock()
        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("", self.port))
        self.sock.settimeout(1)

    def _targets(self):
        t = {"255.255.255.255"}
        for ip in local_ips():
            if ip.count(".") == 3 and not ip.startswith("127."):
                t.add(".".join(ip.split(".")[:3]) + ".255")
        return t

    def announce(self, kind="hello"):
        msg = f"FSHR2|{INSTANCE}|{HOSTNAME}|{TCP_PORT}|{kind}".encode("utf-8")
        for t in self._targets():
            try:
                self.sock.sendto(msg, (t, self.port))
            except OSError:
                pass

    def bye(self):
        self.running = False
        self.announce("bye")

    def _announcer(self):
        while self.running:
            self.announce()
            time.sleep(2)

    def handle_packet(self, data, ip):
        """Returns True if this was a new device (so we should answer quickly)."""
        try:
            tag, inst, name, _port, kind = data.decode("utf-8").split("|", 4)
        except (ValueError, UnicodeDecodeError):
            return False
        if tag != "FSHR2" or inst == INSTANCE or not name:
            return False
        with self.lock:
            if kind == "bye":
                self.peers.pop(ip, None)
                return False
            new = ip not in self.peers
            self.peers[ip] = [name[:60], time.time(), inst]
        return new

    def run(self):
        threading.Thread(target=self._announcer, daemon=True).start()
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                time.sleep(0.5)
                continue
            if self.handle_packet(data, addr[0]):
                self.announce()

    def list(self):
        now = time.time()
        with self.lock:
            return {ip: v[0] for ip, v in self.peers.items() if now - v[1] < self.STALE}
