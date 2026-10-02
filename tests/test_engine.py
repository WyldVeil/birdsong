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


class Events(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        e = E.Engine.__new__(E.Engine)
        e.cfg = dict(C.DEFAULTS)
        e.db = E.connect(os.path.join(self.tmp, "birds.db"))
        e.net = mock.Mock(sci=["Erithacus rubecula", "Turdus migratorius"], common=["European Robin", "American Robin"])
        e.tiers = np.array([1, 3], dtype=np.int8)
        e.events = {}
        e.ring = E.Ring(10)
        e.ring.push(np.zeros(E.SR * 5, dtype=np.int16), time.time())
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
