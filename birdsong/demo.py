"""Demo mode: a believable few months of made-up detections, synthetic
spectrograms and clips, and a fake live waterfall - so you can see the page
without a microphone or the model. Photos still come from Wikipedia."""

import math
import os
import random
import threading
import time
from datetime import date, datetime, timedelta

import numpy as np

from . import engine as E

# (scientific, common, detections per day, when it sings)
SPECIES = [
    ("Erithacus rubecula", "European Robin", 14, "dawn"),
    ("Turdus merula", "Eurasian Blackbird", 11, "dawn"),
    ("Troglodytes troglodytes", "Eurasian Wren", 8, "dawn"),
    ("Columba palumbus", "Common Woodpigeon", 9, "day"),
    ("Pica pica", "Eurasian Magpie", 6, "day"),
    ("Corvus corone", "Carrion Crow", 5, "day"),
    ("Passer domesticus", "House Sparrow", 7, "day"),
    ("Parus major", "Great Tit", 6, "dawn"),
    ("Cyanistes caeruleus", "Eurasian Blue Tit", 5, "day"),
    ("Sturnus vulgaris", "European Starling", 4, "day"),
    ("Larus argentatus", "European Herring Gull", 3, "day"),
    ("Fringilla coelebs", "Common Chaffinch", 3, "dawn"),
    ("Carduelis carduelis", "European Goldfinch", 2.5, "day"),
    ("Prunella modularis", "Dunnock", 2, "dawn"),
    ("Aegithalos caudatus", "Long-tailed Tit", 1.6, "day"),
    ("Turdus philomelos", "Song Thrush", 1.4, "dawn"),
    ("Strix aluco", "Tawny Owl", 0.8, "night"),
    ("Coloeus monedula", "Western Jackdaw", 1.5, "day"),
    ("Dendrocopos major", "Great Spotted Woodpecker", 0.5, "day"),
    ("Regulus regulus", "Goldcrest", 0.6, "day"),
    ("Periparus ater", "Coal Tit", 0.7, "day"),
    ("Sylvia atricapilla", "Eurasian Blackcap", 0.3, "dawn"),
    ("Phylloscopus collybita", "Common Chiffchaff", 0.3, "dawn"),
    ("Pyrrhula pyrrhula", "Eurasian Bullfinch", 0.3, "dawn"),
    ("Motacilla alba", "White Wagtail", 0.4, "day"),
    ("Ardea cinerea", "Grey Heron", 0.25, "night"),
    ("Bombycilla garrulus", "Bohemian Waxwing", 0.08, "day"),
]
UNHEARD = [("Picus viridis", "European Green Woodpecker"), ("Sitta europaea", "Eurasian Nuthatch"),
           ("Certhia familiaris", "Eurasian Treecreeper"), ("Spinus spinus", "Eurasian Siskin"),
           ("Chloris chloris", "European Greenfinch"), ("Buteo buteo", "Common Buzzard"),
           ("Accipiter nisus", "Eurasian Sparrowhawk"), ("Corvus frugilegus", "Rook"),
           ("Apus apus", "Common Swift"), ("Hirundo rustica", "Barn Swallow")]
UNUSUAL = {"Bombycilla garrulus", "Ardea cinerea"}


def _hour(profile, rnd):
    if profile == "dawn":
        h = rnd.gauss(6.5, 1.5) if rnd.random() < .65 else rnd.gauss(17, 2.2)
    elif profile == "night":
        return rnd.choice([21, 22, 23, 0, 1, 2, 3, 4, 5])
    else:
        h = rnd.gauss(12, 3.5)
    return int(min(22, max(4, h)))


def synth_song(seconds: float, seed: int) -> np.ndarray:
    """A bird-ish song: frequency-modulated whistles and trills over soft noise."""
    rnd = np.random.default_rng(seed)
    n = int(seconds * E.SR)
    t = np.arange(n) / E.SR
    x = rnd.normal(0, 0.004, n)
    pos = 0.4
    while pos < seconds - 0.6:
        dur = rnd.uniform(0.08, 0.5)
        f0, f1 = rnd.uniform(2200, 6000), rnd.uniform(2200, 7500)
        a, b = int(pos * E.SR), int((pos + dur) * E.SR)
        tt = t[a:b] - pos
        f = f0 + (f1 - f0) * tt / dur + 400 * np.sin(2 * np.pi * rnd.uniform(8, 40) * tt)
        phase = 2 * np.pi * np.cumsum(f) / E.SR
        env = np.sin(np.pi * tt / dur) ** 2
        x[a:b] += 0.25 * env * (np.sin(phase) + 0.3 * np.sin(2 * phase))
        pos += dur + rnd.uniform(0.05, 0.6)
    return (np.clip(x, -1, 1) * 32767).astype(np.int16)


def seed(data: str) -> None:
    db = E.connect(os.path.join(data, "birds.db"))
    if db.execute("SELECT COUNT(*) FROM detections").fetchone()[0]:
        return
    print("Creating demo data…")
    for sci, common, _r, _p in SPECIES:
        db.execute("INSERT OR IGNORE INTO species(sci, common, tier, slug) VALUES (?,?,?,?)",
                   (sci, common, 2 if sci in UNUSUAL else 1, E.slugify(sci)))
    for sci, common in UNHEARD:
        db.execute("INSERT OR IGNORE INTO species(sci, common, tier, slug) VALUES (?,?,1,?)", (sci, common, E.slugify(sci)))
    db.execute("INSERT OR IGNORE INTO species(sci, common, tier, slug) VALUES "
               "('Turdus migratorius','American Robin',3,'turdus-migratorius')")
    # A handful of real-looking clips, reused across detections.
    month = "demo"
    os.makedirs(os.path.join(data, "spectro", month), exist_ok=True)
    os.makedirs(os.path.join(data, "clips", month), exist_ok=True)
    assets = []
    for k in range(6):
        song = synth_song(random.Random(k).choice([5, 7, 9, 12]), k)
        spec = f"spectro/{month}/s{k}.png"
        E.write_png(os.path.join(data, spec), E.spectrogram(song))
        clip = E.encode_clip(song, os.path.join(data, "clips", month, f"c{k}"))
        assets.append((spec, os.path.relpath(clip, data).replace(os.sep, "/") if clip else None,
                       os.path.getsize(clip) if clip else 0))
    rnd = random.Random(7)
    now, today, rows = time.time(), date.today(), []
    for back in range(90, -1, -1):
        d = today - timedelta(days=back)
        season = 1 + .25 * math.sin(back / 9)
        for sci, _c, rate, prof in SPECIES:
            if sci == "Bombycilla garrulus" and back > 40:
                continue
            for _ in range(sum(1 for _ in range(int(rate * 3 * season)) if rnd.random() < 1 / 3)):
                h = _hour(prof, rnd)
                ts = time.mktime(datetime(d.year, d.month, d.day, h, rnd.randrange(60), rnd.randrange(60)).timetuple())
                if ts > now - 60:
                    continue
                dur = rnd.choice([3, 3, 4.5, 6, 9, 13.5, 21])
                spec, clip, size = rnd.choice(assets)
                recent = back < 30
                rows.append((sci, ts, ts + dur, d.isoformat(), h, round(rnd.uniform(.7, .99), 3), int(dur / 1.5),
                             2 if sci in UNUSUAL else 1, spec, clip if recent else None, size if recent else 0))
    rows.sort(key=lambda r: r[1])
    db.executemany("INSERT INTO detections(sci, start, end, day, hour, conf, hits, tier, spectro, clip, clip_bytes, open) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,0)", rows)
    lt = time.localtime(now - 7200)
    db.execute("INSERT INTO detections(sci, start, end, day, hour, conf, hits, tier, review, open) "
               "VALUES ('Turdus migratorius',?,?,?,?,0.96,3,3,'pending',0)",
               (now - 7200, now - 7190, time.strftime("%Y-%m-%d", lt), lt.tm_hour))
    for back in range(91):
        dd = today - timedelta(days=back)
        secs = 86400 if back else now - time.mktime(dd.timetuple())
        db.execute("INSERT OR REPLACE INTO daily(day, windows, seconds) VALUES (?,?,?)", (dd.isoformat(), int(secs / 1.5), secs))
    db.commit()
    db.close()


class FakeLive:
    """Stands in for the engine: a waterfall of soft noise with the odd
    synthetic song, and a new made-up detection every minute or so."""

    def __init__(self, data: str):
        self.data = data
        self.spec = E.LiveSpectrum()
        self.started = time.time()
        self.level = -48.0
        self.ts = 0.0

    def run(self, stop: threading.Event):
        rnd = np.random.default_rng()
        lead = np.zeros(E.LiveSpectrum.N_FFT, dtype=np.float32)
        song, song_pos, next_det = None, 0, time.time() + 20
        db = E.connect(os.path.join(self.data, "birds.db"))
        while not stop.wait(0.5):
            n = E.SR // 2
            x = rnd.normal(0, 0.006, n).astype(np.float32)
            if song is None and rnd.random() < 0.08:
                song, song_pos = synth_song(4, int(rnd.integers(1 << 30))).astype(np.float32) / 32768, 0
            if song is not None:
                part = song[song_pos:song_pos + n]
                x[:len(part)] += part
                song_pos += n
                if song_pos >= len(song):
                    song = None
            self.level = self.spec.feed(np.concatenate([lead, x]))
            lead = x[-E.LiveSpectrum.N_FFT:]
            self.ts = time.time()
            if time.time() > next_det:
                next_det = time.time() + rnd.uniform(40, 90)
                sci = random.choice(SPECIES[:12])[0]
                spec, clip = random.choice(db.execute("SELECT spectro, clip FROM detections WHERE clip IS NOT NULL "
                                                      "LIMIT 50").fetchall() or [(None, None)])
                lt = time.localtime()
                db.execute("INSERT INTO detections(sci, start, end, day, hour, conf, hits, tier, spectro, clip, open) "
                           "VALUES (?,?,?,?,?,?,2,1,?,?,0)",
                           (sci, time.time() - 6, time.time() - 1.5, time.strftime("%Y-%m-%d", lt), lt.tm_hour,
                            round(float(rnd.uniform(.72, .98)), 3), spec, clip))
                db.commit()

    def snapshot(self, since: int = 0) -> dict:
        seq, cols = self.spec.since(since)
        return {"ts": self.ts, "started": self.started, "level_db": round(self.level, 1), "peak": 0.05,
                "clipped_at": None, "mic_error": None, "low_disk": False, "seq": seq, "bands": 64,
                "col_rate": 20, "cols": cols, "hearing": []}
