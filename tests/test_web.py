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
