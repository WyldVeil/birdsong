"""The web server: one page plus a small JSON API, standard library only.

Public: detections, stats, photos, spectrograms (never for clips flagged as
containing speech). Admin: play/download clips, star them (kept forever),
hide false positives, confirm rarities.

With a secret base_path, every URL outside it - and every unknown URL
inside it - gets the same plain 404, so the page cannot be found by probing.
"""

import calendar
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import config as C

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
# Never public: hidden, unconfirmed rarities, and detections the extra filters
# are holding ("pending" until checked) or rejected ("failed" re-check,
# "human" noise guard). The admin sees those in the review list.
PUBLIC_SQL = ("d.hidden=0 AND (d.tier<3 OR d.review='confirmed') "
              "AND d.verified NOT IN ('pending','failed','human','review')")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COOKIE = "birdsong_admin"
SESSION_TTL = 30 * 86400
ENGINE_STALE = 20
NOT_FOUND = b"<!doctype html><title>404 Not Found</title><h1>Not Found</h1>"

log = logging.getLogger("birdsong.web")


class App:
    """State shared by request handlers."""

    def __init__(self, data: str, cfg: dict, live=None):
        self.data, self.cfg = data, cfg
        self.db_file = os.path.join(data, "birds.db")
        self.live = live                   # object with .snapshot(since) -> dict, or None
        self.base = C.normalise_base(cfg.get("base_path", "/"))
        self.secret = (cfg.get("secret") or "").encode()
        self._species = {"at": 0.0, "n": -1, "rows": {}}
        self._sp_lock = threading.Lock()
        self._fails = {}                   # ip -> [timestamps]
        self._fail_lock = threading.Lock()

    # --- db
    def connect(self):
        if not os.path.isfile(self.db_file):
            return None
        con = sqlite3.connect(f"file:{self.db_file}?mode=rw", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def species(self, con) -> dict:
        with self._sp_lock:
            n = con.execute("SELECT COUNT(*) FROM species").fetchone()[0]
            c = self._species
            if n == c["n"] and time.time() - c["at"] < 60:
                return c["rows"]
            rows = {r["sci"]: {"sci": r["sci"], "common": r["common"], "tier": r["tier"], "slug": r["slug"],
                               "photo": bool(r["photo"]), "credit": r["photo_credit"], "license": r["photo_license"],
                               "photo_page": r["photo_page"], "wiki": r["wiki_url"], "extract": r["extract"],
                               "fx": r["focal_x"] if "focal_x" in r.keys() else None,
                               "fy": r["focal_y"] if "focal_y" in r.keys() else None}
                    for r in con.execute("SELECT * FROM species")}
            c.update(at=time.time(), n=n, rows=rows)
            return rows

    # --- login throttling (5 failures in 10 minutes -> 15 minute lockout)
    def login_blocked(self, ip: str) -> bool:
        with self._fail_lock:
            ts = [t for t in self._fails.get(ip, []) if time.time() - t < 900]
            self._fails[ip] = ts
            return len([t for t in ts if time.time() - t < 600]) >= 5

    def login_failed(self, ip: str):
        with self._fail_lock:
            self._fails.setdefault(ip, []).append(time.time())

    # --- session
    def make_session(self) -> str:
        exp = str(int(time.time()) + SESSION_TTL)
        return exp + "." + hmac.new(self.secret, b"admin." + exp.encode(), hashlib.sha256).hexdigest()

    def session_ok(self, val: str) -> bool:
        try:
            exp, sig = val.split(".", 1)
            if int(exp) < time.time():
                return False
        except ValueError:
            return False
        good = hmac.new(self.secret, b"admin." + exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, sig)


# ---------------------------------------------------------------- helpers

def _brief(sp, sci):
    s = sp.get(sci) or {"sci": sci, "common": sci, "tier": 3, "slug": "", "photo": False}
    return {"sci": s["sci"], "common": s["common"], "tier": s["tier"], "slug": s["slug"], "photo": s["photo"],
            "fx": s.get("fx"), "fy": s.get("fy")}


def _det(r, admin):
    d = {"id": r["id"], "sci": r["sci"], "start": round(r["start"], 1), "end": round(r["end"], 1),
         "conf": round(r["conf"], 3), "tier": r["tier"], "open": bool(r["open"]),
         "spec": bool(r["spectro"]) and (admin or not r["speech"])}
    if admin:
        d.update(clip=bool(r["clip"]), saved=bool(r["saved"]), hidden=bool(r["hidden"]),
                 review=r["review"], speech=bool(r["speech"]), bytes=r["clip_bytes"],
                 verified=r["verified"], verify_score=r["verify_score"])
    return d


NEW_GAP_DAYS = 45


def _fresh_species(con, vis: str, t0: float, t1: float) -> set:
    """Species with a detection in [t0, t1) that is their first ever, or their
    first after NEW_GAP_DAYS or more unheard. Looking back only NEW_GAP_DAYS
    before t0 is enough: no earlier row in that window means a long gap (or none)."""
    gap = NEW_GAP_DAYS * 86400
    return {r[0] for r in con.execute(
        f"SELECT sci FROM (SELECT d.sci AS sci, d.start AS start, "
        f"LAG(d.start) OVER (PARTITION BY d.sci ORDER BY d.start) AS prev "
        f"FROM detections d WHERE {vis} AND d.start>=? AND d.start<?) "
        f"WHERE start>=? AND (prev IS NULL OR start-prev>=?)", (t0 - gap, t1, t0, gap))}


def _vis(admin):
    return "d.hidden=0 AND d.verified NOT IN ('failed','human','review')" if admin else PUBLIC_SQL


def _ts(d: date) -> float:
    return time.mktime(d.timetuple())


def _same_point(a: date, pa: date, now: datetime = None) -> float:
    """The moment as far into the period starting `pa` as `now` is into the one
    starting `a`, in wall-clock terms (this time yesterday, etc.)."""
    now = now or datetime.now()
    then = datetime(pa.year, pa.month, pa.day) + (now - datetime(a.year, a.month, a.day))
    return time.mktime(then.timetuple())


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def period_bounds(span: str, d: date, first: date = None):
    if span == "day":
        a, b = d, d + timedelta(days=1)
        keys, labels = list(range(24)), [f"{h:02d}" for h in range(24)]
        prev, nxt, label = d - timedelta(days=1), b, d.strftime("%A %d %B %Y").replace(" 0", " ")
    elif span == "week":
        a = d - timedelta(days=d.weekday())
        b = a + timedelta(days=7)
        keys = [(a + timedelta(days=i)).isoformat() for i in range(7)]
        labels = [(a + timedelta(days=i)).strftime("%a %d").replace(" 0", " ") for i in range(7)]
        prev, nxt = a - timedelta(days=7), b
        label = f"{a.strftime('%d %b').lstrip('0')} – {(b - timedelta(days=1)).strftime('%d %b %Y').lstrip('0')}"
    elif span == "month":
        a = date(d.year, d.month, 1)
        b = _add_months(a, 1)
        keys = [(a + timedelta(days=i)).isoformat() for i in range((b - a).days)]
        labels = [str(i + 1) for i in range((b - a).days)]
        prev, nxt, label = _add_months(a, -1), b, a.strftime("%B %Y")
    elif span == "year":
        a, b = date(d.year, 1, 1), date(d.year + 1, 1, 1)
        keys = [f"{d.year}-{m:02d}" for m in range(1, 13)]
        labels = [calendar.month_abbr[m] for m in range(1, 13)]
        prev, nxt, label = date(d.year - 1, 1, 1), b, str(d.year)
    else:
        today = date.today()
        a = date((first or today).year, (first or today).month, 1)
        b = _add_months(date(today.year, today.month, 1), 1)
        keys, labels, m = [], [], a
        while m < b:
            keys.append(m.strftime("%Y-%m"))
            labels.append(m.strftime("%b %y"))
            m = _add_months(m, 1)
        prev = nxt = None
        label = "All time"
    return a, b, keys, labels, prev, nxt, label


def _bucket(span, row):
    if span == "day":
        return row["hour"]
    if span in ("week", "month"):
        return row["day"]
    return row["day"][:7]


# ---------------------------------------------------------------- API reads

def api_period(app: App, span: str, d: date, admin: bool) -> dict:
    con = app.connect()
    if con is None:
        a, b, keys, labels, prev, nxt, label = period_bounds(span, d)
        return {"span": span, "date": d.isoformat(), "label": label, "start": a.isoformat(), "end": b.isoformat(),
                "prev": prev.isoformat() if prev else None, "next": None, "current": True, "buckets": labels,
                "keys": [str(k) for k in keys], "series": [0] * len(keys), "species": [],
                "totals": {"detections": 0, "species": 0, "new": 0, "hours": 0}, "prev_totals": None,
                "first": None, "since": None}
    try:
        vis = _vis(admin)
        first_ts = con.execute(f"SELECT MIN(d.start) FROM detections d WHERE {vis}").fetchone()[0]
        first = date.fromtimestamp(first_ts) if first_ts else None
        a, b, keys, labels, prev, nxt, label = period_bounds(span, d, first)
        t0, t1 = _ts(a), _ts(b)
        rows = con.execute(f"SELECT d.sci, d.start, d.day, d.hour, d.conf FROM detections d WHERE {vis} "
                           f"AND d.start>=? AND d.start<? ORDER BY d.start", (t0, t1)).fetchall()
        sp = app.species(con)
        index = {k: i for i, k in enumerate(keys)}
        series, per = [0] * len(keys), {}
        for r in rows:
            i = index.get(_bucket(span, r))
            if i is None:
                continue
            series[i] += 1
            s = per.setdefault(r["sci"], {"n": 0, "counts": [0] * len(keys), "first": r["start"], "last": 0, "best": 0})
            s["n"] += 1
            s["counts"][i] += 1
            s["last"] = r["start"]
            s["best"] = max(s["best"], r["conf"])
        # "New": heard in this period for the first time ever, or for the first
        # time in NEW_GAP_DAYS or more (a returning migrant). Only meaningful if
        # the station was already listening before the period: in the period it
        # started, every bird would be "new".
        news_ok = first is not None and first < a
        fresh = _fresh_species(con, vis, t0, t1) if news_ok else set()
        species = []
        for sci, s in per.items():
            item = _brief(sp, sci)
            item.update(n=s["n"], counts=s["counts"], first=s["first"], last=s["last"], best=round(s["best"], 3),
                        new=sci in fresh)
            species.append(item)
        species.sort(key=lambda x: (-x["n"], x["common"]))
        hours = con.execute("SELECT COALESCE(SUM(seconds),0) FROM daily WHERE day>=? AND day<?",
                            (a.isoformat(), b.isoformat())).fetchone()[0] / 3600
        prev_totals = None
        if prev is not None:
            pa, pb = period_bounds(span, prev)[:2]
            p1, partial = _ts(pb), False
            if a <= date.today() < b:
                # The period is still going: compare with the same point in the
                # previous one (by the clock, so DST days line up), not all of it.
                p1, partial = min(p1, _same_point(a, pa)), True
            r = con.execute(f"SELECT COUNT(*), COUNT(DISTINCT d.sci) FROM detections d WHERE {vis} "
                            f"AND d.start>=? AND d.start<?", (_ts(pa), p1)).fetchone()
            prev_totals = {"detections": r[0], "species": r[1]}
            if partial:
                prev_totals["partial"] = True
        today = date.today()
        return {"span": span, "date": d.isoformat(), "label": label, "start": a.isoformat(), "end": b.isoformat(),
                "prev": prev.isoformat() if prev else None, "next": nxt.isoformat() if nxt and nxt <= today else None,
                "current": a <= today < b, "buckets": labels, "keys": [str(k) for k in keys], "series": series,
                "species": species, "prev_totals": prev_totals,
                "totals": {"detections": len(rows), "species": len(per), "new": sum(1 for x in species if x["new"]) if news_ok else None,
                           "hours": round(hours, 1)},
                "first": {"sci": rows[0]["sci"], "start": rows[0]["start"]} if rows else None,
                "since": first.isoformat() if first else None}
    finally:
        con.close()


def api_live(app: App, since: int, admin: bool) -> dict:
    import base64
    snap = app.live.snapshot(since) if app.live else {}
    now = time.time()
    paused = bool(snap.get("paused"))
    online = bool(snap) and now - float(snap.get("ts") or 0) < ENGINE_STALE and not snap.get("mic_error") and not paused
    out = {"now": now, "online": online, "level_db": snap.get("level_db"), "seq": snap.get("seq", 0),
           "bands": snap.get("bands", 64), "col_rate": snap.get("col_rate", 20),
           "cols": base64.b64encode(snap.get("cols") or b"").decode() if online else "",
           "started": bool(snap), "hearing": [], "recent": [], "today": {"detections": 0, "species": 0, "hours": 0},
           "rev": "0", "mic_error": bool(snap.get("mic_error")),
           "birdweather": bool(app.cfg.get("birdweather_token")),     # a flag only, never the token
           "paused": paused}
    if admin:
        out.update(peak=snap.get("peak"), clipped_at=snap.get("clipped_at"), mic_error_text=snap.get("mic_error"),
                   low_disk=snap.get("low_disk"))
        rec = _recorder(app)
        out["recording"] = bool(rec and rec.status()["recording"])
        out["room_audio"] = _listen(app) is not None
    con = app.connect()
    if con is None:
        return out
    try:
        sp, vis = app.species(con), _vis(admin)
        out["recent"] = [dict(_det(r, admin), **_brief(sp, r["sci"])) for r in
                         con.execute(f"SELECT * FROM detections d WHERE {vis} ORDER BY d.start DESC LIMIT 12")]
        today = date.today().isoformat()
        r = con.execute(f"SELECT COUNT(*), COUNT(DISTINCT d.sci) FROM detections d WHERE {vis} AND d.day=?", (today,)).fetchone()
        secs = con.execute("SELECT seconds FROM daily WHERE day=?", (today,)).fetchone()
        out["today"] = {"detections": r[0], "species": r[1], "hours": round((secs[0] if secs else 0) / 3600, 1)}
        r = con.execute("SELECT COALESCE(MAX(id),0), COALESCE(SUM(hits),0), COALESCE(SUM(hidden+saved*2),0), "
                        "COUNT(*), COUNT(spectro) FROM detections").fetchone()
        out["rev"] = ".".join(str(v) for v in r)
        if online:
            out["hearing"] = [dict(_brief(sp, h["sci"]), conf=h.get("conf"), since=h.get("since"))
                              for h in snap.get("hearing") or [] if h.get("sci") in sp]
        return out
    finally:
        con.close()


def api_log(app: App, p: dict, admin: bool) -> dict:
    con = app.connect()
    if con is None:
        return {"items": [], "more": False}
    try:
        sp = app.species(con)
        where, args = [_vis(admin)], []
        for key, op in (("from", ">="), ("to", "<")):
            if DATE_RE.match(p.get(key) or ""):
                where.append(f"d.start{op}?")
                args.append(_ts(date.fromisoformat(p[key])))
        if p.get("sci"):
            where.append("d.sci=?")
            args.append(p["sci"][:100])
        if (p.get("before") or "").isdigit():
            where.append("d.id<?")
            args.append(int(p["before"]))
        try:
            limit = max(1, min(100, int(p.get("limit") or 40)))
        except ValueError:
            limit = 40
        rows = con.execute(f"SELECT * FROM detections d WHERE {' AND '.join(where)} ORDER BY d.id DESC LIMIT ?",
                           args + [limit + 1]).fetchall()
        return {"items": [dict(_det(r, admin), **_brief(sp, r["sci"])) for r in rows[:limit]], "more": len(rows) > limit}
    finally:
        con.close()


def api_species_list(app: App, admin: bool) -> dict:
    con = app.connect()
    if con is None:
        return {"species": []}
    try:
        sp = app.species(con)
        stats = {r["sci"]: r for r in con.execute(
            f"SELECT d.sci, COUNT(*) n, MIN(d.start) first, MAX(d.start) last, MAX(d.conf) best "
            f"FROM detections d WHERE {_vis(admin)} GROUP BY d.sci")}
        out = []
        for sci, s in sp.items():
            st = stats.get(sci)
            if s["tier"] >= 3 and not st:
                continue
            item = _brief(sp, sci)
            item.update(n=st["n"] if st else 0, first=st["first"] if st else None,
                        last=st["last"] if st else None, best=round(st["best"], 3) if st else None)
            out.append(item)
        out.sort(key=lambda x: (x["n"] == 0, x["tier"], x["common"]))
        return {"species": out}
    finally:
        con.close()


def api_species(app: App, slug: str, admin: bool):
    con = app.connect()
    if con is None:
        return None
    try:
        sp = app.species(con)
        s = next((v for v in sp.values() if v["slug"] == slug), None)
        if s is None:
            return None
        rows = con.execute(f"SELECT * FROM detections d WHERE {_vis(admin)} AND d.sci=? ORDER BY d.start DESC",
                           (s["sci"],)).fetchall()
        if s["tier"] >= 3 and not rows:
            return None
        hours, months, days = [0] * 24, [0] * 12, {}
        cutoff = (date.today() - timedelta(days=89)).isoformat()
        for r in rows:
            hours[r["hour"]] += 1
            months[int(r["day"][5:7]) - 1] += 1
            if r["day"] >= cutoff:
                days[r["day"]] = days.get(r["day"], 0) + 1
        day_keys = [(date.today() - timedelta(days=89 - i)).isoformat() for i in range(90)]
        out = dict(s)
        out.update(n=len(rows), first=rows[-1]["start"] if rows else None, last=rows[0]["start"] if rows else None,
                   best=round(max((r["conf"] for r in rows), default=0), 3), hours=hours, months=months,
                   days=[days.get(k, 0) for k in day_keys], day_keys=day_keys,
                   recent=[_det(r, admin) for r in rows[:30]])
        return out
    finally:
        con.close()


def api_calendar(app: App, year: int, admin: bool) -> dict:
    con = app.connect()
    if con is None:
        return {"year": year, "days": {}}
    try:
        rows = con.execute(f"SELECT d.day, COUNT(*), COUNT(DISTINCT d.sci) FROM detections d WHERE {_vis(admin)} "
                           f"AND d.day>=? AND d.day<? GROUP BY d.day", (f"{year}-01-01", f"{year + 1}-01-01")).fetchall()
        return {"year": year, "days": {r[0]: [r[1], r[2]] for r in rows}}
    finally:
        con.close()


def _dir_bytes(path):
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _listen(app: App):
    return getattr(app.live, "listen", None) if app.live else None


def _recorder(app: App):
    return getattr(app.live, "recorder", None) if app.live else None


REC_NAME_RE = re.compile(r"^rec-(\d{8})-(\d{6})\.mp3$")
_MP3_KBPS = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]   # MPEG-1 Layer III


def _mp3_seconds(path: str, size: int):
    """Duration of a constant-bitrate MP3, from its first frame header."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return None
    for i in range(len(head) - 3):
        if head[i] == 0xFF and (head[i + 1] & 0xFE) == 0xFA:        # sync, MPEG-1, Layer III
            idx = (head[i + 2] >> 4) & 0x0F
            if 0 < idx < 15:
                return round(size * 8 / (_MP3_KBPS[idx] * 1000))
    return None


def api_recordings(app: App) -> dict:
    rec = _recorder(app)
    status = rec.status() if rec else {"recording": False, "unavailable": True}
    d = os.path.join(app.data, "recordings")
    files = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            m = REC_NAME_RE.match(name)
            if not m:
                continue
            path = os.path.join(d, name)
            size = os.path.getsize(path)
            start = time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
            active = name == status.get("file")
            seconds = _mp3_seconds(path, size)
            if active and status.get("started"):
                # ffmpeg writes in big blocks: show elapsed time and the size it's heading for.
                seconds = round(time.time() - float(status["started"]))
                size = max(size, int(seconds * int(app.cfg.get("rec_kbps", 256)) * 125))
            files.append({"name": name, "start": start, "bytes": size, "seconds": seconds, "active": active})
    cur = status.get("file")
    if cur and status.get("started") and not any(f["name"] == cur for f in files):
        # Just started: ffmpeg hasn't created the file yet.
        m = REC_NAME_RE.match(cur)
        if m:
            seconds = round(time.time() - float(status["started"]))
            files.append({"name": cur, "start": time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")),
                          "bytes": int(seconds * int(app.cfg.get("rec_kbps", 256)) * 125), "seconds": seconds, "active": True})
    files.sort(key=lambda f: -f["start"])
    return {"status": status, "files": files, "disk_free": shutil.disk_usage(app.data).free,
            "kbps": app.cfg.get("rec_kbps", 256), "max_hours": app.cfg.get("rec_max_hours", 24)}


def api_admin_status(app: App) -> dict:
    du = shutil.disk_usage(app.data)
    snap = app.live.snapshot(10 ** 12) if app.live else {}
    cfg = app.cfg
    out = {"disk_free": du.free, "photos_bytes": _dir_bytes(os.path.join(app.data, "photos")),
           "spectro_bytes": _dir_bytes(os.path.join(app.data, "spectro")),
           "settings": {k: cfg.get(k) for k in ("min_conf", "unusual_conf", "vagrant_conf", "clip_days",
                                                "spectro_days", "max_clip_mb", "min_free_gb", "step_s")},
           "engine": {k: snap.get(k) for k in ("level_db", "peak", "clipped_at", "mic_error", "low_disk", "started")}}
    con = app.connect()
    if con is not None:
        try:
            r = con.execute("SELECT COUNT(clip), COALESCE(SUM(clip_bytes),0), "
                            "COALESCE(SUM(CASE WHEN saved=1 AND clip IS NOT NULL THEN 1 ELSE 0 END),0), "
                            "COALESCE(SUM(CASE WHEN tier=3 AND review='pending' AND hidden=0 THEN 1 ELSE 0 END),0), "
                            "COALESCE(SUM(hidden),0) FROM detections").fetchone()
            out.update(clips=r[0], clip_bytes=r[1], saved=r[2], pending=r[3], hidden=r[4])
            out["rules"] = api_rules(app)
            pauser = getattr(app.live, "pauser", None)
            out["pause"] = pauser.status() if pauser else None
            out["review_keep_days"] = app.cfg.get("review_keep_days", 4)
            listen = _listen(app)
            out["live_listeners"] = listen.count() if listen else None
            out["room_audio"] = listen is not None
            out["unverified"] = con.execute("SELECT COUNT(*) FROM detections WHERE verified IN ('failed','human','review') "
                                            "AND hidden=0").fetchone()[0]
            bw = dict(con.execute("SELECT status, COUNT(*) FROM birdweather GROUP BY status").fetchall())
            out["birdweather"] = {"enabled": bool(cfg.get("birdweather_token")), "audio": bool(cfg.get("birdweather_audio")),
                                  "sent": bw.get("sent", 0), "queued": bw.get("retry", 0), "failed": bw.get("failed", 0)}
        finally:
            con.close()
    return out


LEVELS = {1: "Trusted", 2: "Standard", 3: "Guarded", 4: "Strict", 5: "Very strict", 6: "Review all"}


def _norm_levels(cfg: dict) -> dict:
    """{sci: 1..6}. Configs saved before "very strict" existed (no
    species_levels_scale) used 5 for review-all: read that as 6."""
    old = cfg.get("species_levels_scale") != 6
    out = {}
    for k, v in (cfg.get("species_levels") or {}).items():
        if isinstance(v, int) and not isinstance(v, bool):
            v = 6 if (old and v == 5) else v
            if 1 <= v <= 6:
                out[str(k)] = v
    return out


def _default_level(cfg: dict, sci: str) -> int:
    if sci in (cfg.get("human_guard_species") or []):
        return 3
    if sci in (cfg.get("trusted_species") or []):
        return 1
    return 2


def _level(cfg: dict, sci: str) -> int:
    lv = _norm_levels(cfg).get(sci)
    return lv if lv else _default_level(cfg, sci)


def api_rules(app: App) -> dict:
    """Species whose level isn't Standard, or that the admin has set by hand."""
    cfg = app.cfg
    levels = _norm_levels(cfg)
    scis = set(cfg.get("human_guard_species") or []) | set(cfg.get("trusted_species") or []) | set(levels)
    con = app.connect()
    sp = app.species(con) if con is not None else {}
    if con is not None:
        con.close()
    rows = [dict(_brief(sp, s), level=_level(cfg, s), default=_default_level(cfg, s))
            for s in scis if s in sp and (_level(cfg, s) != 2 or s in levels)]   # only species this station knows
    rows.sort(key=lambda r: (r["level"], r["common"]))
    return {"rules": rows, "names": LEVELS}


def api_rule(app: App, sci: str):
    con = app.connect()
    sp = app.species(con) if con is not None else {}
    if con is not None:
        con.close()
    if sci not in sp:
        return None
    return dict(_brief(sp, sci), level=_level(app.cfg, sci), default=_default_level(app.cfg, sci),
                noise_prone=sci in (app.cfg.get("human_guard_species") or []), names=LEVELS)


def set_rule(app: App, sci: str, level):
    """Set (1..5) or reset (None) one species' level. The engine shares this
    config dict, so new detections use it at once; it's also saved to disk."""
    if api_rule(app, sci) is None:
        return None
    levels = _norm_levels(app.cfg)
    app.cfg["species_levels_scale"] = 6
    if level is None or level == _default_level(app.cfg, sci):
        levels.pop(sci, None)
    else:
        levels[sci] = int(level)
    app.cfg["species_levels"] = levels
    C.save(app.cfg, app.data)
    return api_rule(app, sci)


def api_admin_review(app: App) -> dict:
    con = app.connect()
    if con is None:
        return {"items": []}
    try:
        sp = app.species(con)
        rows = con.execute("SELECT * FROM detections d WHERE (d.tier=3 AND d.review='pending') OR d.hidden=1 "
                           "OR d.saved=1 OR d.verified IN ('failed','human','review') ORDER BY d.start DESC LIMIT 300").fetchall()
        return {"items": [dict(_det(r, True), **_brief(sp, r["sci"])) for r in rows]}
    finally:
        con.close()


def safe_path(app: App, rel: str) -> str:
    p = os.path.realpath(os.path.join(app.data, rel))
    if not p.startswith(os.path.realpath(app.data) + os.sep):
        raise ValueError("path escapes data dir")
    return p


def admin_action(app: App, det_id: int, action: str):
    con = app.connect()
    if con is None:
        return None
    try:
        r = con.execute("SELECT * FROM detections WHERE id=?", (det_id,)).fetchone()
        if r is None:
            return None
        sql = {"save": "saved=1", "unsave": "saved=0", "hide": "hidden=1", "unhide": "hidden=0",
               "confirm": "review=CASE WHEN tier=3 THEN 'confirmed' ELSE review END, verified=CASE WHEN "
                          "verified IN ('failed','pending','human','review') THEN 'ok' ELSE verified END, hidden=0"}.get(action)
        if sql:
            con.execute(f"UPDATE detections SET {sql} WHERE id=?", (det_id,))
        elif action == "delete_clip":
            if r["clip"]:
                try:
                    os.unlink(safe_path(app, r["clip"]))
                except (OSError, ValueError):
                    pass
            con.execute("UPDATE detections SET clip=NULL, clip_bytes=0, saved=0 WHERE id=?", (det_id,))
        else:
            return None
        con.commit()
        return _det(con.execute("SELECT * FROM detections WHERE id=?", (det_id,)).fetchone(), True)
    finally:
        con.close()


# ---------------------------------------------------------------- HTTP

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; media-src 'self'; font-src 'self'; connect-src 'self'; "
       "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")


class Handler(BaseHTTPRequestHandler):
    server_version = "Birdsong"
    sys_version = ""
    app: App = None   # set by make_server

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_ip(), fmt % args)

    def client_ip(self) -> str:
        if self.app.cfg.get("behind_proxy"):
            for h in ("CF-Connecting-IP", "X-Real-IP"):
                if self.headers.get(h):
                    return self.headers[h].strip()
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip()
        return self.client_address[0]

    def is_https(self) -> bool:
        return self.app.cfg.get("behind_proxy") and self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def do_GET(self):
        self._go("GET")

    def do_HEAD(self):
        self._go("HEAD")

    def do_POST(self):
        self._go("POST")

    def _go(self, method):
        try:
            if not self.route(method):
                self.not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.exception(f"error on {method} {self.path}: {e}")
            try:
                self.send_json(500, {"ok": False, "error": "server error"})
            except Exception:
                pass

    # --- responses
    def headers_common(self, cache="no-store"):
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("Content-Security-Policy", CSP)

    def send_body(self, status, body: bytes, ctype, cache="no-store", extra=()):
        self.send_response(status)
        self.headers_common(cache)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return True

    def send_json(self, status, obj, extra=()):
        return self.send_body(status, json.dumps(obj, separators=(",", ":")).encode(),
                              "application/json; charset=utf-8", extra=extra)

    def not_found(self):
        self.send_body(404, NOT_FOUND, "text/html; charset=utf-8")

    def send_file(self, path, ctype, cache, ranges=False):
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError:
            return False
        with fh:
            start, end, status = 0, size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip()) if ranges else None
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return True
                status = 206
            self.send_response(status)
            self.headers_common(cache)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(end - start + 1))
            if ranges:
                self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return True
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)
        return True

    def is_admin(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v:
                return self.app.session_ok(v.strip())
        return False

    def cookie(self, value, max_age):
        secure = "; Secure" if self.is_https() else ""
        return ("Set-Cookie", f"{COOKIE}={value}; Path={self.app.base}; Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}")

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n <= 0 or n > 4096:
            return None
        try:
            obj = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    # --- routing
    def route(self, method) -> bool:
        app = self.app
        path, _, qs = self.path.partition("?")
        base = app.base
        if path == base.rstrip("/") and base != "/" and method in ("GET", "HEAD"):
            self.send_response(301)
            self.send_header("Location", base)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if not path.startswith(base):
            return False
        rest = path[len(base):]
        get = method in ("GET", "HEAD")

        if rest in ("", "birds.html", "index.html") and get:
            with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                return self.send_body(200, fh.read(), "text/html; charset=utf-8")
        if get and (m := re.fullmatch(r"font/(fraunces|fraunces-italic)\.woff2", rest)):
            return self.send_file(os.path.join(STATIC, "fonts", m.group(1) + ".woff2"), "font/woff2",
                                  "public, max-age=31536000, immutable")
        if get and (m := re.fullmatch(r"img/([a-z0-9-]+?)(_sm)?\.jpg", rest)):
            if not SLUG_RE.match(m.group(1)):
                return False
            return self.send_file(os.path.join(app.data, "photos", f"{m.group(1)}{m.group(2) or ''}.jpg"),
                                  "image/jpeg", "public, max-age=2592000")
        if get and (m := re.fullmatch(r"spec/(\d{1,10})\.png", rest)):
            r = self._row(int(m.group(1)))
            admin = self.is_admin()
            if r is None or not r["spectro"]:
                return False
            if not admin and (r["speech"] or r["hidden"] or (r["tier"] == 3 and r["review"] != "confirmed")
                              or r["verified"] in ("pending", "failed", "human", "review")):
                return False
            try:
                return self.send_file(safe_path(app, r["spectro"]), "image/png", "private, max-age=86400")
            except ValueError:
                return False
        if get and (m := re.fullmatch(r"clip/(\d{1,10})", rest)):
            if not self.is_admin():
                return False
            r = self._row(int(m.group(1)))
            if r is None or not r["clip"]:
                return False
            ctype = "audio/mpeg" if r["clip"].endswith(".mp3") else "audio/wav"
            try:
                return self.send_file(safe_path(app, r["clip"]), ctype, "private, no-store", ranges=True)
            except ValueError:
                return False
        if rest == "live.mp3" and get:
            # The microphone, live. Admin only: to anyone else a plain 404.
            if not self.is_admin():
                return False
            return self.stream_live()
        if get and rest.startswith("rec/"):
            name = rest[4:]
            if not self.is_admin() or not REC_NAME_RE.match(name):
                return False
            return self.send_file(os.path.join(app.data, "recordings", name), "audio/mpeg", "private, no-store",
                                  ranges=True)
        if not rest.startswith("api/"):
            return False
        return self.api(method, get, rest[4:], {k: v[-1] for k, v in parse_qs(qs).items()})

    def stream_live(self) -> bool:
        listen = _listen(self.app)
        pauser = getattr(self.app.live, "pauser", None)
        if listen is None:
            return self.send_json(503, {"ok": False, "error": "Live audio isn't available here."})
        if pauser is not None and pauser.status()["paused"]:
            return self.send_json(503, {"ok": False, "error": "Birdsong is stopped."})
        q = listen.subscribe()
        if q is None:
            return self.send_json(503, {"ok": False, "error": "Too many people are listening already, or ffmpeg is missing."})
        ip, t0 = self.client_ip(), time.time()
        log.info(f"listen live: started by {ip}")
        try:
            self.send_response(200)
            self.headers_common()
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            if self.command == "HEAD":
                return True
            while True:
                try:
                    chunk = q.get(timeout=5)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            listen.unsubscribe(q)
            log.info(f"listen live: {ip} stopped after {time.time() - t0:.0f} s")
        self.close_connection = True
        return True

    def _row(self, det_id):
        con = self.app.connect()
        if con is None:
            return None
        try:
            return con.execute("SELECT * FROM detections WHERE id=?", (det_id,)).fetchone()
        finally:
            con.close()

    def api(self, method, get, api, p) -> bool:
        app = self.app
        if get:
            admin = self.is_admin()
            if api == "info":
                return self.send_json(200, {k: app.cfg.get(k) for k in ("title", "tagline", "about", "microphone")})
            if api == "live":
                try:
                    since = int(p.get("since") or 0)
                except ValueError:
                    since = 0
                return self.send_json(200, api_live(app, since, admin))
            if api == "period":
                span = p.get("span") or "day"
                if span not in ("day", "week", "month", "year", "all"):
                    return False
                try:
                    d = date.fromisoformat(p["date"]) if DATE_RE.match(p.get("date") or "") else date.today()
                except ValueError:
                    d = date.today()
                return self.send_json(200, api_period(app, span, d, admin))
            if api == "log":
                return self.send_json(200, api_log(app, p, admin))
            if api == "species":
                return self.send_json(200, api_species_list(app, admin))
            if api.startswith("species/") and SLUG_RE.match(api[8:]):
                data = api_species(app, api[8:], admin)
                return self.send_json(200, data) if data else False
            if api == "calendar":
                try:
                    year = int(p.get("year") or date.today().year)
                except ValueError:
                    return False
                return self.send_json(200, api_calendar(app, year, admin)) if 2000 <= year <= 2100 else False
            if api == "me":
                return self.send_json(200, {"admin": admin})
            if api == "admin/status" and admin:
                return self.send_json(200, api_admin_status(app))
            if api == "admin/recordings" and admin:
                return self.send_json(200, api_recordings(app))
            if api == "admin/review" and admin:
                return self.send_json(200, api_admin_review(app))
            if api == "admin/rule" and admin:
                data = api_rule(app, p.get("sci") or "")
                return self.send_json(200, data) if data else False
            return False

        if method != "POST" or self.headers.get("X-Birds") != "1":   # CSRF guard
            return False
        ip = self.client_ip()
        if api == "login":
            if app.login_blocked(ip):
                return self.send_json(429, {"ok": False, "error": "Too many attempts. Try again in 15 minutes."})
            body = self.read_json() or {}
            if not C.check_password(app.cfg, str(body.get("username") or "")[:64], str(body.get("password") or "")[:256]):
                app.login_failed(ip)
                log.warning(f"failed admin login from {ip}")
                return self.send_json(401, {"ok": False, "error": "Wrong username or password."})
            return self.send_json(200, {"ok": True}, extra=[self.cookie(app.make_session(), SESSION_TTL)])
        if api == "logout":
            return self.send_json(200, {"ok": True}, extra=[self.cookie("", 0)])
        if not self.is_admin():
            return False
        if api == "admin/recording":
            body = self.read_json() or {}
            action, rec = body.get("action"), _recorder(app)
            if action in ("start", "stop") and rec is None:
                return self.send_json(503, {"ok": False, "error": "Recording isn't available here."})
            if action == "start":
                pauser = getattr(app.live, "pauser", None)
                if pauser is not None and pauser.status()["paused"]:
                    return self.send_json(400, {"ok": False, "error": "Birdsong is stopped. Resume it first."})
                try:
                    hours = float(body.get("hours") or 4)
                except (TypeError, ValueError):
                    return self.send_json(400, {"ok": False, "error": "hours must be a number"})
                res = rec.start(hours)
            elif action == "stop":
                res = rec.stop()
            elif action == "delete":
                name = str(body.get("name") or "")
                if not REC_NAME_RE.match(name) or (rec and name == rec.status().get("file")):
                    return self.send_json(400, {"ok": False, "error": "Can't delete that recording (is it still recording?)."})
                try:
                    os.unlink(os.path.join(app.data, "recordings", name))
                except FileNotFoundError:
                    pass
                res = {"ok": True}
            else:
                return self.send_json(400, {"ok": False, "error": "unknown action"})
            if not res.get("ok", True):
                return self.send_json(400, res)
            log.info(f"recording {action} by {ip} {body.get('hours') or body.get('name') or ''}".rstrip())
            return self.send_json(200, {"ok": True, **api_recordings(app)})
        if api == "admin/pause":
            pauser = getattr(app.live, "pauser", None)
            if pauser is None:
                return self.send_json(503, {"ok": False, "error": "Not available here (demo mode?)."})
            body = self.read_json() or {}
            if body.get("action") == "resume":
                return self.send_json(200, {"ok": True, "pause": pauser.resume()})
            try:
                minutes = float(body.get("minutes") or 0)                # 0 = until resumed
            except (TypeError, ValueError):
                minutes = -1
            if not 0 <= minutes <= 60 * 24 * 30:
                return self.send_json(400, {"ok": False, "error": "Choose up to 30 days, or until resumed."})
            return self.send_json(200, {"ok": True, "pause": pauser.pause(minutes)})
        if api == "admin/settings":
            body = self.read_json() or {}
            if "review_keep_days" in body:
                try:
                    days = float(body["review_keep_days"])
                except (TypeError, ValueError):
                    days = -1
                if not 0 <= days <= 365:
                    return self.send_json(400, {"ok": False, "error": "Days must be 0 (never) to 365."})
                app.cfg["review_keep_days"] = int(days) if days == int(days) else days
                C.save(app.cfg, app.data)
            return self.send_json(200, {"ok": True, "review_keep_days": app.cfg.get("review_keep_days", 4)})
        if api == "admin/rule":
            body = self.read_json() or {}
            lv = body.get("level")
            if lv is not None and not (isinstance(lv, int) and not isinstance(lv, bool) and 1 <= lv <= 6):
                return self.send_json(400, {"ok": False, "error": "level must be 1-6 or null"})
            res = set_rule(app, str(body.get("sci") or "")[:100], lv)
            if res is None:
                return self.send_json(400, {"ok": False, "error": "unknown species"})
            return self.send_json(200, {"ok": True, "rule": res, **api_rules(app)})
        if m := re.fullmatch(r"admin/det/(\d{1,10})", api):
            res = admin_action(app, int(m.group(1)), str((self.read_json() or {}).get("action") or ""))
            if res is None:
                return self.send_json(400, {"ok": False, "error": "unknown detection or action"})
            return self.send_json(200, {"ok": True, "det": res})
        return False


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv
