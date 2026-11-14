"""
Automatic test of the real FastShare window (used by the GitHub build on Windows).
It drives the app like a person would, sending files to itself through 127.0.0.1.
Run:  set FASTSHARE_SELFTEST=result.json  then start FastShare.
"""
import os, sys, json, time, hashlib, tempfile, traceback, shutil

import fastshare_core as C


def md5(p):
    h = hashlib.md5()
    with open(C.L(p), "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def run(app, out_path, shots_dir=None):
    import FastShare as F
    root = app.root
    results = []
    base = tempfile.mkdtemp(prefix="fs_selftest_")
    src = os.path.join(base, "src")
    recv = os.path.join(base, "received")
    os.makedirs(os.path.join(src, "Album", "sub"))
    os.makedirs(os.path.join(src, "Album", "Empty"))

    def mk(rel, size):
        p = os.path.join(src, rel)
        with open(p, "wb") as f:
            f.write(os.urandom(size))
        return p
    photo = mk("photo one.jpg", 1_000_000)
    video = mk("video.mp4", 80 * C.MB)
    mk(os.path.join("Album", "sub", "x.raw"), 3 * C.MB)
    album = os.path.join(src, "Album")
    app.save_dir = recv
    app.ask_first = True
    app.ask_var.set(True)
    ME = "127.0.0.1"
    app.manual = {ME: "This PC (self-test)"}

    def check(name, ok, detail=""):
        results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})

    def shot(name):
        if not shots_dir:
            return
        try:
            from PIL import ImageGrab
            os.makedirs(shots_dir, exist_ok=True)
            root.update()
            for d in app.dialogs:
                d.update()
            x, y = root.winfo_rootx(), root.winfo_rooty()
            w, h = root.winfo_width(), root.winfo_height()
            ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(os.path.join(shots_dir, name + ".png"))
        except Exception as e:
            check("screenshot " + name, False, e)

    def dialog(contains):
        for d in app.dialogs:
            if contains.lower() in d.kind.lower():
                return d
        return None

    def press(d, label):
        d.choose(d.buttons[label])

    def sends():
        return [t for t in app.transfers if t.direction == "send"]

    def last_send():
        s = sends()
        return s[-1] if s else None

    class Drop:
        def __init__(self, paths):
            self.data = " ".join("{%s}" % p for p in paths)
            self.action = "copy"

    def steps():
        # 1. window
        check("window opened", root.winfo_exists())
        check("drag and drop support loaded", F.HAVE_DND, "tkinterdnd2 missing" if not F.HAVE_DND else "")
        check("receiver listening", app.rx is not None)
        check("discovery running", app.disc is not None)

        # 2. drop files (incl. names with spaces and a folder) on the window
        app.on_drop(Drop([photo, video, album]), None)
        yield "files added by drop", lambda: len(app.entries) == 3 and not app.scanning, 30
        e = {x["name"]: x for x in app.entries}
        check("folder counted", e.get("Album", {}).get("count") == 1 and e["Album"]["dirs"] == [os.path.join("Album", "Empty")], e.get("Album"))
        app.on_drop(Drop([photo]), None)
        yield "same file dropped twice", lambda: not app.scanning, 10
        check("no duplicate rows in queue", len(app.entries) == 3, len(app.entries))
        yield "device card shown", lambda: ME in app.cards, 10
        shot("1_main")

        # 3. first send -> incoming popup -> Accept
        app.on_card_click(ME)
        yield "incoming popup", lambda: dialog("wants to send"), 30
        d = dialog("wants to send")
        check("incoming popup has Accept/Decline", set(d.buttons) == {"Accept", "Decline"}, list(d.buttons))
        shot("2_incoming")
        press(d, "Accept")
        yield "first send finished", lambda: last_send() and last_send().finished, 120
        t = last_send()
        check("first send done", t.state == "done", t.msg)
        check("video arrived intact", os.path.exists(os.path.join(recv, "video.mp4")) and md5(video) == md5(os.path.join(recv, "video.mp4")))
        check("photo with space arrived", os.path.exists(os.path.join(recv, "photo one.jpg")))
        check("folder structure kept", os.path.exists(os.path.join(recv, "Album", "sub", "x.raw")) and os.path.isdir(os.path.join(recv, "Album", "Empty")))
        check("no leftover .part files", not [f for r, _, fs in os.walk(recv) for f in fs if f.endswith(".part")])
        yield "success message", lambda: "Received" in (getattr(app, "last_toast", "") or "") or "Sent" in (getattr(app, "last_toast", "") or ""), 10
        shot("3_sent")

        # 4. same files again -> duplicate popup -> Skip -> sender is told
        app.on_card_click(ME)
        yield "duplicate popup", lambda: dialog("wants to send"), 30
        d = dialog("wants to send")
        check("duplicate popup offers Skip / Download again", {"Skip", "Download again"} <= set(d.buttons), list(d.buttons))
        shot("4_duplicate")
        press(d, "Skip")
        yield "sender told files already there", lambda: dialog("already has"), 30
        shot("5_already_has")
        check("sender popup lists skipped files", len(last_send().skipped) == 3, last_send().skipped)
        dialog("already has").choose(None)
        check("nothing copied twice", not os.path.exists(os.path.join(recv, "video (1).mp4")))

        # 5. Download again keeps both
        app.on_card_click(ME)
        yield "duplicate popup (again)", lambda: dialog("wants to send"), 30
        press(dialog("wants to send"), "Download again")
        yield "download again finished", lambda: last_send().finished, 120
        check("download again keeps both copies", os.path.exists(os.path.join(recv, "video (1).mp4")) and os.path.exists(os.path.join(recv, "video.mp4")))

        # 6. Partial duplicates
        new = mk("new clip.mov", 2 * C.MB)
        app.on_drop(Drop([new]), None)
        yield "new file added", lambda: len(app.entries) == 4 and not app.scanning, 10
        app.on_card_click(ME)
        yield "partial duplicate popup", lambda: dialog("wants to send"), 30
        d = dialog("wants to send")
        check("partial popup offers Accept new only", "Accept new only" in d.buttons, list(d.buttons))
        press(d, "Accept new only")
        yield "partial finished", lambda: last_send().finished, 60
        check("only the new file copied", os.path.exists(os.path.join(recv, "new clip.mov")) and not os.path.exists(os.path.join(recv, "video (2).mp4")))
        yield "partial popup on sender", lambda: dialog("already downloaded"), 15
        dialog("already downloaded").choose(None)

        # 7. Decline
        app.on_card_click(ME)
        yield "popup for decline", lambda: dialog("wants to send"), 30
        press(dialog("wants to send"), "Decline")
        yield "declined", lambda: last_send().finished, 30
        check("decline reported to sender", last_send().state == "declined", last_send().state)

        # 8. Cancel in the middle of a big transfer
        big = os.path.join(src, "huge.bin")
        with open(big, "wb") as f:
            f.truncate(3 * 1024 * C.MB)
        app.clear_queue()
        app.on_drop(Drop([big]), None)
        yield "big file queued", lambda: len(app.entries) == 1 and not app.scanning, 10
        app.on_card_click(ME)
        yield "popup for big file", lambda: dialog("wants to send"), 60
        press(dialog("wants to send"), "Accept")
        yield "big transfer running", lambda: last_send().state == "active" and last_send().done > 30 * C.MB or last_send().finished, 60
        shot("6_sending")
        if not last_send().finished:
            app.cancel_for(ME)
            yield "cancel finished", lambda: all(t.finished for t in app.transfers), 30
            check("cancel stops sender", last_send().state == "cancelled", last_send().state)
            time.sleep(0.5)
            check("cancel removes unfinished file", not os.path.exists(os.path.join(recv, "huge.bin.part")) and not os.path.exists(os.path.join(recv, "huge.bin")))
        else:
            check("cancel (transfer too fast to cancel)", True, "finished before cancel")
        yield "cancel shown", lambda: True, 1

        # 9. Ask before receiving OFF: no popup for new files, popup for duplicates
        app.ask_var.set(False)
        app.save_settings()
        app.clear_queue()
        quiet = mk("quiet.txt", 1000)
        app.on_drop(Drop([quiet]), None)
        yield "quiet file queued", lambda: len(app.entries) == 1 and not app.scanning, 10
        app.on_card_click(ME)
        yield "auto-received", lambda: last_send().finished, 30
        check("ask off: received without popup", last_send().state == "done" and os.path.exists(os.path.join(recv, "quiet.txt")))
        app.on_card_click(ME)
        yield "ask off: duplicate still asks", lambda: dialog("wants to send"), 30
        d = dialog("wants to send")
        check("ask off duplicate popup has no Decline", "Decline" not in d.buttons and "Skip" in d.buttons, list(d.buttons))
        press(d, "Skip")
        yield "ask-off skip", lambda: dialog("already has"), 30
        dialog("already has").choose(None)
        app.ask_var.set(True)
        app.save_settings()

        # 10. File deleted after adding
        gone = mk("gone.txt", 10)
        app.clear_queue()
        app.on_drop(Drop([gone]), None)
        yield "gone queued", lambda: len(app.entries) == 1 and not app.scanning, 10
        os.remove(gone)
        app.on_card_click(ME)
        yield "missing file error", lambda: last_send().finished, 30
        check("missing file explained", last_send().state == "error" and "can't be found" in last_send().msg, last_send().msg)

        # 11. Device that isn't there
        app.clear_queue()
        app.on_drop(Drop([photo]), None)
        yield "photo queued", lambda: len(app.entries) == 1 and not app.scanning, 10
        app.manual["192.0.2.1"] = "Offline PC"
        yield "offline card", lambda: "192.0.2.1" in app.cards, 10
        app.on_card_click("192.0.2.1")
        yield "unreachable", lambda: last_send().ip == "192.0.2.1" and last_send().finished, 30
        check("unreachable device explained", last_send().state == "error" and "Couldn't reach" in last_send().msg, last_send().msg)
        app.manual.pop("192.0.2.1", None)

        # 12. Add by IP validation
        app.add_by_ip()
        yield "add-by-ip dialog", lambda: dialog("Add a device"), 5
        d = dialog("Add a device")
        d.entry_var.set("hello world")
        d.choose("add")
        check("bad IP rejected with message", dialog("Add a device") is not None and "IP address" in d.err["text"], d.err["text"])
        shot("7_add_ip")
        d.entry_var.set("10.0.0.99")
        d.choose("add")
        check("good IP added", "10.0.0.99" in app.manual and dialog("Add a device") is None)
        app.manual.pop("10.0.0.99", None)

        # 13. Quit while busy asks first
        app.on_card_click(ME)
        yield "busy popup", lambda: dialog("wants to send"), 30
        app.on_close()
        yield "quit warning", lambda: dialog("still running"), 5
        press(dialog("still running"), "Keep open")
        check("window still open after Keep open", root.winfo_exists())
        press(dialog("wants to send"), "Decline")
        yield "settled", lambda: all(t.finished for t in app.transfers), 30

        check("no UI errors", not getattr(app, "errors", []), "\n".join(getattr(app, "errors", []))[:400])

    gen = steps()
    state = {"cond": None, "name": "", "deadline": 0}
    started = time.time()

    def finish():
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({"theme": "dark" if app.P is F.DARK else "light", "seconds": round(time.time() - started, 1),
                           "results": results}, f, indent=2)
        finally:
            shutil.rmtree(base, ignore_errors=True)
            try:
                app.on_close(force=True)
            except Exception:
                os._exit(0)

    def advance():
        try:
            if time.time() - started > 420:
                raise TimeoutError("self-test took too long")
            c = state["cond"]
            if c is not None and not c():
                if time.time() > state["deadline"]:
                    raise TimeoutError("timed out waiting for: " + state["name"])
                root.after(100, advance)
                return
            name, cond, secs = next(gen)
            state.update(name=name, cond=cond, deadline=time.time() + secs)
            root.after(50, advance)
        except StopIteration:
            finish()
        except Exception:
            check("step: " + state["name"], False, traceback.format_exc())
            finish()
    root.after(800, advance)
