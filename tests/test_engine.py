import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from birdsong import audio, config as C, demo, engine as E  # noqa: E402


class Pictures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_spectrogram_axis(self):
        t = np.arange(E.SR * 2) / E.SR
        img = E.spectrogram((np.sin(2 * np.pi * 4000 * t) * 8000).astype(np.int16))
        self.assertEqual(img.shape[0], 128)
        row = int(np.argmax(img.sum(axis=(1, 2))))
        self.assertAlmostEqual(row / 128, 1 - 4000 / 12000, delta=0.04)
        p = os.path.join(self.tmp, "a.png")
        E.write_png(p, img)
        with open(p, "rb") as fh:
            self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")

    def test_live_level_is_true_rms(self):
        rng = np.random.default_rng(1)
        x = rng.normal(0, 0.01, E.SR * 2).astype(np.float32)        # -40 dBFS white noise
        level = E.LiveSpectrum().feed(x)
        self.assertAlmostEqual(level, -40.0, delta=1.0)

    def test_live_columns_since(self):
        ls = E.LiveSpectrum()
        ls.feed(np.zeros(E.SR * 2, dtype=np.float32))
        seq, cols = ls.since(0)
        self.assertEqual(len(cols), seq * 64)
        seq2, cols2 = ls.since(seq - 3)
        self.assertEqual((seq2, len(cols2)), (seq, 3 * 64))

    def test_clip_encoding(self):
        song = demo.synth_song(3, 1)
        path = E.encode_clip(song, os.path.join(self.tmp, "c"))
        self.assertTrue(path and os.path.getsize(path) > 1000)
        self.assertTrue(path.endswith(".mp3"), path)

    def test_resample(self):
        x = (np.sin(2 * np.pi * 1000 * np.arange(4410) / 44100) * 10000).astype(np.int16)
        y = audio.resample(x, 44100, 48000)
        self.assertEqual(len(y), 4800)
        f = np.argmax(np.abs(np.fft.rfft(y))) * 48000 / len(y)
        self.assertAlmostEqual(f, 1000, delta=15)


class Retention(unittest.TestCase):
    def test_age_cap_and_saved(self):
        tmp = tempfile.mkdtemp()
        try:
            con = E.connect(os.path.join(tmp, "birds.db"))
            now = time.time()
            os.makedirs(os.path.join(tmp, "clips", "m"))

            def add(i, age, saved=0, size=1000):
                rel = f"clips/m/{i}.mp3"
                with open(os.path.join(tmp, rel), "wb") as fh:
                    fh.write(b"x" * size)
                con.execute("INSERT INTO detections(id, sci, start, end, day, hour, conf, tier, saved, clip, "
                            "clip_bytes, open) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                            (i, "a b", now - age * 86400, now, "d", 0, .9, 1, saved, rel, size))
            add(1, 40)
            add(2, 40, saved=1)
            add(3, 5, size=600_000)
            add(4, 1, size=600_000)
            con.commit()
            E.retention_pass(tmp, dict(C.DEFAULTS, clip_days=30, max_clip_mb=1), con, now)
            left = {i for (i,) in con.execute("SELECT id FROM detections WHERE clip IS NOT NULL")}
            self.assertEqual(left, {2, 4})
            con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ReviewPurge(unittest.TestCase):
    def test_old_held_back_items_cleared(self):
        tmp = tempfile.mkdtemp()
        try:
            con = E.connect(os.path.join(tmp, "birds.db"))
            now = time.time()
            os.makedirs(os.path.join(tmp, "clips"))

            def add(i, age, verified="", saved=0, tier=1, review="", hidden=0):
                rel = f"clips/{i}.mp3"
                open(os.path.join(tmp, rel), "wb").close()
                con.execute("INSERT INTO detections(id, sci, start, end, day, hour, conf, tier, saved, clip, open, "
                            "verified, review, hidden) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
                            (i, "a b", now - age * 86400, now, "d", 0, .9, tier, saved, rel, verified, review, hidden))
            add(1, 5, "review")
            add(2, 5, "failed", saved=1)                          # starred: kept
            add(3, 2, "human")                                    # too new
            add(4, 5, "ok")                                       # public: never purged
            add(5, 5, "pending", tier=3, review="pending")        # unconfirmed rarity
            add(6, 5, "multi", hidden=1)
            con.commit()
            self.assertEqual(E.purge_review(tmp, dict(C.DEFAULTS), con, now), 3)
            left = {i for (i,) in con.execute("SELECT id FROM detections")}
            self.assertEqual(left, {2, 3, 4})
            self.assertFalse(os.path.exists(os.path.join(tmp, "clips/1.mp3")))
            self.assertEqual(E.purge_review(tmp, dict(C.DEFAULTS, review_keep_days=0), con, now + 1e7), 0)
            con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Pause(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.eng = mock.Mock(spec=[])
        self.cap = mock.Mock(backend="sounddevice")
        self.cap.paused = __import__("threading").Event()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stop_resume_and_persist(self):
        p = E.Pauser(self.eng, self.cap, dict(C.DEFAULTS), self.tmp)
        self.assertIs(self.eng.pauser, p)
        self.assertFalse(p.status()["paused"])
        st = p.pause(30)
        self.assertTrue(st["paused"] and self.cap.paused.is_set())
        self.assertAlmostEqual(st["until"], time.time() + 1800, delta=5)
        p2 = E.Pauser(mock.Mock(spec=[]), self.cap, dict(C.DEFAULTS), self.tmp)     # after a restart
        self.assertTrue(p2.status()["paused"])
        p.resume()
        self.assertFalse(self.cap.paused.is_set())
        p.pause(0)
        self.assertTrue(p.status()["indefinite"])
        p.paused_until = time.time() - 1                      # the chosen time has passed
        stop = __import__("threading").Event()
        stop.set()
        p.run(stop)
        self.assertFalse(p.status()["paused"] or self.cap.paused.is_set())


class RoomAudio(unittest.TestCase):
    """Real ffmpeg (the bundled one): this also checks it works on each platform."""

    def setUp(self):
        if not E.ffmpeg_exe():
            self.skipTest("no ffmpeg")
        self.tmp = tempfile.mkdtemp()
        self.ring = E.Ring(40)
        self.cfg = dict(C.DEFAULTS, rec_min_free_gb=0)
        self.stop = __import__("threading").Event()
        t = np.arange(E.SR // 10) / E.SR

        def feed():                                         # 100 ms blocks of a 3 kHz whistle, in real time
            while not self.stop.wait(0.1):
                self.ring.push((8000 * np.sin(2 * np.pi * 3000 * t)).astype(np.int16), time.time())
        __import__("threading").Thread(target=feed, daemon=True).start()

    def tearDown(self):
        self.stop.set()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recording_makes_a_playable_mp3(self):
        from birdsong import web
        rec = E.RoomRecorder(self.ring, self.cfg, self.tmp)
        st = rec.start(1)
        self.assertTrue(st["ok"] and st["recording"], st)
        time.sleep(3)
        self.assertTrue(rec.stop()["ok"])
        self.assertFalse(rec.status()["recording"])
        files = os.listdir(os.path.join(self.tmp, "recordings"))
        self.assertEqual(len(files), 1)
        self.assertTrue(web.REC_NAME_RE.match(files[0]))
        path = os.path.join(self.tmp, "recordings", files[0])
        self.assertIn(web._mp3_seconds(path, os.path.getsize(path)), (2, 3, 4))
        self.assertEqual(rec._kv("rec_until"), "0")

    def test_restart_carries_on_recording(self):
        rec = E.RoomRecorder(self.ring, self.cfg, self.tmp)
        rec.start(1)
        time.sleep(1)
        rec.shutdown()                                       # Birdsong closing: keep the stop time
        rec2 = E.RoomRecorder(self.ring, self.cfg, self.tmp)
        time.sleep(1.1)                                      # a new part gets a new file name
        rec2.resume_if_needed()
        self.assertTrue(rec2.status()["recording"])
        rec2.stop()
        self.assertEqual(len(os.listdir(os.path.join(self.tmp, "recordings"))), 2)

    def test_listen_live_streams_mp3_and_pause_cuts_it(self):
        live = E.LiveListen(self.ring, self.cfg)
        q = live.subscribe()
        got = b""
        deadline = time.time() + 10
        while len(got) < 4000 and time.time() < deadline:
            try:
                got += q.get(timeout=1) or b""
            except Exception:
                pass
        self.assertGreater(len(got), 4000)
        self.assertTrue(got[:3] == b"ID3" or got[0] == 0xFF)    # an MP3 stream
        cap = mock.Mock(backend="sounddevice")
        cap.paused = __import__("threading").Event()
        rec = E.RoomRecorder(self.ring, self.cfg, self.tmp)
        rec.start(1)
        p = E.Pauser(mock.Mock(spec=[]), cap, self.cfg, self.tmp, live, rec)
        p.pause(30)
        self.assertEqual(live.count(), 0)
        self.assertFalse(rec.status()["recording"])
        self.assertIsNone(live.subscribe())                  # no new listeners while stopped
        p.resume()
        q2 = live.subscribe()
        self.assertIsNotNone(q2)
        live.unsubscribe(q2)


class Focal(unittest.TestCase):
    def test_finds_the_bird(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        tmp = tempfile.mkdtemp()
        try:
            a = np.full((300, 400, 3), (90, 140, 70), dtype=np.uint8)    # green background
            a[40:160, 260:360] = (30, 30, 35)                             # a dark bird, top right
            path = os.path.join(tmp, "b.jpg")
            Image.fromarray(a).save(path)
            fx, fy = E.photo_focal(path)
            self.assertTrue(65 <= fx <= 90, fx)
            self.assertTrue(10 <= fy <= 40, fy)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Events(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        e = E.Engine.__new__(E.Engine)
        e.cfg = dict(C.DEFAULTS)
        e.db = E.connect(os.path.join(self.tmp, "birds.db"))
        e.net = mock.Mock(sci=["Erithacus rubecula", "Turdus migratorius"], common=["European Robin", "American Robin"])
        e.net.predict = lambda x, sens=1.0: np.zeros(2, dtype=np.float32)     # re-check finds nothing
        e.sci_index = {"Erithacus rubecula": 0, "Turdus migratorius": 1}
        e.hnv_idx = 1                                                        # stub "Human non-vocal" slot
        e.human_idxs = {1}
        e.tiers = np.array([1, 3], dtype=np.int8)
        e.events = {}
        e.ring = E.Ring(40)
        e.ring.push(np.zeros(E.SR * 30, dtype=np.int16), time.time())
        e.clip_q = mock.Mock()
        self.e = e

    def tearDown(self):
        self.e.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def count(self):
        return self.e.db.execute("SELECT COUNT(*) FROM detections").fetchone()[0]

    def test_merge_and_split(self):
        e, t = self.e, time.time() - 100
        e._hit(0, 0.8, t, t + 3, 0)
        e._hit(0, 0.9, t + 1.5, t + 4.5, 0.4)
        e._hit(0, 0.75, t + 9, t + 12, 0)
        self.assertEqual(e.db.execute("SELECT COUNT(*), MAX(hits), MAX(conf) FROM detections").fetchone(), (1, 3, 0.9))
        e._hit(0, 0.8, t + 40, t + 43, 0)
        self.assertEqual(self.count(), 2)
        self.assertEqual(e.db.execute("SELECT open, speech FROM detections WHERE id=1").fetchone(), (0, 1))

    def test_long_song_split(self):
        e, t = self.e, time.time() - 200
        for k in range(30):
            e._hit(0, 0.8, t + k * 1.5, t + k * 1.5 + 3, 0)
        self.assertEqual(self.count(), 2)

    def verdict(self, det_id):
        return self.e.db.execute("SELECT verified FROM detections WHERE id=?", (det_id,)).fetchone()[0]

    def test_recheck_real_call_vs_noise(self):
        e, SR = self.e, E.SR
        audio = np.zeros(7 * SR, dtype=np.int16)
        audio[int(3.5 * SR)] = 30000                              # a short call mid-window
        e.net.predict = lambda x, sens=1.0: np.array([0.9 if np.abs(x).max() > 0.5 else 0.0, 0], dtype=np.float32)
        self.assertEqual(e._verify("Erithacus rubecula", audio, 2 * SR)[0], "ok")
        # A "hit" that only exists for the exact original framing (noise artefact):
        e.net.predict = lambda x, sens=1.0: np.array([0.9 if abs(x[0]) > 0.5 else 0.0, 0], dtype=np.float32)
        audio2 = np.zeros(7 * SR, dtype=np.int16)
        audio2[2 * SR] = 30000
        self.assertEqual(e._verify("Erithacus rubecula", audio2, 2 * SR), ("failed", 0.0))

    def test_single_window_pending_then_checked(self):
        e = self.e
        t = e.ring.time_of(e.ring.total) - 6
        e._hit(0, 0.9, t - 3, t, 0)
        self.assertEqual(self.verdict(1), "pending")
        e._close(e.events["Erithacus rubecula"])                  # stub re-check finds nothing
        self.assertEqual(self.verdict(1), "failed")
        e._hit(0, 0.9, t + 100, t + 103, 0)
        e._hit(0, 0.9, t + 101.5, t + 104.5, 0)
        self.assertEqual(self.verdict(2), "multi")                # heard twice: public at once

    def test_human_noise_guard(self):
        e = self.e
        e.cfg["human_guard_species"] = ["Erithacus rubecula"]      # pretend robins are owl-like
        t = e.ring.time_of(e.ring.total) - 6
        human = {"v": 0.6}
        e.net.predict = lambda x, sens=1.0: np.array([0.9, human["v"]], dtype=np.float32)
        e._hit(0, 0.9, t - 4.5, t - 1.5, 0)
        e._hit(0, 0.9, t - 3, t, 0)
        self.assertEqual(self.verdict(1), "pending")              # guarded: held even when heard twice
        e._close(e.events["Erithacus rubecula"])
        self.assertEqual(self.verdict(1), "human")
        human["v"] = 0.05
        e._hit(0, 0.9, t - 3, t, 0)
        e._close(e.events["Erithacus rubecula"])
        self.assertEqual(self.verdict(2), "ok")                   # no human noise, and it re-checks fine
        human["v"] = 0.95                                         # human is BirdNET's top guess...
        e.cfg["human_guard_threshold"] = 0.99                     # ...though under the score threshold
        e._hit(0, 0.9, t - 4.5, t - 1.5, 0)
        e._hit(0, 0.9, t - 3, t, 0)
        e._close(e.events["Erithacus rubecula"])
        self.assertEqual(self.verdict(3), "human")
        e.cfg["human_guard_threshold"] = 0.25
        e.cfg["human_guard_species"] = []                         # unguarded: never held for human noise
        human["v"] = 0.9
        e._hit(0, 0.9, t - 4.5, t - 1.5, 0)
        e._hit(0, 0.9, t - 3, t, 0)
        self.assertEqual(self.verdict(4), "multi")

    def test_species_levels(self):
        e = self.e
        t = e.ring.time_of(e.ring.total) - 6
        self.assertEqual(e._level("Erithacus rubecula"), 2)
        e.cfg["trusted_species"] = ["Erithacus rubecula"]
        e._hit(0, 0.72, t - 3, t, 0)                              # stub re-check would fail it...
        e._close(e.events["Erithacus rubecula"])
        self.assertEqual(self.verdict(1), "trusted")              # ...but trusted skips it
        e.cfg["trusted_species"] = []
        e.net.predict = lambda x, sens=1.0: np.array([0.9, 0.0], dtype=np.float32)    # re-check would pass
        e.cfg["species_levels"] = {"Erithacus rubecula": 4}       # strict: one window -> review
        e._hit(0, 0.9, t - 3, t, 0)
        e._close(e.events["Erithacus rubecula"])
        self.assertEqual(self.verdict(2), "review")
        e.cfg["species_levels"] = {"Erithacus rubecula": 5}       # review all: even two windows
        e._hit(0, 0.9, t - 4.5, t - 1.5, 0)
        e._hit(0, 0.9, t - 3, t, 0)
        e._close(e.events["Erithacus rubecula"])
        self.assertEqual(self.verdict(3), "review")
        e.cfg["species_levels"] = {}
        e.cfg["human_guard_species"] = ["Erithacus rubecula"]
        e.cfg["trusted_species"] = ["Erithacus rubecula"]
        self.assertEqual(e._level("Erithacus rubecula"), 3)      # guarded is never trusted by default

    def test_very_strict_needs_three_windows(self):
        e = self.e
        t = e.ring.time_of(e.ring.total) - 8
        e.net.predict = lambda x, sens=1.0: np.array([0.9, 0.0], dtype=np.float32)
        e.cfg["species_levels"], e.cfg["species_levels_scale"] = {"Erithacus rubecula": 5}, 6
        self.assertEqual(e._level("Erithacus rubecula"), 5)
        e._hit(0, 0.9, t - 4, t - 1, 0)                           # two windows: a keyboard burst
        e._hit(0, 0.9, t - 3, t, 0)
        e._close(e.events["Erithacus rubecula"])
        self.assertEqual(self.verdict(1), "review")
        for k in range(3):                                        # three in a row: a real bird
            e._hit(0, 0.9, t - 5 + k, t - 2 + k, 0)
        e._close(e.events["Erithacus rubecula"])
        self.assertEqual(self.verdict(2), "multi")
        e.cfg["species_levels_scale"] = None                      # a config from before v1.5: 5 meant review all
        self.assertEqual(e._level("Erithacus rubecula"), 6)

    def test_vagrant_rules(self):
        e, t = self.e, time.time() - 100
        e._hit(1, 0.97, t, t + 3, 0)
        self.assertEqual(self.count(), 0)
        e._hit(1, 0.98, t + 1.5, t + 4.5, 0)
        self.assertEqual(e.db.execute("SELECT tier, review FROM detections").fetchone(), (3, "pending"))


class BirdWeather(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = E.connect(os.path.join(self.tmp, "birds.db"))
        self.cfg = dict(C.DEFAULTS, birdweather_token="TOKEN123")
        self.names = {"Erithacus rubecula": "European Robin", "Turdus merula": "Eurasian Blackbird"}
        self.sent = []

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, sci, start, **kw):
        cols = dict(sci=sci, start=start, end=start + 3, day="2026-10-02", hour=12, conf=0.9, hits=2, tier=1, open=0)
        cols.update(kw)
        self.db.execute(f"INSERT INTO detections({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(cols.values()))
        self.db.commit()

    def ok(self, url, body=None, ctype=None):
        self.sent.append((url, body, ctype))
        if "/soundscapes" in url:
            return {"success": True, "soundscape": {"id": 77}}
        return {"success": True, "detection": {"id": 1}}

    def status(self):
        return [r[0] for r in self.db.execute("SELECT status FROM birdweather ORDER BY det_id")]

    def test_new_public_detections_only(self):
        import json
        t = 1_800_000_000.0
        E.birdweather_pass(self.tmp, self.db, self.cfg, self.names, now=t, request=self.ok)   # starts the clock
        self.add("Erithacus rubecula", t - 60)                          # earlier: not backfilled
        self.add("Turdus merula", t + 10, conf=0.8123)
        self.add("Erithacus rubecula", t + 20, hidden=1)
        self.add("Turdus migratorius", t + 30, tier=3, review="pending")
        self.add("Erithacus rubecula", t + 40, open=1)
        E.birdweather_pass(self.tmp, self.db, self.cfg, self.names, now=t + 100, request=self.ok)
        self.assertEqual(len(self.sent), 1)
        url, body, ctype = self.sent[0]
        self.assertTrue(url.endswith("/stations/TOKEN123/detections"))
        p = json.loads(body)
        self.assertEqual((p["commonName"], p["scientificName"], p["confidence"]), ("Eurasian Blackbird", "Turdus merula", 0.8123))
        self.assertNotIn("lat", p)
        self.assertNotIn("soundscapeId", p)
        E.birdweather_pass(self.tmp, self.db, self.cfg, self.names, now=t + 200, request=self.ok)
        self.assertEqual(len(self.sent), 1)

    def test_audio_is_flac_and_never_speech(self):
        import json
        t = 1_800_000_000.0
        self.cfg["birdweather_audio"] = True
        E.birdweather_pass(self.tmp, self.db, self.cfg, self.names, now=t, request=self.ok)
        os.makedirs(os.path.join(self.tmp, "clips", "m"))
        clip = E.encode_clip(demo.synth_song(4, 3), os.path.join(self.tmp, "clips", "m", "1"))
        rel = os.path.relpath(clip, self.tmp).replace(os.sep, "/")
        self.add("Turdus merula", t + 10, clip=rel)
        self.add("Turdus merula", t + 20, clip=rel, speech=1)
        E.birdweather_pass(self.tmp, self.db, self.cfg, self.names, now=t + 100, request=self.ok)
        kinds = [(u.rsplit("/", 1)[-1].split("?")[0], c) for u, _b, c in self.sent]
        self.assertEqual(kinds, [("soundscapes", "audio/flac"), ("detections", "application/json"),
                                 ("detections", "application/json")])
        self.assertEqual(self.sent[0][1][:4], b"fLaC")
        self.assertEqual(json.loads(self.sent[1][1])["soundscapeId"], 77)
        self.assertNotIn("soundscapeId", json.loads(self.sent[2][1]))      # speech: no audio

    def test_retry_fail_and_bad_token(self):
        t = 1_800_000_000.0
        E.birdweather_pass(self.tmp, self.db, self.cfg, self.names, now=t, request=self.ok)
        self.add("Turdus merula", t + 10)

        def down(*a, **k):
            raise E.BWError(0, "network: down")
        E.birdweather_pass(self.tmp, self.db, self.cfg, self.names, now=t + 100, request=down)
        self.assertEqual(self.status(), ["retry"])
        E.birdweather_pass(self.tmp, self.db, self.cfg, self.names, now=t + 1000, request=self.ok)
        self.assertEqual(self.status(), ["sent"])
        self.add("Turdus merula", t + 2000)

        def denied(*a, **k):
            raise E.BWError(401, "HTTP 401")
        self.assertTrue(E.birdweather_pass(self.tmp, self.db, self.cfg, self.names, now=t + 3000, request=denied)["auth"])

    def test_off_without_token(self):
        self.add("Turdus merula", 1.0)
        c = E.birdweather_pass(self.tmp, self.db, dict(C.DEFAULTS), self.names, request=self.ok)
        self.assertEqual((c["sent"], self.sent), (0, []))


class Demo(unittest.TestCase):
    def test_seed(self):
        tmp = tempfile.mkdtemp()
        try:
            demo.seed(tmp)
            con = E.connect(os.path.join(tmp, "birds.db"))
            n = con.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
            self.assertGreater(n, 1000)
            self.assertGreater(con.execute("SELECT COUNT(*) FROM detections WHERE clip IS NOT NULL").fetchone()[0], 0)
            con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
