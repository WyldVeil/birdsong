"""The listening engine: BirdNET on overlapping 3-second windows.

BirdNET V2.4 is by the K. Lisa Yang Center for Conservation Bioacoustics
(Cornell Lab of Ornithology) and Chemnitz University of Technology; the
model is CC BY-NC-SA 4.0 and is downloaded on first run, not shipped here.

Each run of consecutive hits for one species becomes ONE detection (an
"event"): a row is written on the first hit, so the page shows it within
seconds, and extended while the bird keeps calling. When the event ends,
the audio around it is saved as an MP3 clip plus a spectrogram PNG.

Species are admitted in three tiers built from BirdNET's location model:
  1 expected - occurs at your location in some week of the year  -> min_conf
  2 unusual  - plausible within ~150 km                          -> unusual_conf
  3 vagrant  - any other bird; vagrant_conf AND two windows, and held as
               review='pending' (hidden from the public) until the admin
               confirms it
Non-bird classes (Human vocal, Dog, Engine, ...) are never logged. "Human
vocal" is used only to flag clips containing speech, whose spectrograms
are then shown to the admin only.
"""

import io
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime

import numpy as np

SR = 48000
WIN = 3 * SR
RING_S = 150

MODEL_FILES = {
    "BirdNET_GLOBAL_6K_V2.4_Model_FP32.tflite": 51726412,
    "BirdNET_GLOBAL_6K_V2.4_MData_Model_V2_FP16.tflite": 14770468,
    "BirdNET_GLOBAL_6K_V2.4_Labels_en_uk.txt": 259894,
}
MODEL_URL = "https://github.com/birdnet-team/BirdNET-Analyzer/raw/v1.5.1/birdnet_analyzer/{}"
MODEL_SUBDIR = {"BirdNET_GLOBAL_6K_V2.4_Labels_en_uk.txt": "labels/V2.4/"}

NON_SPECIES = frozenset([
    "Dog", "Engine", "Environmental", "Fireworks", "Gun", "Human non-vocal",
    "Human vocal", "Human whistle", "Noise", "Power tools", "Siren",
])

USER_AGENT = "Birdsong/1.0 (+https://github.com/WyldVeil/birdsong)"

log = logging.getLogger("birdsong.engine")


# ---------------------------------------------------------------- model files

def model_dir(data: str) -> str:
    return os.path.join(data, "model")


def model_present(data: str) -> bool:
    d = model_dir(data)
    return all(os.path.isfile(os.path.join(d, f)) and os.path.getsize(os.path.join(d, f)) == n
               for f, n in MODEL_FILES.items())


def download_model(data: str, progress=print) -> None:
    d = model_dir(data)
    os.makedirs(d, exist_ok=True)
    for name, size in MODEL_FILES.items():
        dst = os.path.join(d, name)
        if os.path.isfile(dst) and os.path.getsize(dst) == size:
            continue
        sub = MODEL_SUBDIR.get(name, "checkpoints/V2.4/")
        url = MODEL_URL.format(sub + name)
        progress(f"  downloading {name} ({size / 1e6:.0f} MB)…")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        tmp = dst + ".part"
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh, 1 << 20)
        if os.path.getsize(tmp) != size:
            os.unlink(tmp)
            raise RuntimeError(f"{name}: unexpected size, download corrupted?")
        os.replace(tmp, dst)


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS species (
    sci TEXT PRIMARY KEY, common TEXT NOT NULL, tier INTEGER NOT NULL DEFAULT 3,
    slug TEXT NOT NULL, photo INTEGER NOT NULL DEFAULT 0,
    photo_credit TEXT NOT NULL DEFAULT '', photo_license TEXT NOT NULL DEFAULT '',
    photo_page TEXT NOT NULL DEFAULT '', wiki_url TEXT NOT NULL DEFAULT '',
    extract TEXT NOT NULL DEFAULT '', checked REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS detections (
    id INTEGER PRIMARY KEY, sci TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
    day TEXT NOT NULL, hour INTEGER NOT NULL, conf REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1, tier INTEGER NOT NULL,
    review TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0,
    saved INTEGER NOT NULL DEFAULT 0, speech INTEGER NOT NULL DEFAULT 0,
    open INTEGER NOT NULL DEFAULT 1, clip TEXT, clip_bytes INTEGER NOT NULL DEFAULT 0, spectro TEXT,
    verified TEXT NOT NULL DEFAULT '',   -- pending | multi | ok | failed | human | '' (older rows)
    verify_score REAL
);
CREATE INDEX IF NOT EXISTS det_start ON detections(start);
CREATE INDEX IF NOT EXISTS det_day ON detections(day);
CREATE INDEX IF NOT EXISTS det_sci ON detections(sci, start);
CREATE TABLE IF NOT EXISTS birdweather (
    det_id INTEGER PRIMARY KEY, status TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0,
    remote_id INTEGER, error TEXT NOT NULL DEFAULT '', at REAL NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily (
    day TEXT PRIMARY KEY, windows INTEGER NOT NULL DEFAULT 0, seconds REAL NOT NULL DEFAULT 0
);
"""


def connect(db_file: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_file), exist_ok=True)
    con = sqlite3.connect(db_file, timeout=15, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.executescript(SCHEMA)
    migrate(con)
    return con


def migrate(con: sqlite3.Connection) -> None:
    """Add columns introduced after a database was created (upgrades)."""
    have = {r[1] for r in con.execute("PRAGMA table_info(detections)")}
    for col, decl in (("verified", "TEXT NOT NULL DEFAULT ''"), ("verify_score", "REAL")):
        if col not in have:
            con.execute(f"ALTER TABLE detections ADD COLUMN {col} {decl}")
    con.commit()


def slugify(sci: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sci.lower()).strip("-")


# ---------------------------------------------------------------- BirdNET

class BirdNET:
    def __init__(self, mdir: str):
        from ai_edge_litert.interpreter import Interpreter
        names = list(MODEL_FILES)
        self.it = Interpreter(os.path.join(mdir, names[0]), num_threads=1)
        self.inp = self.it.get_input_details()[0]["index"]
        self.out = self.it.get_output_details()[0]["index"]
        self.it.resize_tensor_input(self.inp, [1, WIN])
        self.it.allocate_tensors()
        self.meta = Interpreter(os.path.join(mdir, names[1]), num_threads=1)
        self.meta.allocate_tensors()
        self.meta_in = self.meta.get_input_details()[0]["index"]
        self.meta_out = self.meta.get_output_details()[0]["index"]
        self.sci, self.common = [], []
        with open(os.path.join(mdir, names[2]), encoding="utf-8") as fh:
            for line in fh:
                s, _, c = line.rstrip("\n").partition("_")
                self.sci.append(s)
                self.common.append(c or s)

    def predict(self, x: np.ndarray, sensitivity: float = 1.0) -> np.ndarray:
        self.it.set_tensor(self.inp, x.reshape(1, WIN).astype(np.float32))
        self.it.invoke()
        logits = self.it.get_tensor(self.out)[0]
        return 1.0 / (1.0 + np.exp(-sensitivity * np.clip(logits, -15, 15)))

    def occurrence(self, lat: float, lon: float, week: int) -> np.ndarray:
        self.meta.set_tensor(self.meta_in, np.array([[lat, lon, week]], dtype=np.float32))
        self.meta.invoke()
        return self.meta.get_tensor(self.meta_out)[0].copy()


def build_tiers(net: BirdNET, cfg: dict) -> np.ndarray:
    """0 = never log, 1 expected, 2 unusual, 3 vagrant (per label index)."""
    tiers = np.full(len(net.sci), 3, dtype=np.int8)
    lat, lon = cfg.get("latitude"), cfg.get("longitude")
    if lat is not None and lon is not None:
        lat, lon, r = float(lat), float(lon), float(cfg["unusual_radius_deg"])
        year = net.occurrence(lat, lon, -1)
        weekly = np.max([net.occurrence(lat, lon, w) for w in range(1, 49)], axis=0)
        near = np.max([net.occurrence(lat + a, lon + b * 1.6, -1)
                       for a in (-r, 0, r) for b in (-r, 0, r)], axis=0)
        local = np.maximum(year, weekly)
        tiers[np.maximum(local, near) >= float(cfg["unusual_threshold"])] = 2
        tiers[local >= float(cfg["expected_threshold"])] = 1
    else:
        tiers[:] = 2   # no location: every bird needs the "unusual" confidence
    for i, c in enumerate(net.common):
        if c in NON_SPECIES or net.sci[i] == c or net.sci[i].startswith(("Gryllus", "Miogryllus")):
            tiers[i] = 0
    return tiers


# ---------------------------------------------------------------- audio ring

class Ring:
    """Mono int16 ring buffer indexed by absolute sample number."""

    def __init__(self, seconds: int = RING_S):
        self.buf = np.zeros(seconds * SR, dtype=np.int16)
        self.total = 0
        self.lock = threading.Condition()
        self.clock_idx, self.clock_t = 0, time.time()
        self.consumed = 0

    def push(self, block: np.ndarray, now: float):
        n = len(block)
        if n == 0:
            return
        with self.lock:
            if n > len(self.buf):
                block, n = block[-len(self.buf):], len(self.buf)
            pos = self.total % len(self.buf)
            first = min(n, len(self.buf) - pos)
            self.buf[pos:pos + first] = block[:first]
            if first < n:
                self.buf[:n - first] = block[first:]
            self.total += n
            self.clock_idx, self.clock_t = self.total, now
            self.lock.notify_all()

    def time_of(self, idx: int) -> float:
        return self.clock_t - (self.clock_idx - idx) / SR

    def index_of(self, t: float) -> int:
        return self.clock_idx - int((self.clock_t - t) * SR)

    def get(self, a: int, b: int) -> np.ndarray:
        with self.lock:
            a = max(a, self.total - len(self.buf), 0)
            b = min(b, self.total)
            n = b - a
            if n <= 0:
                return np.zeros(0, dtype=np.int16)
            pos = a % len(self.buf)
            first = min(n, len(self.buf) - pos)
            out = np.empty(n, dtype=np.int16)
            out[:first] = self.buf[pos:pos + first]
            if first < n:
                out[first:] = self.buf[:n - first]
            return out


# ---------------------------------------------------------------- pictures

def _lut() -> np.ndarray:
    stops = [(0.00, (11, 20, 24)), (0.30, (20, 66, 76)), (0.55, (38, 128, 122)),
             (0.75, (214, 168, 74)), (0.90, (246, 214, 140)), (1.00, (255, 246, 222))]
    xs = np.linspace(0, 1, 256)
    out = np.zeros((256, 3), dtype=np.uint8)
    for c in range(3):
        out[:, c] = np.interp(xs, [s[0] for s in stops], [s[1][c] for s in stops]).astype(np.uint8)
    return out


LUT = _lut()


def write_png(path: str, rgb: np.ndarray):
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(png)
    os.replace(tmp, path)


def spectrogram(x: np.ndarray, fmax: int = 12000, rows: int = 128, px_per_s: int = 80) -> np.ndarray:
    """Linear-frequency spectrogram as RGB (high frequencies on top)."""
    n_fft, hop = 1024, SR // px_per_s
    x = x.astype(np.float32) / 32768.0
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    n = 1 + (len(x) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    mag = np.abs(np.fft.rfft(x[idx] * np.hanning(n_fft), axis=1))
    top, lo = int(fmax / (SR / n_fft)), 3
    edges = np.linspace(lo, top, rows + 1).astype(int)
    mag = np.add.reduceat(mag[:, :top], edges[:-1], axis=1) / np.maximum(np.diff(edges), 1)
    db = 20 * np.log10(mag + 1e-9)
    floor = np.percentile(db, 50) + 4
    peak = max(np.percentile(db, 99.7), floor + 30)
    v = np.clip((db - floor) / (peak - floor), 0, 1) ** 0.85
    return np.ascontiguousarray(LUT[(v * 255).astype(np.uint8)].transpose(1, 0, 2)[::-1])


class LiveSpectrum:
    """Rolling waterfall for the page: 64 log-spaced bands (0.3-12 kHz),
    20 columns/s, each band normalised to its own running noise floor."""

    BANDS, HOP, N_FFT, KEEP = 64, SR // 20, 2048, 600

    def __init__(self):
        freqs = np.fft.rfftfreq(self.N_FFT, 1 / SR)
        edges = np.geomspace(300, 12000, self.BANDS + 1)
        self.bins = []
        for i in range(self.BANDS):
            a = int(np.searchsorted(freqs, edges[i]))
            self.bins.append((a, max(int(np.searchsorted(freqs, edges[i + 1])), a + 1)))
        self.floor = None
        self.cols = []
        self.seq = 0
        self.win = np.hanning(self.N_FFT).astype(np.float32)
        self.win_sq = float((self.win ** 2).sum())
        self.hp_bin = int(np.searchsorted(freqs, 150))
        self.lock = threading.Lock()

    def feed(self, x: np.ndarray) -> float:
        """x: float audio = N_FFT samples of lead-in + the new audio. Returns
        the level above 150 Hz in dBFS (true RMS)."""
        powers = []
        start0 = self.HOP if len(x) > self.N_FFT + self.HOP else 0
        new = []
        for start in range(start0, len(x) - self.N_FFT + 1, self.HOP):
            spec = np.abs(np.fft.rfft(x[start:start + self.N_FFT] * self.win)) ** 2
            band = np.array([spec[a:b].mean() for a, b in self.bins])
            db = 10 * np.log10(band + 1e-14)
            if self.floor is None:
                self.floor = db.copy()
            down = db < self.floor
            self.floor = np.where(down, 0.8 * self.floor + 0.2 * db, 0.997 * self.floor + 0.003 * db)
            new.append(bytes((np.clip((db - self.floor - 3) / 30, 0, 1) * 255).astype(np.uint8)))
            powers.append(spec[self.hp_bin:].sum() * 2 / (self.N_FFT * self.win_sq))
        with self.lock:
            self.cols.extend(new)
            self.seq += len(new)
            if len(self.cols) > self.KEEP:
                del self.cols[:-self.KEEP]
        return float(10 * np.log10(np.mean(powers) + 1e-14)) if powers else -120.0

    def since(self, seq: int):
        with self.lock:
            have = len(self.cols)
            want = have if seq <= 0 or seq > self.seq else min(have, self.seq - seq)
            return self.seq, b"".join(self.cols[have - want:]) if want else b""


# ---------------------------------------------------------------- the engine

class Event:
    __slots__ = ("id", "sci", "start", "end", "conf", "hits", "tier", "human", "inserted")

    def __init__(self, sci, start, end, conf, tier, human):
        self.id = None
        self.sci, self.start, self.end, self.conf = sci, start, end, conf
        self.hits, self.tier, self.human, self.inserted = 1, tier, human, False


class Engine:
    def __init__(self, data: str, cfg: dict, net: BirdNET = None, live_clock: bool = True):
        self.data, self.cfg = data, cfg
        self.net = net or BirdNET(model_dir(data))
        self.ring = Ring()
        self.live_clock = live_clock
        self.db = connect(os.path.join(data, "birds.db"))
        self.tiers = build_tiers(self.net, cfg)
        self.thresh = np.where(self.tiers == 1, cfg["min_conf"],
                      np.where(self.tiers == 2, cfg["unusual_conf"],
                      np.where(self.tiers == 3, cfg["vagrant_conf"], 9.0))).astype(np.float32)
        self.human_idx = self.net.common.index("Human vocal")
        self.hnv_idx = self.net.common.index("Human non-vocal")
        self.sci_index = {s: i for i, s in enumerate(self.net.sci)}
        self.events = {}
        self.live = LiveSpectrum()
        self.status = {"started": time.time(), "level_db": None, "peak": 0.0}
        self.windows_today, self.day = 0, None
        self.low_disk = False
        self.last_tick = 0.0
        self.clip_q = queue.Queue()
        self.stop_event = threading.Event()
        self._seed_species()
        self.writer = threading.Thread(target=self._clip_worker, name="clips", daemon=True)
        self.writer.start()
        n = np.bincount(self.tiers, minlength=4)
        log.info(f"species: {n[1]} expected here, {n[2]} unusual, {n[3]} others (rarity review)")

    # --- species table
    def _seed_species(self):
        rows = [(self.net.sci[i], self.net.common[i], int(t), slugify(self.net.sci[i]))
                for i, t in enumerate(self.tiers) if t in (1, 2)]
        self.db.executemany("INSERT INTO species(sci, common, tier, slug) VALUES (?,?,?,?) "
                            "ON CONFLICT(sci) DO UPDATE SET common=excluded.common, tier=excluded.tier", rows)
        keep = {r[0] for r in rows}
        for (sci,) in self.db.execute("SELECT sci FROM species WHERE tier<3").fetchall():
            if sci not in keep:
                self.db.execute("UPDATE species SET tier=3 WHERE sci=?", (sci,))
        self.db.commit()

    # --- the loop
    def run(self, finished: threading.Event = None):
        step = int(float(self.cfg["step_s"]) * SR)
        next_end = live_from = None
        ring = self.ring
        while not self.stop_event.is_set():
            with ring.lock:
                if next_end is None and ring.total >= WIN:
                    next_end = ring.total
                    live_from = max(0, next_end - step)
                if next_end is None or ring.total < next_end:
                    if finished is not None and finished.is_set():
                        break
                    ring.lock.wait(0.5)
                    continue
                if self.live_clock and ring.total - next_end > 20 * SR:
                    log.warning("analysis fell behind; skipping ahead")
                    next_end = ring.total
                    live_from = next_end - step
            x16 = ring.get(next_end - WIN, next_end)
            ring.consumed = next_end - 60 * SR
            t_end = ring.time_of(next_end)
            self._window(x16.astype(np.float32) / 32768.0, t_end)
            span = ring.get(max(0, live_from - LiveSpectrum.N_FFT), next_end).astype(np.float32) / 32768.0
            level = self.live.feed(span)
            peak = float(np.abs(x16[-step:]).max()) / 32768.0 if len(x16) else 0.0
            self._tick(t_end, level, peak)
            live_from = next_end
            next_end += step
        self.close_all()

    def stop(self):
        self.stop_event.set()
        with self.ring.lock:
            self.ring.lock.notify_all()

    def _window(self, x: np.ndarray, t_end: float):
        t_start = t_end - WIN / SR
        day = time.strftime("%Y-%m-%d", time.localtime(t_end))
        if day != self.day:
            self.day = day
            row = self.db.execute("SELECT windows FROM daily WHERE day=?", (day,)).fetchone()
            self.windows_today = row[0] if row else 0
        self.windows_today += 1
        p = self.net.predict(x, float(self.cfg["sensitivity"]))
        human = float(p[self.human_idx])
        for i in np.where(p >= self.thresh)[0]:
            self._hit(int(i), float(p[i]), t_start, t_end, human)
        for ev in self.events.values():
            if t_start <= ev.end + 1.0:
                ev.human = max(ev.human, human)
        self._close_stale(t_end)

    def _hit(self, i: int, conf: float, t0: float, t1: float, human: float):
        sci = self.net.sci[i]
        ev = self.events.get(sci)
        if ev and t0 <= ev.end + float(self.cfg["merge_gap_s"]) and t1 - ev.start <= float(self.cfg["max_event_s"]):
            ev.end, ev.hits = t1, ev.hits + 1
            ev.conf, ev.human = max(ev.conf, conf), max(ev.human, human)
            self._upsert(ev, i)
            return
        if ev:
            self._close(ev)
        ev = self.events[sci] = Event(sci, t0, t1, conf, int(self.tiers[i]), human)
        self._upsert(ev, i)
        log.info(f"heard {self.net.common[i]} ({sci}) {conf:.0%}")

    def _upsert(self, ev: Event, i: int):
        if ev.tier == 3 and ev.hits < 2:
            return
        # "pending" = not public yet: heard in one window so far (re-checked when
        # it closes), or a human-noise-guarded species (checked when it closes).
        guarded = self._guarded(ev.sci)
        held = (ev.hits < 2 and self.cfg.get("verify_single", True)) or guarded
        if not ev.inserted:
            self.db.execute("INSERT OR IGNORE INTO species(sci, common, tier, slug) VALUES (?,?,?,?)",
                            (ev.sci, self.net.common[i], ev.tier, slugify(ev.sci)))
            lt = time.localtime(ev.start)
            cur = self.db.execute(
                "INSERT INTO detections(sci, start, end, day, hour, conf, hits, tier, review, verified) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ev.sci, ev.start, ev.end, time.strftime("%Y-%m-%d", lt), lt.tm_hour, round(ev.conf, 4),
                 ev.hits, ev.tier, "pending" if ev.tier == 3 else "", "pending" if held else "multi"))
            ev.id, ev.inserted = cur.lastrowid, True
        else:
            self.db.execute("UPDATE detections SET end=?, conf=?, hits=?, verified=CASE WHEN ?>=2 AND "
                            "verified='pending' AND ?=0 THEN 'multi' ELSE verified END WHERE id=?",
                            (ev.end, round(ev.conf, 4), ev.hits, ev.hits, int(guarded), ev.id))
        self.db.commit()

    def _close_stale(self, now: float):
        gap = float(self.cfg["merge_gap_s"]) + float(self.cfg["clip_pad_s"])
        for ev in [e for e in self.events.values() if now - e.end > gap]:
            self._close(ev)

    def _close(self, ev: Event):
        self.events.pop(ev.sci, None)
        if not ev.inserted:
            return
        speech = ev.human >= float(self.cfg["speech_threshold"])
        pad = float(self.cfg["clip_pad_s"])
        a = self.ring.index_of(ev.start - pad)
        a_eff = max(a, self.ring.total - len(self.ring.buf), 0)       # get() clamps to what's left
        audio = self.ring.get(a, self.ring.index_of(ev.end + pad))
        verified, vscore = None, None
        hnv = self._human_noise(audio) if self._guarded(ev.sci) else None
        if hnv is not None and hnv >= float(self.cfg.get("human_guard_threshold", 0.25)):
            verified = "human"
            log.info(f"human-noise guard held back {self.net.common[self.sci_index[ev.sci]]} "
                     f"(human non-vocal {hnv:.2f})")
        elif hnv is not None and ev.hits >= 2:
            verified = "multi"
        elif ev.hits < 2 and self.cfg.get("verify_single", True):
            verified, vscore = self._verify(ev.sci, audio, self.ring.index_of(ev.start) - a_eff)
            log.info(f"re-check {self.net.common[self.sci_index[ev.sci]]} {ev.conf:.0%}: {verified} "
                     f"(best shifted score {vscore:.2f})")
        self.db.execute("UPDATE detections SET open=0, speech=?, verified=COALESCE(?, verified), "
                        "verify_score=COALESCE(?, verify_score) WHERE id=?",
                        (int(speech), verified, None if vscore is None else round(vscore, 3), ev.id))
        self.db.commit()
        if len(audio) >= SR:
            self.clip_q.put((ev.id, ev.start, audio))

    def _guarded(self, sci: str) -> bool:
        return sci in set(self.cfg.get("human_guard_species") or ())

    def _verify(self, sci: str, audio: np.ndarray, orig: int):
        """Re-score `sci` with the 3 s window shifted +/-0.25..1.0 s from `orig`
        (the detecting window's start, in samples). A real call is still there
        when the window moves; a hit on background noise vanishes."""
        i = self.sci_index.get(sci)
        x = audio.astype(np.float32) / 32768.0
        sens = float(self.cfg["sensitivity"])
        scores = [float(self.net.predict(x[s:s + WIN], sens)[i])
                  for s in (orig + k * SR // 4 for k in (-4, -3, -2, -1, 1, 2, 3, 4))
                  if i is not None and 0 <= s and s + WIN <= len(x)]
        good = sum(v >= float(self.cfg.get("verify_min_score", 0.5)) for v in scores)
        return ("ok" if good >= int(self.cfg.get("verify_min_windows", 2)) else "failed"), max(scores, default=0.0)

    def _human_noise(self, audio: np.ndarray) -> float:
        """Max BirdNET "Human non-vocal" score over the clip (3 s windows every 0.5 s)."""
        x = audio.astype(np.float32) / 32768.0
        if len(x) < WIN:
            x = np.pad(x, (0, WIN - len(x)))
        sens = float(self.cfg["sensitivity"])
        return max(float(self.net.predict(x[s:s + WIN], sens)[self.hnv_idx])
                   for s in range(0, len(x) - WIN + 1, SR // 2))

    def close_all(self):
        for ev in list(self.events.values()):
            self._close(ev)
        self.clip_q.put(None)
        self.writer.join(timeout=60)

    # --- clips + spectrograms, off the analysis thread
    def _clip_worker(self):
        db = connect(os.path.join(self.data, "birds.db"))
        while True:
            item = self.clip_q.get()
            if item is None:
                return
            try:
                write_clip(self.data, db, *item, low_disk=self.low_disk)
            except Exception as e:
                log.exception(f"clip {item[0]} failed: {e}")

    # --- live state for the page
    def _tick(self, t_end: float, level: float, peak: float):
        st = self.status
        st["level_db"] = round(max(-120.0, level), 1)
        st["peak"] = round(peak, 4)
        st["ts"] = time.time()
        if peak >= 0.999:
            st["clipped_at"] = t_end
        if time.time() - self.last_tick >= 5 or not self.live_clock:
            self.last_tick = time.time()
            self.db.execute("INSERT INTO daily(day, windows, seconds) VALUES (?,?,?) "
                            "ON CONFLICT(day) DO UPDATE SET windows=excluded.windows, seconds=excluded.seconds",
                            (self.day, self.windows_today, self.windows_today * float(self.cfg["step_s"])))
            self.db.commit()

    def snapshot(self, since: int = 0) -> dict:
        seq, cols = self.live.since(since)
        st = self.status
        return {
            "ts": st.get("ts", 0), "started": st["started"], "level_db": st.get("level_db"),
            "peak": st.get("peak"), "clipped_at": st.get("clipped_at"), "mic_error": st.get("mic_error"),
            "low_disk": self.low_disk, "seq": seq, "bands": LiveSpectrum.BANDS, "col_rate": 20, "cols": cols,
            "hearing": [{"sci": e.sci, "conf": round(e.conf, 3), "since": e.start}
                        for e in list(self.events.values())
                        if e.inserted and e.tier < 3 and e.hits >= 2 and not self._guarded(e.sci)],
        }


def write_clip(data: str, db, det_id: int, start: float, audio: np.ndarray, low_disk: bool = False):
    month = time.strftime("%Y-%m", time.localtime(start))
    os.makedirs(os.path.join(data, "spectro", month), exist_ok=True)
    rel_spec = f"spectro/{month}/{det_id}.png"
    write_png(os.path.join(data, rel_spec), spectrogram(audio))
    rel_clip, size = None, 0
    if not low_disk:
        os.makedirs(os.path.join(data, "clips", month), exist_ok=True)
        rel_clip = encode_clip(audio, os.path.join(data, "clips", month, str(det_id)))
        if rel_clip:
            size = os.path.getsize(rel_clip)
            rel_clip = os.path.relpath(rel_clip, data).replace(os.sep, "/")
    db.execute("UPDATE detections SET clip=?, clip_bytes=?, spectro=? WHERE id=?", (rel_clip, size, rel_spec, det_id))
    db.commit()


def encode_clip(audio: np.ndarray, base: str):
    """MP3 via libsndfile (bundled in the soundfile wheel), WAV as a fallback."""
    import soundfile as sf
    # High-pass at 120 Hz so house rumble doesn't dominate the clip.
    x = audio.astype(np.float32) / 32768.0
    spec = np.fft.rfft(x)
    spec[np.fft.rfftfreq(len(x), 1 / SR) < 120] = 0
    x = np.fft.irfft(spec, len(x)).astype(np.float32)
    for ext, kw in ((".mp3", {"format": "MP3", "subtype": "MPEG_LAYER_III"}), (".wav", {"format": "WAV", "subtype": "PCM_16"})):
        path = base + ext
        try:
            sf.write(path + ".part", x, SR, **kw)
            os.replace(path + ".part", path)
            return path
        except Exception as e:
            log.warning(f"could not write {ext} clip: {e}")
            try:
                os.unlink(path + ".part")
            except OSError:
                pass
    return None


# ---------------------------------------------------------------- housekeeping

def retention_pass(data: str, cfg: dict, db, now: float = None) -> dict:
    now = now or time.time()
    removed = {"clips": 0, "spectro": 0, "bytes": 0}

    def rm(rel):
        try:
            p = os.path.join(data, rel)
            removed["bytes"] += os.path.getsize(p)
            os.unlink(p)
        except OSError:
            pass

    for det_id, rel in db.execute("SELECT id, clip FROM detections WHERE clip IS NOT NULL AND saved=0 AND start<?",
                                  (now - float(cfg["clip_days"]) * 86400,)).fetchall():
        rm(rel)
        db.execute("UPDATE detections SET clip=NULL, clip_bytes=0 WHERE id=?", (det_id,))
        removed["clips"] += 1
    for det_id, rel in db.execute("SELECT id, spectro FROM detections WHERE spectro IS NOT NULL AND saved=0 AND start<?",
                                  (now - float(cfg["spectro_days"]) * 86400,)).fetchall():
        rm(rel)
        db.execute("UPDATE detections SET spectro=NULL WHERE id=?", (det_id,))
        removed["spectro"] += 1
    cap = float(cfg["max_clip_mb"]) * 1024 * 1024
    total = db.execute("SELECT COALESCE(SUM(clip_bytes),0) FROM detections WHERE clip IS NOT NULL").fetchone()[0]
    if total > cap:
        for det_id, rel, n in db.execute("SELECT id, clip, clip_bytes FROM detections WHERE clip IS NOT NULL "
                                         "AND saved=0 ORDER BY start").fetchall():
            if total <= cap:
                break
            rm(rel)
            db.execute("UPDATE detections SET clip=NULL, clip_bytes=0 WHERE id=?", (det_id,))
            total -= n
            removed["clips"] += 1
    db.commit()
    for sub in ("clips", "spectro"):
        root = os.path.join(data, sub)
        if os.path.isdir(root):
            for d in os.listdir(root):
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
    return removed


def housekeeping(engine: Engine, stop: threading.Event):
    db = connect(os.path.join(engine.data, "birds.db"))
    while not stop.is_set():
        try:
            engine.low_disk = shutil.disk_usage(engine.data).free < float(engine.cfg["min_free_gb"]) * 1024 ** 3
            r = retention_pass(engine.data, engine.cfg, db)
            if r["clips"] or r["spectro"]:
                log.info(f"retention: removed {r['clips']} clips, {r['spectro']} spectrograms "
                         f"({r['bytes'] / 1e6:.1f} MB)")
        except Exception as e:
            log.exception(f"housekeeping failed: {e}")
        stop.wait(900)


# ---------------------------------------------------------------- photos

def _get(url: str, timeout: int = 20) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as r:
        return r.read()


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()[:200]


def fetch_species_info(data: str, sci: str, slug: str) -> dict:
    """Wikipedia summary + the article's lead photo from Commons (two sizes) + credit."""
    info = json.loads(_get("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(sci.replace(" ", "_"))))
    out = {"extract": (info.get("extract") or "")[:1200],
           "wiki_url": (info.get("content_urls") or {}).get("desktop", {}).get("page", ""),
           "photo": 0, "photo_credit": "", "photo_license": "", "photo_page": ""}
    orig = (info.get("originalimage") or {}).get("source", "").split("?", 1)[0]
    if not orig:
        return out
    parts = orig.split("/")      # .../File.jpg  or  .../thumb/a/ab/File.jpg/3840px-File.jpg
    fname = urllib.parse.unquote(parts[-2] if "/thumb/" in orig else parts[-1])
    q = urllib.parse.urlencode({"action": "query", "format": "json", "prop": "imageinfo",
                                "iiprop": "url|extmetadata", "iiurlwidth": 960, "titles": "File:" + fname})
    meta = json.loads(_get("https://commons.wikimedia.org/w/api.php?" + q))
    page = next(iter((meta.get("query") or {}).get("pages", {}).values()), {})
    ii = (page.get("imageinfo") or [{}])[0]
    big = ii.get("thumburl")
    if not big:
        raise ValueError(f"no Commons thumbnail for {fname}")
    em = ii.get("extmetadata") or {}
    out.update(photo_credit=_strip_html((em.get("Artist") or {}).get("value", "")),
               photo_license=_strip_html((em.get("LicenseShortName") or {}).get("value", "")),
               photo_page=ii.get("descriptionurl", ""))
    pdir = os.path.join(data, "photos")
    os.makedirs(pdir, exist_ok=True)
    for url, name in ((big, f"{slug}.jpg"), (re.sub(r"/\d+px-", "/330px-", big), f"{slug}_sm.jpg")):
        blob = _get(url, 40)
        if blob[:3] != b"\xff\xd8\xff" and blob[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"not an image: {url}")
        with open(os.path.join(pdir, name + ".tmp"), "wb") as fh:
            fh.write(blob)
        os.replace(os.path.join(pdir, name + ".tmp"), os.path.join(pdir, name))
        time.sleep(1)
    out["photo"] = 1
    return out


def photo_fetcher(data: str, stop: threading.Event):
    """Fill in photos and descriptions, heard species first, one at a time
    (Wikimedia API etiquette: descriptive User-Agent, serial, paused)."""
    db = connect(os.path.join(data, "birds.db"))
    while not stop.is_set():
        row = db.execute(
            "SELECT s.sci, s.slug FROM species s WHERE s.photo=0 AND s.checked < ? ORDER BY "
            "(SELECT COUNT(*) FROM detections d WHERE d.sci=s.sci) DESC, s.tier, s.common LIMIT 1",
            (time.time() - 7 * 86400,)).fetchone()
        if not row:
            stop.wait(600)
            continue
        sci, slug = row
        try:
            info = fetch_species_info(data, sci, slug)
            db.execute("UPDATE species SET photo=?, photo_credit=?, photo_license=?, photo_page=?, wiki_url=?, "
                       "extract=?, checked=? WHERE sci=?",
                       (info["photo"], info["photo_credit"], info["photo_license"], info["photo_page"],
                        info["wiki_url"], info["extract"], time.time(), sci))
        except Exception as e:
            db.execute("UPDATE species SET checked=? WHERE sci=?", (time.time(), sci))
            log.debug(f"photo for {sci} failed: {e}")
        db.commit()
        stop.wait(3)


# ---------------------------------------------------------------- BirdWeather
#
# Optional: stream detections to a BirdWeather station (app.birdweather.com),
# the public map of BirdNET-powered listening stations. Each new
# detection is posted once its bird stops singing. No coordinates are sent:
# BirdWeather uses the station's own map location, which you choose when
# creating it. Audio is off unless birdweather_audio is true, and clips that
# contain speech are never sent. Hidden detections and unconfirmed rarities
# are never sent. Only detections made after it was switched on are sent.

BW_API = "https://app.birdweather.com/api/v1/stations/{token}{what}"
BW_MAX_TRIES = 12


class BWError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def bw_request(url: str, body=None, ctype: str = "application/json") -> dict:
    """GET (body None) or POST, returning the JSON reply. The token is part of
    the URL, so no error message ever includes the URL."""
    req = urllib.request.Request(url, data=body, method="GET" if body is None else "POST",
                                 headers={"Content-Type": ctype, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:200].decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise BWError(e.code, f"HTTP {e.code} {detail}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BWError(0, f"network: {getattr(e, 'reason', e)}") from None


def bw_station(token: str) -> dict:
    """The station a token belongs to (used by setup to confirm the token)."""
    return bw_request(BW_API.format(token=urllib.parse.quote(token, safe=""), what=""))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds")


def _flac(data: str, rel_clip: str):
    """BirdWeather only accepts FLAC: re-encode the saved clip in memory."""
    import soundfile as sf
    try:
        x, rate = sf.read(os.path.join(data, rel_clip), dtype="float32")
        buf = io.BytesIO()
        sf.write(buf, x, rate, format="FLAC")
        return buf.getvalue()
    except Exception as e:
        log.debug(f"flac encode failed for {rel_clip}: {e}")
        return None


def birdweather_pass(data: str, db, cfg: dict, names: dict, now: float = None, request=bw_request,
                     limit: int = 20) -> dict:
    """Send what's due, once. Returns counts. `request` is swappable for tests."""
    now = now or time.time()
    token = (cfg.get("birdweather_token") or "").strip()
    counts = {"sent": 0, "retry": 0, "failed": 0, "auth": False}
    if not token:
        return counts
    base = BW_API.format(token=urllib.parse.quote(token, safe=""), what="")
    row = db.execute("SELECT v FROM kv WHERE k='birdweather_since'").fetchone()
    if row is None:
        db.execute("INSERT INTO kv(k, v) VALUES ('birdweather_since', ?)", (str(now),))
        db.commit()
        since = now
    else:
        since = float(row[0])
    cols = [c[0] for c in db.execute("SELECT * FROM detections LIMIT 0").description] + ["bw_tries"]
    due = db.execute(
        "SELECT d.*, b.tries FROM detections d LEFT JOIN birdweather b ON b.det_id=d.id "
        "WHERE d.open=0 AND d.start>=? AND d.hidden=0 AND (d.tier<3 OR d.review='confirmed') "
        "AND d.verified NOT IN ('pending','failed','human') "
        "AND (b.det_id IS NULL OR (b.status='retry' AND b.next_try<=?)) ORDER BY d.start LIMIT ?",
        (since, now, limit)).fetchall()
    pad = float(cfg.get("clip_pad_s", 2.0))
    for values in due:
        d = dict(zip(cols, values))
        tries = (d["bw_tries"] or 0) + 1
        payload = {"timestamp": _iso(d["start"]), "commonName": names.get(d["sci"], d["sci"]),
                   "scientificName": d["sci"], "confidence": round(float(d["conf"]), 4)}
        try:
            if cfg.get("birdweather_audio") and d["clip"] and not d["speech"]:
                audio = _flac(data, d["clip"])
                if audio:
                    sc = request(base + "/soundscapes?timestamp=" + urllib.parse.quote(_iso(d["start"] - pad)),
                                 audio, "audio/flac")
                    sid = (sc.get("soundscape") or {}).get("id")
                    if sid:
                        payload.update(soundscapeId=sid, soundscapeStartTime=pad,
                                       soundscapeEndTime=round(pad + d["end"] - d["start"], 2))
            res = request(base + "/detections", json.dumps(payload).encode(), "application/json")
            if not res.get("success", True):
                raise BWError(422, f"rejected: {json.dumps(res)[:150]}")
            db.execute("INSERT OR REPLACE INTO birdweather(det_id, status, tries, remote_id, error, at, next_try) "
                       "VALUES (?, 'sent', ?, ?, '', ?, 0)", (d["id"], tries, (res.get("detection") or {}).get("id"), now))
            counts["sent"] += 1
            log.info(f"BirdWeather: sent {payload['commonName']}")
        except BWError as e:
            if e.status in (401, 403, 404):
                counts["auth"] = True
                log.error(f"BirdWeather rejected the station token ({e}). Check it with `setup`.")
                db.execute("INSERT OR REPLACE INTO birdweather(det_id, status, tries, error, at, next_try) "
                           "VALUES (?, 'retry', ?, ?, ?, ?)", (d["id"], tries, str(e)[:200], now, now + 3600))
                db.commit()
                break
            permanent = 400 <= e.status < 500 and e.status != 429
            status = "failed" if permanent or tries >= BW_MAX_TRIES else "retry"
            if status == "failed":
                log.warning(f"BirdWeather: gave up on detection {d['id']}: {e}")
            db.execute("INSERT OR REPLACE INTO birdweather(det_id, status, tries, error, at, next_try) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (d["id"], status, tries, str(e)[:200], now, 0 if status == "failed" else now + min(3600, 30 * 2 ** tries)))
            counts[status] += 1
            if not permanent:
                db.commit()
                break                    # network/server trouble: wait for the next pass
        db.commit()
    return counts


def birdweather_uploader(data: str, cfg: dict, names: dict, stop: threading.Event):
    db = connect(os.path.join(data, "birds.db"))
    log.info(f"BirdWeather: sharing detections ({'with' if cfg.get('birdweather_audio') else 'without'} audio)")
    while not stop.is_set():
        try:
            wait = 3600 if birdweather_pass(data, db, cfg, names)["auth"] else 20
        except Exception as e:
            log.exception(f"BirdWeather upload pass failed: {e}")
            wait = 120
        stop.wait(wait)


# ---------------------------------------------------------------- file input

def analyse_file(data: str, cfg: dict, path: str, start_time: float = None, net: BirdNET = None) -> int:
    """Run a recording through the engine on a simulated clock. Returns the
    number of detections added. Decodes anything libsndfile reads (WAV, FLAC,
    OGG, MP3); other formats need ffmpeg on PATH."""
    import soundfile as sf
    eng = Engine(data, cfg, net=net, live_clock=False)
    before = eng.db.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
    start = start_time or os.path.getmtime(path)
    eng.ring.clock_t = start
    done = threading.Event()

    def feed():
        try:
            for block in _decode(path):
                with eng.ring.lock:
                    while eng.ring.total - eng.ring.consumed > (RING_S - 40) * SR and not eng.stop_event.is_set():
                        eng.ring.lock.wait(0.2)
                eng.ring.push(block, start + (eng.ring.total + len(block)) / SR)
            for _ in range(40):  # trailing silence so the last events close
                eng.ring.push(np.zeros(SR // 2, dtype=np.int16), start + (eng.ring.total + SR // 2) / SR)
        finally:
            done.set()

    threading.Thread(target=feed, daemon=True).start()
    eng.run(finished=done)
    return eng.db.execute("SELECT COUNT(*) FROM detections").fetchone()[0] - before


def _decode(path: str):
    import soundfile as sf
    from .audio import resample
    try:
        with sf.SoundFile(path) as f:
            rate = f.samplerate
            for block in f.blocks(blocksize=rate // 2, dtype="int16", always_2d=True):
                mono = block.mean(axis=1).astype(np.int16)
                yield resample(mono, rate, SR) if rate != SR else mono
        return
    except Exception:
        if not shutil.which("ffmpeg"):
            raise
    import subprocess
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-f", "s16le",
                          "-ac", "1", "-ar", str(SR), "pipe:1"], stdout=subprocess.PIPE)
    while True:
        raw = p.stdout.read(SR)
        if not raw:
            break
        yield np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2")
    p.wait()
