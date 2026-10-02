"""Command line: setup wizard, run, demo, autostart, self-test."""

import argparse
import getpass
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from . import audio, config as C

log = logging.getLogger("birdsong")


def setup_logging(data: str, verbose: bool = False):
    os.makedirs(data, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(os.path.join(data, "birdsong.log"), maxBytes=1_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        v = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        v = ""
    return v or default


def yes(prompt: str, default: bool = False) -> bool:
    v = ask(prompt + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not v else v.startswith("y")


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 80))       # TEST-NET address; nothing is sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "your-computer-ip"


def page_urls(cfg: dict) -> list:
    base = C.normalise_base(cfg.get("base_path"))
    port = cfg.get("port", 8080)
    urls = [f"http://localhost:{port}{base}"]
    if cfg.get("host") in ("0.0.0.0", "::"):
        urls.append(f"http://{lan_ip()}:{port}{base}")
    return urls


# ---------------------------------------------------------------- setup wizard

def geocode(q: str):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": q, "format": "json", "limit": 1})
    req = urllib.request.Request(url, headers={"User-Agent": "Birdsong-setup/1.0 (+https://github.com/WyldVeil/birdsong)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.load(r)
    if not res:
        return None
    return float(res[0]["lat"]), float(res[0]["lon"]), res[0].get("display_name", q)


def wizard(cfg: dict, data: str) -> dict:
    print("\n  Birdsong setup\n  ──────────────")
    print("  Press Enter to keep the value in [brackets].\n")

    # 1. location
    print("1. Where is the microphone?")
    print("   Used only to work out which birds are likely near you (it is never shown on the page).")
    print("   Type a town, postcode or address, or 'lat, lon' (right-click a spot in Google Maps to copy it).")
    while True:
        cur = f"{cfg['latitude']}, {cfg['longitude']}" if cfg.get("latitude") is not None else ""
        q = ask("   Location", cfg.get("place") or cur)
        m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*", q)
        if m:
            lat, lon, name = float(m.group(1)), float(m.group(2)), ""
        elif q:
            try:
                found = geocode(q)
            except Exception as e:
                print(f"   Couldn't look that up ({e}). Try 'lat, lon' instead.")
                continue
            if not found:
                print("   Nothing found for that. Try a nearby town, or 'lat, lon'.")
                continue
            lat, lon, name = found
            print(f"   Found: {name}")
            if not yes("   Is that right?", True):
                continue
        else:
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            # Two decimals (~1 km) is plenty for BirdNET's range model.
            cfg.update(latitude=round(lat, 2), longitude=round(lon, 2), place=q if not m else "")
            break
        print("   That isn't a valid latitude/longitude.")

    # 2. microphone
    print("\n2. Which microphone?")
    backend = audio.pick_backend(cfg.get("audio_backend", "auto"))
    while True:
        devs = audio.list_devices(backend)
        if not devs:
            print(f"   No input devices found via '{backend}'. Plug a microphone in and press Enter to retry,")
            print("   or Ctrl+C to quit.")
            ask("   ")
            continue
        for n, (dev_id, desc, is_def) in enumerate(devs, 1):
            print(f"   {n:>2}. {desc}{'   (default)' if is_def else ''}")
        cur = ""
        for n, (dev_id, _d, is_def) in enumerate(devs, 1):
            if (cfg.get("device") is not None and str(dev_id) == str(cfg.get("device"))) or \
               (cfg.get("device") is None and is_def):
                cur = str(n)
        pick = ask("   Number", cur or "1")
        if not pick.isdigit() or not 1 <= int(pick) <= len(devs):
            continue
        dev_id, desc, is_def = devs[int(pick) - 1]
        cfg["audio_backend"] = backend
        cfg["device"] = None if is_def and backend == "sounddevice" else dev_id
        print("   Listening for 4 seconds to check the level…")
        try:
            rms, peak, clipped = audio.measure(4, backend, cfg["device"])
        except Exception as e:
            print(f"   Couldn't record from it: {e}")
            if yes("   Pick another?", True):
                continue
            break
        print(f"   Level {rms:.0f} dBFS (peaks {peak:.0f} dBFS)")
        if clipped > 0.001:
            print("   ⚠ It's clipping (too loud). Turn the input volume down in your sound settings, then retry.")
        elif rms < -75:
            print("   ⚠ That's nearly silent. Check it's the right device and not muted.")
        else:
            print("   ✓ Sounds fine.")
        if not yes("   Test again / choose another?", False):
            break

    # 3. admin password
    print("\n3. Admin password (lets you play and keep recordings; visitors can't).")
    if cfg.get("admin_hash") and not yes("   Change the existing password?", False):
        pass
    else:
        while True:
            pw = getpass.getpass("   New password (blank = make one up for me): ")
            if not pw:
                pw = secrets.token_urlsafe(9)
                print(f"   Your admin password is:  {pw}   (username: admin)  - write it down.")
                break
            if len(pw) < 6:
                print("   Use at least 6 characters.")
                continue
            if getpass.getpass("   Again: ") == pw:
                break
            print("   They didn't match.")
        C.set_password(cfg, pw)

    # 4. who can see it
    print("\n4. Who should be able to open the page?")
    print("   1. Just this computer           (http://localhost)")
    print("   2. Devices on my home network   (phones, tablets, other PCs on your Wi-Fi)")
    print("   3. The internet, via my own domain / reverse proxy / tunnel (advanced)")
    cur = "3" if cfg.get("behind_proxy") else "2" if cfg.get("host") in ("0.0.0.0", "::") else "1"
    choice = ask("   Choice", cur)
    cfg["host"] = "0.0.0.0" if choice == "2" else "127.0.0.1"
    cfg["behind_proxy"] = choice == "3"
    port = ask("   Port", str(cfg.get("port", 8080)))
    cfg["port"] = int(port) if port.isdigit() and 0 < int(port) < 65536 else 8080
    if choice in ("2", "3"):
        print("   You can hide the page under a random path, so only people you give the link to can find it.")
        if yes("   Use a secret path?", choice == "3" or cfg.get("base_path", "/") != "/"):
            if cfg.get("base_path", "/") == "/" or yes("   Make a new one?", False):
                alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
                seg = lambda n: "".join(secrets.choice(alphabet) for _ in range(n))
                cfg["base_path"] = f"/{seg(10)}/{seg(16)}/"
        else:
            cfg["base_path"] = "/"

    # 5. name
    print("\n5. What should the page be called?")
    cfg["title"] = ask("   Title", cfg.get("title", "Birdsong"))
    cfg["tagline"] = ask("   Subtitle", cfg.get("tagline", "Live from the garden"))

    C.save(cfg, data)
    print("\n  Saved. The page will be at:")
    for u in page_urls(cfg):
        print(f"    {u}")
    if cfg["behind_proxy"]:
        print(f"  Point your reverse proxy / tunnel at http://127.0.0.1:{cfg['port']} (see README).")
    print()
    return cfg


# ---------------------------------------------------------------- run

def ensure_model(data: str):
    from . import engine
    if not engine.model_present(data):
        print("Downloading the BirdNET model (about 66 MB, one time only)…")
        engine.download_model(data)


def cmd_run(args, data):
    from . import engine as E, web
    cfg = C.load(data)
    if not C.is_configured(cfg):
        if not interactive():
            sys.exit("Not set up yet. Run:  run.bat setup   (Windows)   or   ./run.sh setup   (Linux/macOS)")
        cfg = wizard(cfg, data)
    setup_logging(data, args.verbose)
    ensure_model(data)
    if args.port:
        cfg["port"] = args.port

    eng = E.Engine(data, cfg)
    cap = audio.Capture(eng.ring.push, cfg.get("audio_backend", "auto"), cfg.get("device"), eng.status, log)
    stop = threading.Event()
    cap.start()
    threading.Thread(target=eng.run, name="engine", daemon=True).start()
    threading.Thread(target=E.housekeeping, args=(eng, stop), name="housekeeping", daemon=True).start()
    if cfg.get("fetch_photos", True):
        threading.Thread(target=E.photo_fetcher, args=(data, stop), name="photos", daemon=True).start()

    app = web.App(data, cfg, live=eng)
    try:
        srv = web.make_server(app, cfg["host"], int(cfg["port"]))
    except OSError as e:
        sys.exit(f"Couldn't listen on port {cfg['port']}: {e}\nIs Birdsong already running? "
                 f"Otherwise pick another port with `setup` or --port.")
    log.info(f"listening with {cap.backend}" + (f" ({cfg['device']})" if cfg.get("device") is not None else " (default mic)"))
    for u in page_urls(cfg):
        log.info(f"page: {u}")
    if cfg.get("open_browser", True) and not args.no_browser and interactive():
        _open(page_urls(cfg)[0])
    _serve(srv, [cap.stop, eng.stop, stop.set])


def _open(url):
    import webbrowser
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()


def _serve(srv, stoppers):
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        for f in stoppers:
            try:
                f()
            except Exception:
                pass
        srv.server_close()


# ---------------------------------------------------------------- other commands

def cmd_setup(args, data):
    wizard(C.load(data), data)
    if yes("Start Birdsong now?", True):
        cmd_run(args, data)


def cmd_devices(args, data):
    cfg = C.load(data)
    backend = audio.pick_backend(cfg.get("audio_backend", "auto"))
    print(f"Input devices ({backend}):")
    for dev_id, desc, is_def in audio.list_devices(backend):
        print(f"  {dev_id!s:<50} {desc}{'  (default)' if is_def else ''}")


def cmd_mictest(args, data):
    cfg = C.load(data)
    print("Listening for 5 seconds…")
    rms, peak, clipped = audio.measure(5, cfg.get("audio_backend", "auto"), cfg.get("device"))
    print(f"Level {rms:.1f} dBFS, peak {peak:.1f} dBFS, clipped {clipped * 100:.2f}% of samples")
    if clipped > 0.001:
        print("Too loud: turn the microphone input volume down.")
    elif rms < -75:
        print("Nearly silent: wrong device or muted?")


def cmd_password(args, data):
    cfg = C.load(data)
    pw = getpass.getpass("New admin password: ")
    if len(pw) < 6 or getpass.getpass("Again: ") != pw:
        sys.exit("Not changed (too short, or they didn't match).")
    C.set_password(cfg, pw)
    C.save(cfg, data)
    print("Saved. Restart Birdsong if it's running.")


def cmd_analyse(args, data):
    from . import engine as E
    cfg = C.load(data)
    setup_logging(data, args.verbose)
    ensure_model(data)
    net = E.BirdNET(E.model_dir(data))
    total = 0
    for path in args.files:
        n = E.analyse_file(data, cfg, path, net=net)
        print(f"{path}: {n} detection(s)")
        total += n
    print(f"Done: {total} detection(s) added.")


def cmd_demo(args, data):
    from . import demo, web, engine as E
    ddir = os.path.join(os.path.dirname(data), "data-demo")
    setup_logging(ddir, args.verbose)
    cfg = C.load(ddir)
    cfg.update(title="Birdsong", tagline="Demo · made-up detections", host="127.0.0.1",
               port=args.port or cfg.get("port", 8080), base_path="/")
    if not cfg.get("admin_hash"):
        C.set_password(cfg, "demo")
    C.save(cfg, ddir)
    demo.seed(ddir)
    live = demo.FakeLive(ddir)
    stop = threading.Event()
    threading.Thread(target=live.run, args=(stop,), daemon=True).start()
    if not args.no_photos:
        threading.Thread(target=E.photo_fetcher, args=(ddir, stop), daemon=True).start()
    srv = web.make_server(web.App(ddir, cfg, live=live), "127.0.0.1", int(cfg["port"]))
    url = f"http://localhost:{cfg['port']}/"
    log.info(f"demo page: {url}   (admin login: admin / demo)")
    if not args.no_browser and interactive():
        _open(url)
    _serve(srv, [stop.set])


def cmd_autostart(args, data):
    from . import autostart
    {"install": autostart.install, "remove": autostart.remove, "status": autostart.status}[args.action]()


def cmd_selftest(args, data):
    """Unit tests, then (unless --quick) a real-model check on a robin recording."""
    import unittest
    root = C.ROOT
    suite = unittest.defaultTestLoader.discover(os.path.join(root, "tests"), top_level_dir=root)
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    if not res.wasSuccessful():
        sys.exit(1)
    if args.quick:
        return
    import tempfile
    from . import engine as E
    tmp = tempfile.mkdtemp(prefix="birdsong-selftest-")
    try:
        print("Model check: downloading BirdNET and a robin recording from Wikimedia Commons…")
        mdir = os.path.join(tmp, "model")
        shared = os.path.join(data, "model")
        if E.model_present(data):
            shutil.copytree(shared, mdir)
        else:
            E.download_model(tmp)
        rec = os.path.join(tmp, "robin.mp3")
        q = urllib.parse.urlencode({"action": "query", "format": "json", "prop": "imageinfo", "iiprop": "url",
                                    "titles": "File:Erithacus rubecula - European Robin XC509375.mp3"})
        meta = json.loads(E._get("https://commons.wikimedia.org/w/api.php?" + q))
        real = next(iter(meta["query"]["pages"].values()))["imageinfo"][0]["url"]
        with open(rec, "wb") as fh:
            fh.write(E._get(real, 60))
        cfg = dict(C.DEFAULTS, latitude=51.5, longitude=-0.1)
        n = E.analyse_file(tmp, cfg, rec, start_time=time.time() - 3600)
        con = E.connect(os.path.join(tmp, "birds.db"))
        found = [r[0] for r in con.execute("SELECT sci FROM detections")]
        clips = [r[0] for r in con.execute("SELECT clip FROM detections WHERE clip IS NOT NULL")]
        con.close()
        print(f"Model check: {n} detection(s): {', '.join(found) or 'none'}; {len(clips)} clip(s) written")
        if "Erithacus rubecula" not in found or not clips:
            sys.exit("Model check FAILED: expected a European Robin with a clip.")
        print("Model check passed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="server.py", description="Birdsong: a 24/7 bird sound identifier with a live web page.")
    ap.add_argument("--data", help="data folder (default: ./data, or $BIRDSONG_DATA)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="listen and serve the page (the default)")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true")
    p = sub.add_parser("setup", help="first-time setup wizard (location, microphone, password, access)")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true")
    sub.add_parser("devices", help="list microphones")
    sub.add_parser("mic-test", help="record 5 seconds and report the level")
    sub.add_parser("password", help="change the admin password")
    p = sub.add_parser("analyse", help="analyse recordings (WAV/FLAC/OGG/MP3) into the log")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("demo", help="preview the page with made-up data (no microphone needed)")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--no-photos", action="store_true")
    p = sub.add_parser("autostart", help="start Birdsong automatically when you log in / at boot")
    p.add_argument("action", choices=["install", "remove", "status"])
    p = sub.add_parser("selftest", help="run the tests and a real detection check")
    p.add_argument("--quick", action="store_true", help="unit tests only (no downloads)")
    args = ap.parse_args(argv)
    if args.data:
        os.environ["BIRDSONG_DATA"] = os.path.abspath(args.data)
    data = C.data_dir()
    if args.cmd is None:
        args.cmd, args.port, args.no_browser = "run", None, False
    {"run": cmd_run, "setup": cmd_setup, "devices": cmd_devices, "mic-test": cmd_mictest,
     "password": cmd_password, "analyse": cmd_analyse, "demo": cmd_demo, "autostart": cmd_autostart,
     "selftest": cmd_selftest}[args.cmd](args, data)
