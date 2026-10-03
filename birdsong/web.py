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
import re
import shutil
import sqlite3
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import config as C

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
# Never public: hidden, unconfirmed rarities, and detections the extra filters
# are holding ("pending" until checked) or rejected ("failed" re-check,
# "human" noise guard). The admin sees those in the review list.
PUBLIC_SQL = ("d.hidden=0 AND (d.tier<3 OR d.review='confirmed') "
              "AND d.verified NOT IN ('pending','failed','human')")
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
                               "photo_page": r["photo_page"], "wiki": r["wiki_url"], "extract": r["extract"]}
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
    return {"sci": s["sci"], "common": s["common"], "tier": s["tier"], "slug": s["slug"], "photo": s["photo"]}


def _det(r, admin):
    d = {"id": r["id"], "sci": r["sci"], "start": round(r["start"], 1), "end": round(r["end"], 1),
         "conf": round(r["conf"], 3), "tier": r["tier"], "open": bool(r["open"]),
         "spec": bool(r["spectro"]) and (admin or not r["speech"])}
    if admin:
        d.update(clip=bool(r["clip"]), saved=bool(r["saved"]), hidden=bool(r["hidden"]),
                 review=r["review"], speech=bool(r["speech"]), bytes=r["clip_bytes"],
                 verified=r["verified"], verify_score=r["verify_score"])
    return d


def _vis(admin):
    return "d.hidden=0 AND d.verified NOT IN ('failed','human')" if admin else PUBLIC_SQL


def _ts(d: date) -> float:
    return time.mktime(d.timetuple())


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
        firsts = dict(con.execute(f"SELECT d.sci, MIN(d.start) FROM detections d WHERE {vis} GROUP BY d.sci").fetchall())
        species = []
        for sci, s in per.items():
            item = _brief(sp, sci)
            item.update(n=s["n"], counts=s["counts"], first=s["first"], last=s["last"], best=round(s["best"], 3),
                        new=t0 <= firsts.get(sci, 0) < t1)
            species.append(item)
        species.sort(key=lambda x: (-x["n"], x["common"]))
        hours = con.execute("SELECT COALESCE(SUM(seconds),0) FROM daily WHERE day>=? AND day<?",
                            (a.isoformat(), b.isoformat())).fetchone()[0] / 3600
        prev_totals = None
        if prev is not None:
            pa, pb = period_bounds(span, prev)[:2]
            r = con.execute(f"SELECT COUNT(*), COUNT(DISTINCT d.sci) FROM detections d WHERE {vis} "
                            f"AND d.start>=? AND d.start<?", (_ts(pa), _ts(pb))).fetchone()
            prev_totals = {"detections": r[0], "species": r[1]}
        today = date.today()
        return {"span": span, "date": d.isoformat(), "label": label, "start": a.isoformat(), "end": b.isoformat(),
                "prev": prev.isoformat() if prev else None, "next": nxt.isoformat() if nxt and nxt <= today else None,
                "current": a <= today < b, "buckets": labels, "keys": [str(k) for k in keys], "series": series,
                "species": species, "prev_totals": prev_totals,
                "totals": {"detections": len(rows), "species": len(per), "new": sum(1 for x in species if x["new"]),
                           "hours": round(hours, 1)},
                "first": {"sci": rows[0]["sci"], "start": rows[0]["start"]} if rows else None,
                "since": first.isoformat() if first else None}
    finally:
        con.close()


def api_live(app: App, since: int, admin: bool) -> dict:
    import base64
    snap = app.live.snapshot(since) if app.live else {}
    now = time.time()
    online = bool(snap) and now - float(snap.get("ts") or 0) < ENGINE_STALE and not snap.get("mic_error")
    out = {"now": now, "online": online, "level_db": snap.get("level_db"), "seq": snap.get("seq", 0),
           "bands": snap.get("bands", 64), "col_rate": snap.get("col_rate", 20),
           "cols": base64.b64encode(snap.get("cols") or b"").decode() if online else "",
           "started": bool(snap), "hearing": [], "recent": [], "today": {"detections": 0, "species": 0, "hours": 0},
           "rev": "0", "mic_error": bool(snap.get("mic_error")),
           "birdweather": bool(app.cfg.get("birdweather_token"))}     # a flag only, never the token
    if admin:
        out.update(peak=snap.get("peak"), clipped_at=snap.get("clipped_at"), mic_error_text=snap.get("mic_error"),
                   low_disk=snap.get("low_disk"))
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
            out["unverified"] = con.execute("SELECT COUNT(*) FROM detections WHERE verified IN ('failed','human') "
                                            "AND hidden=0").fetchone()[0]
            bw = dict(con.execute("SELECT status, COUNT(*) FROM birdweather GROUP BY status").fetchall())
            out["birdweather"] = {"enabled": bool(cfg.get("birdweather_token")), "audio": bool(cfg.get("birdweather_audio")),
                                  "sent": bw.get("sent", 0), "queued": bw.get("retry", 0), "failed": bw.get("failed", 0)}
        finally:
            con.close()
    return out


def api_admin_review(app: App) -> dict:
    con = app.connect()
    if con is None:
        return {"items": []}
    try:
        sp = app.species(con)
        rows = con.execute("SELECT * FROM detections d WHERE (d.tier=3 AND d.review='pending') OR d.hidden=1 "
                           "OR d.saved=1 OR d.verified IN ('failed','human') ORDER BY d.start DESC LIMIT 300").fetchall()
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
                          "verified IN ('failed','pending','human') THEN 'ok' ELSE verified END, hidden=0"}.get(action)
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
                              or r["verified"] in ("pending", "failed", "human")):
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
        if not rest.startswith("api/"):
            return False
        return self.api(method, get, rest[4:], {k: v[-1] for k, v in parse_qs(qs).items()})

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
            if api == "admin/review" and admin:
                return self.send_json(200, api_admin_review(app))
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
