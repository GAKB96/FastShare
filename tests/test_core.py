"""Engine tests: run with  python -m unittest discover -s tests -v"""
import os, sys, json, socket, struct, threading, time, tempfile, shutil, hashlib, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fastshare_core as C

C.IDLE_TIMEOUT = 5


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def md5(p):
    h = hashlib.md5()
    with open(C.L(p), "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def wait_for(cond, t=10):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.src)
        self.events = []
        self.decide = C.auto_decide
        self.port = free_port()
        self.rx = C.Receiver(lambda: self.out, lambda *a: self.decide(*a),
                             lambda k, tr: self.events.append((k, tr)), port=self.port, host="127.0.0.1")
        self.rx.start()

    def tearDown(self):
        self.rx.stop()
        time.sleep(0.05)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mk(self, rel, data=b"", size=None):
        p = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            if size is not None:
                f.write(os.urandom(size))
            else:
                f.write(data)
        return p

    def send(self, items, dirs=(), tr=None):
        return C.send_files("127.0.0.1", items, dirs, tr=tr, port=self.port)

    def last_rx(self):
        wait_for(lambda: any(k == "end" for k, _ in self.events))
        return [tr for k, tr in self.events if k == "end"][-1]


class Basics(Base):
    def test_files_folder_empty_file_empty_dir_unicode(self):
        a = self.mk("a.txt", b"hello")
        e = self.mk("empty.bin", b"")
        u = self.mk("ਪੰਜਾਬ photo é.jpg", b"x" * 1000)
        os.makedirs(os.path.join(self.src, "Album", "Empty"))
        self.mk(os.path.join("Album", "sub", "b.raw"), size=5 * C.MB + 7)
        files, dirs = C.collect_folder(os.path.join(self.src, "Album"))
        items = [(a, "a.txt"), (e, "empty.bin"), (u, os.path.basename(u))] + files
        tr = self.send(items, dirs)
        self.assertEqual(tr.state, "done", tr.msg)
        self.assertEqual(open(os.path.join(self.out, "a.txt"), "rb").read(), b"hello")
        self.assertEqual(os.path.getsize(os.path.join(self.out, "empty.bin")), 0)
        self.assertTrue(os.path.exists(os.path.join(self.out, os.path.basename(u))))
        self.assertEqual(md5(files[0][0]), md5(os.path.join(self.out, "Album", "sub", "b.raw")))
        self.assertTrue(os.path.isdir(os.path.join(self.out, "Album", "Empty")))
        self.assertEqual(self.last_rx().state, "done")

    def test_big_file_integrity_and_speed(self):
        p = self.mk("big.mp4", size=300 * C.MB)
        t = time.time()
        tr = self.send([(p, "big.mp4")])
        dt = time.time() - t
        self.assertEqual(tr.state, "done", tr.msg)
        self.assertEqual(md5(p), md5(os.path.join(self.out, "big.mp4")))
        print(f"\n    300 MB loopback in {dt:.2f}s = {300/dt:.0f} MB/s")

    def test_nothing_to_send(self):
        tr = self.send([])
        self.assertEqual(tr.state, "error")


class Duplicates(Base):
    def test_same_file_twice_is_skipped(self):
        p = self.mk("clip.mov", size=4 * C.MB)
        self.assertEqual(self.send([(p, "clip.mov")]).state, "done")
        tr = self.send([(p, "clip.mov")])
        self.assertEqual(tr.state, "done", tr.msg)
        self.assertEqual(tr.skipped, ["clip.mov"])
        self.assertEqual(tr.total, 0)
        self.assertFalse(os.path.exists(os.path.join(self.out, "clip (1).mov")))

    def test_partial_duplicates(self):
        a = self.mk("a.jpg", b"A" * 100)
        b = self.mk("b.jpg", b"B" * 100)
        self.send([(a, "a.jpg")])
        tr = self.send([(a, "a.jpg"), (b, "b.jpg")])
        self.assertEqual(tr.skipped, ["a.jpg"])
        self.assertTrue(os.path.exists(os.path.join(self.out, "b.jpg")))

    def test_same_name_different_content_is_not_duplicate(self):
        os.makedirs(self.out)
        open(os.path.join(self.out, "x.txt"), "wb").write(b"old!")
        p = self.mk("x.txt", b"new!")
        tr = self.send([(p, "x.txt")])
        self.assertEqual(tr.skipped, [])
        self.assertEqual(open(os.path.join(self.out, "x (1).txt"), "rb").read(), b"new!")
        self.assertEqual(open(os.path.join(self.out, "x.txt"), "rb").read(), b"old!")

    def test_download_again_keeps_both(self):
        p = self.mk("x.txt", b"same")
        self.send([(p, "x.txt")])
        seen = {}

        def again(tr, man, dups):
            seen["dups"] = dups
            pend = C.Pending(None)
            pend.answer(("accept", set()))
            return pend
        self.decide = again
        tr = self.send([(p, "x.txt")])
        self.assertEqual(seen["dups"], [0])
        self.assertEqual(tr.state, "done")
        self.assertTrue(os.path.exists(os.path.join(self.out, "x (1).txt")))

    def test_big_duplicate_detected_by_fingerprint(self):
        p = self.mk("big.bin", size=20 * C.MB)
        self.send([(p, "big.bin")])
        self.assertEqual(self.send([(p, "big.bin")]).skipped, ["big.bin"])


class Failures(Base):
    def test_decline(self):
        self.decide = lambda tr, m, d: (lambda p: (p.answer(("decline", None)), p)[1])(C.Pending(None))
        tr = self.send([(self.mk("a", b"1"), "a")])
        self.assertEqual(tr.state, "declined")
        self.assertIn("declined", tr.msg)
        self.assertFalse(os.path.exists(os.path.join(self.out, "a")))

    def test_not_enough_space(self):
        orig = C.disk_free
        C.disk_free = lambda p: 10 * C.MB
        try:
            tr = self.send([(self.mk("big", size=12 * C.MB), "big")])
        finally:
            C.disk_free = orig
        self.assertEqual(tr.state, "declined")
        self.assertIn("free space", tr.msg)

    def test_receiver_not_running(self):
        tr = C.send_files("127.0.0.1", [(self.mk("a", b"1"), "a")], port=free_port())
        self.assertEqual(tr.state, "error")
        self.assertIn("Couldn't reach", tr.msg)

    def test_missing_file(self):
        tr = self.send([(os.path.join(self.src, "nope.mp4"), "nope.mp4")])
        self.assertEqual(tr.state, "error")
        self.assertIn("can't be found", tr.msg)

    def test_sender_cancels_midway_removes_part(self):
        p = self.mk("big", size=200 * C.MB)
        tr = C.Transfer("send", "127.0.0.1")
        orig = C.CHUNK
        t = threading.Thread(target=self.send, args=([(p, "big")], (), tr))
        t.start()
        self.assertTrue(wait_for(lambda: tr.done > 10 * C.MB))
        tr.cancel()
        t.join(10)
        self.assertEqual(tr.state, "cancelled")
        rx = self.last_rx()
        self.assertIn(rx.state, ("error", "cancelled"))
        wait_for(lambda: not [f for f in os.listdir(self.out) if f.endswith(".part")])
        self.assertEqual([f for f in os.listdir(self.out) if f.endswith(".part")], [])
        self.assertFalse(os.path.exists(os.path.join(self.out, "big")))

    def test_receiver_cancels_midway(self):
        p = self.mk("big", size=300 * C.MB)
        holder = []
        self.rx.on_event = lambda k, tr: (holder.append(tr) if k == "new" else None, self.events.append((k, tr)))
        res = {}
        t = threading.Thread(target=lambda: res.setdefault("tr", self.send([(p, "big")])))
        t.start()
        self.assertTrue(wait_for(lambda: holder and holder[0].done > 10 * C.MB))
        holder[0].cancel()
        t.join(30)
        self.assertEqual(res["tr"].state, "error")
        self.assertEqual(self.last_rx().state, "cancelled")
        self.assertFalse([f for f in os.listdir(self.out) if f.endswith(".part")])

    def test_sender_cancels_while_waiting_for_accept(self):
        pends = []

        def slow(tr, m, d):
            p = C.Pending(("decline", None))
            pends.append(p)
            return p
        self.decide = slow
        tr = C.Transfer("send", "127.0.0.1")
        t = threading.Thread(target=self.send, args=([(self.mk("a", b"1"), "a")], (), tr))
        t.start()
        self.assertTrue(wait_for(lambda: tr.state == "waiting"))
        tr.cancel()
        t.join(5)
        self.assertEqual(tr.state, "cancelled")
        rx = self.last_rx()
        self.assertEqual(rx.state, "cancelled")

    def test_receiver_never_answers_times_out(self):
        old = C.ACCEPT_WAIT
        C.ACCEPT_WAIT = 1
        try:
            self.decide = lambda tr, m, d: C.Pending(("decline", None))
            tr = self.send([(self.mk("a", b"1"), "a")])
        finally:
            C.ACCEPT_WAIT = old
        self.assertEqual(tr.state, "declined")

    def test_file_shrinks_while_sending(self):
        p = self.mk("grow", size=40 * C.MB)
        tr = C.Transfer("send", "127.0.0.1")
        orig_fp = C.fingerprint

        def fp_then_truncate(path, size):
            r = orig_fp(path, size)
            with open(path, "r+b") as f:
                f.truncate(1 * C.MB)
            return r
        C.fingerprint = fp_then_truncate
        try:
            self.send([(p, "grow")], tr=tr)
        finally:
            C.fingerprint = orig_fp
        self.assertEqual(tr.state, "error")
        self.assertIn("changed", tr.msg)
        self.assertEqual(self.last_rx().state, "error")
        self.assertFalse(os.path.exists(os.path.join(self.out, "grow")))

    def test_junk_and_port_scans_dont_break_receiver(self):
        for payload in (b"", b"GET / HTTP/1.1\r\n\r\n", b"FSHR2" + struct.pack("!I", 10**9),
                        b"FSHR2" + struct.pack("!I", 5) + b"notjs",
                        b"FSHR2" + struct.pack("!I", 2) + b"{}"):
            s = socket.create_connection(("127.0.0.1", self.port))
            s.sendall(payload)
            s.close()
        time.sleep(0.3)
        tr = self.send([(self.mk("ok.txt", b"ok"), "ok.txt")])
        self.assertEqual(tr.state, "done", tr.msg)

    def test_hostile_paths_stay_inside_folder(self):
        evil = ["../../evil.txt", "C:/Windows/evil.txt", "\\\\server\\x\\evil.txt", "CON.txt", "a/../../b.txt", "nul", "x:y?.txt"]
        items = [(self.mk(f"f{i}", b"x"), e) for i, e in enumerate(evil)]
        tr = self.send(items)
        self.assertEqual(tr.state, "done", tr.msg)
        for root, _, files in os.walk(self.tmp):
            for f in files:
                self.assertTrue(os.path.abspath(os.path.join(root, f)).startswith(os.path.abspath(self.tmp)))
        self.assertTrue(os.path.exists(os.path.join(self.out, "_CON.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))

    def test_bad_manifest_values_rejected(self):
        for m in ({"v": 2, "files": [{"path": "a", "size": -1}]},
                  {"v": 2, "files": [{"path": "", "size": 1}]},
                  {"v": 2, "files": [{"path": "a", "size": True}]},
                  {"v": 1, "files": []}, [], {"v": 2, "files": "x"}):
            with self.assertRaises(ValueError):
                C.validate_manifest(m)

    def test_two_senders_at_once(self):
        a = self.mk("one.bin", size=60 * C.MB)
        b = self.mk("two.bin", size=60 * C.MB)
        res = []
        ts = [threading.Thread(target=lambda x=x: res.append(self.send([x]))) for x in [(a, "one.bin"), (b, "two.bin")]]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        self.assertEqual(sorted(r.state for r in res), ["done", "done"])
        self.assertEqual(md5(a), md5(os.path.join(self.out, "one.bin")))
        self.assertEqual(md5(b), md5(os.path.join(self.out, "two.bin")))

    def test_same_name_twice_in_one_send(self):
        a = self.mk("x/photo.jpg", b"1")
        b = self.mk("y/photo.jpg", b"2")
        tr = self.send([(a, "photo.jpg"), (b, "photo.jpg")])
        self.assertEqual(tr.state, "done")
        got = sorted(open(os.path.join(self.out, n), "rb").read() for n in ("photo.jpg", "photo (1).jpg"))
        self.assertEqual(got, [b"1", b"2"])

    def test_second_receiver_on_same_port_fails(self):
        with self.assertRaises(OSError):
            C.Receiver(lambda: self.out, port=self.port, host="127.0.0.1")

    def test_long_path(self):
        deep = os.path.join(*["folder_with_a_long_name_%02d" % i for i in range(10)])
        p = self.mk(os.path.join(deep, "file.txt"), b"deep")
        tr = self.send([(p, os.path.join(deep, "file.txt"))])
        self.assertEqual(tr.state, "done", tr.msg)
        self.assertEqual(open(C.L(os.path.join(self.out, deep, "file.txt")), "rb").read(), b"deep")


class Helpers(unittest.TestCase):
    def test_discovery_packets(self):
        d = C.Discovery.__new__(C.Discovery)
        d.peers, d.lock = {}, threading.Lock()
        self.assertTrue(d.handle_packet(b"FSHR2|abc|Office-PC|52525|hello", "10.0.0.5"))
        self.assertFalse(d.handle_packet(b"FSHR2|abc|Office-PC|52525|hello", "10.0.0.5"))
        self.assertFalse(d.handle_packet(f"FSHR2|{C.INSTANCE}|me|1|hello".encode(), "10.0.0.9"))
        self.assertFalse(d.handle_packet(b"\xff\xfe junk", "10.0.0.7"))
        self.assertFalse(d.handle_packet(b"FSHR1|x|old|1", "10.0.0.8"))
        self.assertEqual(C.Discovery.list(d), {"10.0.0.5": "Office-PC"})
        d.handle_packet(b"FSHR2|abc|Office-PC|52525|bye", "10.0.0.5")
        self.assertEqual(C.Discovery.list(d), {})

    def test_human(self):
        self.assertEqual(C.human(0), "0 B")
        self.assertEqual(C.human(1536), "1.5 KB")
        self.assertEqual(C.human_time(3725), "1h 02m")


if __name__ == "__main__":
    unittest.main(verbosity=2)
