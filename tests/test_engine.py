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
