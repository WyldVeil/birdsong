import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from birdsong import config as C, engine as E, web  # noqa: E402


def _ts(d, h=0, m=0):
    return time.mktime(datetime(d.year, d.month, d.day, h, m).timetuple())


class Live:
    def __init__(self):
        self.ts = time.time()

    def snapshot(self, since=0):
        cols = bytes(range(64)) * 10
        want = 10 if since <= 0 or since > 110 else 110 - since
        return {"ts": self.ts, "started": 1, "level_db": -40.0, "seq": 110, "bands": 64, "col_rate": 20,
                "cols": cols[-want * 64:], "hearing": [{"sci": "Erithacus rubecula", "conf": 0.9, "since": 1}]}


class Server(unittest.TestCase):
    base_path = "/"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = C.load(self.tmp)
        self.cfg["base_path"] = self.base_path
        C.set_password(self.cfg, "hunter22")
        self.db = E.connect(os.path.join(self.tmp, "birds.db"))
        self.db.executemany("INSERT INTO species(sci, common, tier, slug, photo) VALUES (?,?,?,?,?)", [
            ("Erithacus rubecula", "European Robin", 1, "erithacus-rubecula", 1),
            ("Turdus merula", "Eurasian Blackbird", 1, "turdus-merula", 0),
            ("Bombycilla garrulus", "Bohemian Waxwing", 2, "bombycilla-garrulus", 0),
            ("Turdus migratorius", "American Robin", 3, "turdus-migratorius", 0),
        ])
        self.db.commit()
        self.app = web.App(self.tmp, self.cfg, live=Live())
        self.srv = web.make_server(self.app, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.cookie = None

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, sci, ts, conf=0.9, tier=1, **kw):
        lt = time.localtime(ts)
        cols = dict(sci=sci, start=ts, end=ts + 3, day=time.strftime("%Y-%m-%d", lt), hour=lt.tm_hour,
                    conf=conf, hits=1, tier=tier, review="pending" if tier == 3 else "", open=0)
        cols.update(kw)
        cur = self.db.execute(f"INSERT INTO detections({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                              list(cols.values()))
        self.db.commit()
        return cur.lastrowid

    def req(self, path, method="GET", body=None, headers=None, admin=False):
        h = dict(headers or {})
        if admin:
            h["Cookie"] = f"{web.COOKIE}={self.app.make_session()}"
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def json(self, path, admin=False):
        status, _h, body = self.req(self.cfg["base_path"].rstrip("/") + "/" + path, admin=admin)
        self.assertEqual(status, 200, path)
        return json.loads(body)


class Basics(Server):
    def test_page_and_headers(self):
        for p in ("/", "/birds.html"):
            status, h, body = self.req(p)
            self.assertEqual(status, 200)
            self.assertIn(b"<!doctype html>", body[:50])
            self.assertIn("noindex", h["X-Robots-Tag"])
            self.assertEqual(h["Referrer-Policy"], "no-referrer")

    def test_font_served(self):
        status, h, body = self.req("/font/fraunces.woff2")
        self.assertEqual((status, h["Content-Type"]), (200, "font/woff2"))
        self.assertEqual(body[:4], b"wOF2")

    def test_unknown_paths_404(self):
        for p in ("/nope", "/api/nope", "/clip/1", "/img/../config.jpg", "/spec/x.png", "/data/config.json"):
            self.assertEqual(self.req(p)[0], 404, p)

    def test_info(self):
        info = self.json("api/info")
        self.assertEqual(info["title"], "Birdsong")
        self.assertNotIn("latitude", json.dumps(info))

    def test_birdweather_token_never_public(self):
        self.app.cfg["birdweather_token"] = "SECRETTOKEN99"
        live = self.json("api/live")
        self.assertTrue(live["birdweather"])
        for path in ("api/live", "api/info", "api/period", "api/species"):
            self.assertNotIn("SECRETTOKEN99", json.dumps(self.json(path)))
        self.assertNotIn("SECRETTOKEN99", json.dumps(self.json("api/admin/status", admin=True)))


class Visibility(Server):
    def test_public_hides_hidden_and_pending(self):
        now = time.time() - 60
        self.add("Erithacus rubecula", now)
        self.add("Turdus merula", now + 1, hidden=1)
        pend = self.add("Turdus migratorius", now + 2, tier=3)
        pub = self.json("api/log")["items"]
        self.assertEqual([d["sci"] for d in pub], ["Erithacus rubecula"])
        self.assertNotIn("clip", pub[0])
        adm = self.json("api/log", admin=True)["items"]
        self.assertEqual({d["sci"] for d in adm}, {"Erithacus rubecula", "Turdus migratorius"})
        status, _h, _b = self.req(f"/api/admin/det/{pend}", "POST", {"action": "confirm"}, {"X-Birds": "1"}, admin=True)
        self.assertEqual(status, 200)
        self.assertIn("Turdus migratorius", {d["sci"] for d in self.json("api/log")["items"]})

    def test_speech_spectrogram_admin_only(self):
        os.makedirs(os.path.join(self.tmp, "spectro", "x"))
        with open(os.path.join(self.tmp, "spectro", "x", "1.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nfake")
        quiet = self.add("Erithacus rubecula", time.time() - 99, spectro="spectro/x/1.png")
        talky = self.add("Erithacus rubecula", time.time() - 50, spectro="spectro/x/1.png", speech=1)
        self.assertEqual(self.req(f"/spec/{quiet}.png")[0], 200)
        self.assertEqual(self.req(f"/spec/{talky}.png")[0], 404)
        self.assertEqual(self.req(f"/spec/{talky}.png", admin=True)[0], 200)

    def test_clip_admin_only_with_ranges(self):
        os.makedirs(os.path.join(self.tmp, "clips", "x"))
        with open(os.path.join(self.tmp, "clips", "x", "1.mp3"), "wb") as fh:
            fh.write(b"0123456789")
        i = self.add("Erithacus rubecula", time.time() - 5, clip="clips/x/1.mp3", clip_bytes=10)
        self.assertEqual(self.req(f"/clip/{i}")[0], 404)
        status, h, body = self.req(f"/clip/{i}", admin=True)
        self.assertEqual((status, body, h["Content-Type"]), (200, b"0123456789", "audio/mpeg"))
        status, h, body = self.req(f"/clip/{i}", admin=True, headers={"Range": "bytes=2-4"})
        self.assertEqual((status, body, h["Content-Range"]), (206, b"234", "bytes 2-4/10"))

    def test_forged_cookie(self):
        for c in ("1.2", "9999999999." + "0" * 64):
            self.assertEqual(self.req("/api/admin/status", headers={"Cookie": f"{web.COOKIE}={c}"})[0], 404)


class Admin(Server):
    def test_login_flow_and_throttle(self):
        status, h, _b = self.req("/api/login", "POST", {"username": "admin", "password": "hunter22"}, {"X-Birds": "1"})
        self.assertEqual(status, 200)
        cookie = h["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Secure", cookie)               # plain http on localhost/LAN
        token = cookie.split(";")[0]
        status, _h, body = self.req("/api/me", headers={"Cookie": token})
        self.assertEqual(json.loads(body), {"admin": True})
        for _ in range(5):
            self.assertEqual(self.req("/api/login", "POST", {"username": "admin", "password": "x"}, {"X-Birds": "1"})[0], 401)
        self.assertEqual(self.req("/api/login", "POST", {"username": "admin", "password": "hunter22"}, {"X-Birds": "1"})[0], 429)

    def test_csrf_header_required(self):
        self.assertEqual(self.req("/api/login", "POST", {"username": "admin", "password": "hunter22"})[0], 404)

    def test_actions(self):
        os.makedirs(os.path.join(self.tmp, "clips", "x"))
        path = os.path.join(self.tmp, "clips", "x", "1.mp3")
        with open(path, "wb") as fh:
            fh.write(b"abc")
        i = self.add("Erithacus rubecula", time.time(), clip="clips/x/1.mp3", clip_bytes=3)

        def act(a):
            status, _h, body = self.req(f"/api/admin/det/{i}", "POST", {"action": a}, {"X-Birds": "1"}, admin=True)
            self.assertEqual(status, 200, a)
            return json.loads(body)["det"]
        self.assertTrue(act("save")["saved"])
        self.assertTrue(act("hide")["hidden"])
        self.assertFalse(act("unhide")["hidden"])
        self.assertFalse(act("delete_clip")["clip"])
        self.assertFalse(os.path.exists(path))

    def test_status(self):
        st = self.json("api/admin/status", admin=True)
        self.assertIn("disk_free", st)
        self.assertEqual(self.req("/api/admin/status")[0], 404)


class Periods(Server):
    def test_new_means_first_ever_or_back_after_45_days(self):
        d = date.today() - timedelta(days=2)
        self.add("Turdus merula", _ts(d - timedelta(days=100), 9))         # station started; Blackbird left
        self.add("Turdus merula", _ts(d, 9))                               # back after 100 days: new
        self.add("Erithacus rubecula", _ts(d - timedelta(days=10), 9))
        self.add("Erithacus rubecula", _ts(d, 9))                          # heard 10 days ago: not new
        self.add("Bombycilla garrulus", _ts(d, 10), tier=2)                # never heard before: new
        P = self.json(f"api/period?span=day&date={d.isoformat()}")
        self.assertEqual({x["sci"] for x in P["species"] if x["new"]}, {"Turdus merula", "Bombycilla garrulus"})
        self.assertEqual(P["totals"]["new"], 2)
        P = self.json(f"api/period?span=day&date={(d - timedelta(days=100)).isoformat()}")   # the first day
        self.assertEqual((P["totals"]["new"], [x["new"] for x in P["species"]]), (None, [False]))
        self.assertFalse(any(x["new"] for x in self.json("api/period?span=all")["species"]))

    def test_today_compares_with_this_time_yesterday(self):
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.add("Erithacus rubecula", time.time() - 1)                                   # today, just now
        self.add("Turdus merula", time.mktime((midnight + (now - midnight) / 2 - timedelta(days=1)).timetuple()))
        self.add("Bombycilla garrulus", _ts(date.today() - timedelta(days=1), 23, 59))      # later than now
        P = self.json(f"api/period?span=day&date={date.today().isoformat()}")
        self.assertEqual(P["prev_totals"], {"detections": 1, "species": 1, "partial": True})
        self.assertEqual(web._same_point(date(2026, 10, 1), date(2026, 9, 1), datetime(2026, 10, 11, 8, 30)),
                         time.mktime(datetime(2026, 9, 11, 8, 30).timetuple()))

    def test_day(self):
        d = date.today() - timedelta(days=1)
        self.add("Erithacus rubecula", _ts(d, 6, 1))
        self.add("Erithacus rubecula", _ts(d, 6, 40))
        self.add("Turdus merula", _ts(d, 18, 5))
        self.add("Turdus merula", _ts(d - timedelta(days=1), 9))
        P = self.json(f"api/period?span=day&date={d.isoformat()}")
        self.assertEqual((P["series"][6], P["series"][18], P["totals"]["detections"]), (2, 1, 3))
        self.assertEqual(P["prev_totals"], {"detections": 1, "species": 1})
        sp = {s["sci"]: s for s in P["species"]}
        self.assertTrue(sp["Erithacus rubecula"]["new"])
        self.assertFalse(sp["Turdus merula"]["new"])

    def test_week_month_year_all(self):
        self.add("Erithacus rubecula", _ts(date(2026, 3, 18), 7))
        self.add("Erithacus rubecula", _ts(date(2026, 3, 16), 7))
        self.add("Turdus merula", _ts(date(2026, 1, 2), 7))
        self.assertEqual(self.json("api/period?span=week&date=2026-03-18")["series"], [1, 0, 1, 0, 0, 0, 0])
        self.assertEqual(len(self.json("api/period?span=month&date=2026-03-18")["series"]), 31)
        self.assertEqual(self.json("api/period?span=year&date=2026-03-18")["series"][:3], [1, 0, 2])
        A = self.json("api/period?span=all")
        self.assertEqual((A["keys"][0], sum(A["series"])), ("2026-01", 3))

    def test_species_and_calendar(self):
        self.add("Erithacus rubecula", _ts(date.today(), 7))
        L = {s["sci"]: s for s in self.json("api/species")["species"]}
        self.assertEqual(L["Erithacus rubecula"]["n"], 1)
        self.assertIn("Bombycilla garrulus", L)
        self.assertNotIn("Turdus migratorius", L)
        D = self.json("api/species/erithacus-rubecula")
        self.assertEqual((D["n"], D["hours"][7], D["days"][-1]), (1, 1, 1))
        C_ = self.json(f"api/calendar?year={date.today().year}")
        self.assertEqual(C_["days"][date.today().isoformat()], [1, 1])

    def test_live(self):
        L = self.json("api/live?since=107")
        self.assertTrue(L["online"])
        import base64
        self.assertEqual(len(base64.b64decode(L["cols"])), 3 * 64)
        self.assertEqual(L["hearing"][0]["common"], "European Robin")


class Filters(Server):
    def test_held_and_rejected_detections_are_admin_only(self):
        now = time.time() - 60
        ok = self.add("Erithacus rubecula", now, verified="ok")
        multi = self.add("Erithacus rubecula", now + 1, verified="multi", hits=3)
        pend = self.add("Turdus merula", now + 2, verified="pending")
        bad = self.add("Turdus merula", now + 3, verified="failed", verify_score=0.04)
        sniff = self.add("Turdus merula", now + 4, verified="human")
        old = self.add("Turdus merula", now + 5)                  # from before the filters existed
        self.assertEqual({d["id"] for d in self.json("api/log")["items"]}, {ok, multi, old})
        self.assertEqual({d["id"] for d in self.json("api/log", admin=True)["items"]}, {ok, multi, old, pend})
        review = {d["id"]: d["verified"] for d in self.json("api/admin/review", admin=True)["items"]}
        self.assertEqual((review[bad], review[sniff]), ("failed", "human"))
        self.assertEqual(self.json("api/admin/status", admin=True)["unverified"], 2)
        status, _h, body = self.req(f"/api/admin/det/{sniff}", "POST", {"action": "confirm"}, {"X-Birds": "1"}, admin=True)
        self.assertEqual(json.loads(body)["det"]["verified"], "ok")
        self.assertIn(sniff, {d["id"] for d in self.json("api/log")["items"]})

    def test_upgrade_adds_columns(self):
        import sqlite3
        path = os.path.join(self.tmp, "old.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE detections (id INTEGER PRIMARY KEY, sci TEXT, start REAL, end REAL, day TEXT, "
                    "hour INTEGER, conf REAL, hits INTEGER, tier INTEGER, review TEXT, hidden INTEGER, saved INTEGER, "
                    "speech INTEGER, open INTEGER, clip TEXT, clip_bytes INTEGER, spectro TEXT)")
        con.commit()
        con.close()
        E.connect(path).close()
        cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(detections)")}
        self.assertTrue({"verified", "verify_score"} <= cols)


class Rules(Server):
    def test_levels_set_reset_and_saved(self):
        self.app.cfg["human_guard_species"] = ["Bombycilla garrulus"]
        r = self.json("api/admin/rule?sci=Erithacus%20rubecula", admin=True)
        self.assertEqual((r["level"], r["default"], r["noise_prone"]), (2, 2, False))
        self.assertTrue(self.json("api/admin/rule?sci=Bombycilla%20garrulus", admin=True)["noise_prone"])
        status, _h, body = self.req("/api/admin/rule", "POST", {"sci": "Erithacus rubecula", "level": 1},
                                    {"X-Birds": "1"}, admin=True)
        res = json.loads(body)
        self.assertEqual(res["rule"]["level"], 1)
        self.assertEqual({x["sci"]: x["level"] for x in res["rules"]}, {"Erithacus rubecula": 1, "Bombycilla garrulus": 3})
        self.assertEqual(C.load(self.tmp)["species_levels"], {"Erithacus rubecula": 1})   # saved to disk
        status, _h, body = self.req("/api/admin/rule", "POST", {"sci": "Erithacus rubecula", "level": None},
                                    {"X-Birds": "1"}, admin=True)
        self.assertEqual(json.loads(body)["rule"]["level"], 2)
        for bad in ({"sci": "Erithacus rubecula", "level": 7}, {"sci": "Erithacus rubecula", "level": True},
                    {"sci": "Made upus", "level": 2}):
            self.assertEqual(self.req("/api/admin/rule", "POST", bad, {"X-Birds": "1"}, admin=True)[0], 400)
        self.assertEqual(self.req("/api/admin/rule?sci=Erithacus%20rubecula")[0], 404)       # admin only

    def test_review_level_is_admin_only(self):
        d = self.add("Erithacus rubecula", time.time() - 30, verified="review")
        self.assertNotIn(d, {x["id"] for x in self.json("api/log")["items"]})
        self.assertIn(d, {x["id"] for x in self.json("api/admin/review", admin=True)["items"]})


class PauseAndSettings(Server):
    def test_pause_endpoints(self):
        h = {"X-Birds": "1"}
        self.assertEqual(self.req("/api/admin/pause", "POST", {"minutes": 30}, h, admin=True)[0], 503)  # no engine
        calls = []

        class P:
            def status(self): return {"paused": bool(calls and calls[-1] != "resume"), "until": None, "indefinite": True}
            def pause(self, m): calls.append(m); return self.status()
            def resume(self): calls.append("resume"); return self.status()
        self.app.live.pauser = P()
        self.assertEqual(self.req("/api/admin/pause", "POST", {"minutes": 30}, h)[0], 404)        # admin only
        st, _h, body = self.req("/api/admin/pause", "POST", {"minutes": 30}, h, admin=True)
        self.assertEqual((st, json.loads(body)["pause"]["paused"]), (200, True))
        self.assertEqual(self.req("/api/admin/pause", "POST", {"minutes": 99999999}, h, admin=True)[0], 400)
        self.req("/api/admin/pause", "POST", {"action": "resume"}, h, admin=True)
        self.assertEqual(calls, [30.0, "resume"])
        self.assertIn("pause", self.json("api/admin/status", admin=True))

    def test_paused_page_is_offline(self):
        self.app.live.snapshot = lambda since=0: dict(Live().snapshot(since), paused=True)
        L = self.json("api/live")
        self.assertEqual((L["online"], L["paused"], L["cols"]), (False, True, ""))

    def test_review_keep_days(self):
        h = {"X-Birds": "1"}
        st, _h, body = self.req("/api/admin/settings", "POST", {"review_keep_days": 7}, h, admin=True)
        self.assertEqual((st, json.loads(body)["review_keep_days"]), (200, 7))
        self.assertEqual(C.load(self.tmp)["review_keep_days"], 7)
        self.assertEqual(self.req("/api/admin/settings", "POST", {"review_keep_days": 999}, h, admin=True)[0], 400)
        self.assertEqual(self.json("api/admin/status", admin=True)["review_keep_days"], 7)

    def test_six_levels(self):
        h = {"X-Birds": "1"}
        self.app.cfg["species_levels"] = {"Turdus merula": 5}          # an old config: 5 was review all
        r = self.json("api/admin/rule?sci=Turdus%20merula", admin=True)
        self.assertEqual((r["level"], r["names"]["5"], r["names"]["6"]), (6, "Very strict", "Review all"))
        st, _h, body = self.req("/api/admin/rule", "POST", {"sci": "Erithacus rubecula", "level": 5}, h, admin=True)
        self.assertEqual(st, 200)
        saved = C.load(self.tmp)
        self.assertEqual((saved["species_levels"], saved["species_levels_scale"]),
                         ({"Turdus merula": 6, "Erithacus rubecula": 5}, 6))


class RoomAudioWeb(Server):
    def test_admin_only_and_unavailable(self):
        self.assertEqual(self.req("/live.mp3")[0], 404)                       # visitors: a plain 404
        self.assertEqual(self.req("/rec/rec-20261011-120000.mp3")[0], 404)
        self.assertEqual(self.req("/live.mp3", admin=True)[0], 503)          # no engine here
        self.assertEqual(self.req("/api/admin/recordings")[0], 404)
        r = self.json("api/admin/recordings", admin=True)
        self.assertTrue(r["status"]["unavailable"])
        st = self.json("api/admin/status", admin=True)
        self.assertFalse(st["listen_and_record"])

    def test_recordings_list_play_delete(self):
        d = os.path.join(self.tmp, "recordings")
        os.makedirs(d)
        frame = bytes([0xFF, 0xFB, 0xD0, 0x00])                             # MPEG-1 Layer III, 256 kbps
        for name in ("rec-20261011-120000.mp3", "rec-20261011-130000.mp3", "notes.txt"):
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(frame + b"\0" * 63996)                            # 64000 bytes = 2 s at 256 kbps
        self.app.live.recorder = mock_rec = type("R", (), {"status": lambda s: {"recording": False}})()
        self.app.live.listen = None
        r = self.json("api/admin/recordings", admin=True)
        self.assertEqual([f["name"] for f in r["files"]], ["rec-20261011-130000.mp3", "rec-20261011-120000.mp3"])
        self.assertEqual(r["files"][0]["seconds"], 2)
        st, h, body = self.req("/rec/rec-20261011-120000.mp3", headers={"Range": "bytes=0-9"}, admin=True)
        self.assertEqual((st, len(body)), (206, 10))
        self.assertEqual(self.req("/rec/notes.txt", admin=True)[0], 404)
        hdr = {"X-Birds": "1"}
        st, _h, body = self.req("/api/admin/recording", "POST", {"action": "delete", "name": "rec-20261011-120000.mp3"},
                                hdr, admin=True)
        self.assertEqual([f["name"] for f in json.loads(body)["files"]], ["rec-20261011-130000.mp3"])
        self.assertEqual(self.req("/api/admin/recording", "POST", {"action": "delete", "name": "../birds.db"},
                                  hdr, admin=True)[0], 400)
        self.assertEqual(self.req("/api/admin/recording", "POST", {"action": "start", "hours": 1}, hdr)[0], 404)
        del mock_rec

    def test_no_recording_while_stopped(self):
        class P:
            def status(self): return {"paused": True}
        self.app.live.pauser = P()
        self.app.live.recorder = object()
        st, _h, body = self.req("/api/admin/recording", "POST", {"action": "start", "hours": 1}, {"X-Birds": "1"}, admin=True)
        self.assertEqual(st, 400)
        self.assertIn("stopped", json.loads(body)["error"])


class SecretPath(Server):
    base_path = "/k3j9x2q8/hidden/"

    def test_only_the_secret_path_works(self):
        self.assertEqual(self.req("/")[0], 404)
        self.assertEqual(self.req("/birds.html")[0], 404)
        self.assertEqual(self.req("/api/info")[0], 404)
        self.assertEqual(self.req("/k3j9x2q8/")[0], 404)
        self.assertEqual(self.req(self.base_path)[0], 200)
        self.assertEqual(self.req(self.base_path + "birds.html")[0], 200)
        self.assertEqual(self.json("api/info")["title"], "Birdsong")
        # The 404 for a wrong path inside the mount is identical to one outside it.
        a = self.req(self.base_path + "nope")
        b = self.req("/nope")
        self.assertEqual((a[0], a[2]), (b[0], b[2]))


class Config(unittest.TestCase):
    def test_password_roundtrip_and_save(self):
        tmp = tempfile.mkdtemp()
        try:
            cfg = C.load(tmp)
            self.assertFalse(C.is_configured(cfg))
            C.set_password(cfg, "pw123456")
            cfg.update(latitude=51.5, longitude=-0.1)
            C.save(cfg, tmp)
            cfg2 = C.load(tmp)
            self.assertTrue(C.is_configured(cfg2))
            self.assertTrue(C.check_password(cfg2, "admin", "pw123456"))
            self.assertTrue(C.check_password(cfg2, "Admin ", "pw123456"))
            self.assertFalse(C.check_password(cfg2, "admin", "nope"))
            self.assertFalse(C.check_password(cfg2, "root", "pw123456"))
            self.assertEqual(cfg2["secret"], cfg["secret"])
            with open(C.config_path(tmp)) as fh:
                stored = json.load(fh)
            self.assertNotIn("min_conf", stored)            # defaults aren't frozen into the file
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_normalise_base(self):
        self.assertEqual(C.normalise_base(""), "/")
        self.assertEqual(C.normalise_base("a/b"), "/a/b/")
        self.assertEqual(C.normalise_base("/a/b/"), "/a/b/")


if __name__ == "__main__":
    unittest.main()
