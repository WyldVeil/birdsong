"""Settings, stored as JSON in the data folder (data/config.json).

Everything has a default, so a missing or partial file is fine. The file
holds the admin password hash and the cookie secret, so it is written
owner-only where the OS supports that.
"""

import hashlib
import hmac
import json
import os
import secrets
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    # --- where you are (drives the regional species filter; never shown on the page)
    "latitude": None,
    "longitude": None,
    "place": "",

    # --- microphone
    "audio_backend": "auto",      # auto | sounddevice | pulse | arecord
    "device": None,               # name or index from `server.py devices`; None = system default

    # --- web
    "host": "127.0.0.1",          # 0.0.0.0 to let other devices on your network see it
    "port": 8080,
    "base_path": "/",             # e.g. "/k3j9x2/birds/" to make the page hard to guess
    "behind_proxy": False,        # trust X-Forwarded-For/-Proto (nginx, Caddy, Cloudflare Tunnel)
    "title": "Birdsong",
    "tagline": "Live from the garden",
    "microphone": "",             # optional, shown in the page footer, e.g. "Clippy EM272 mono microphone"
    "about": "A microphone is listening around the clock. The moment a bird is identified it appears here, usually within a few seconds.",
    "open_browser": True,

    # --- admin (set with `server.py password`)
    "admin_user": "admin",
    "admin_salt": "",
    "admin_hash": "",
    "secret": "",

    # --- detection
    "min_conf": 0.70,             # species expected in your area
    "unusual_conf": 0.85,         # species plausible within ~150 km
    "vagrant_conf": 0.95,         # anything else; also needs 2 windows + admin review
    "expected_threshold": 0.03,
    "unusual_threshold": 0.005,
    "unusual_radius_deg": 1.5,
    "sensitivity": 1.0,
    "step_s": 1.5,
    "merge_gap_s": 6.0,
    "max_event_s": 30.0,
    "clip_pad_s": 2.0,
    "speech_threshold": 0.15,

    # --- extra false-detection filters on top of BirdNET (see README)
    # Re-check: a detection heard in only one 3 s window is re-scored with the
    # window shifted +/-0.25..1.0 s; it needs >= verify_min_score in
    # >= verify_min_windows of those, or it stays off the public page.
    "verify_single": True, "verify_min_score": 0.5, "verify_min_windows": 2,
    # Human-noise guard: species a person's sounds near the mic can imitate
    # (a sniff -> "Barn Owl"). Held back if BirdNET hears "Human non-vocal"
    # at >= human_guard_threshold anywhere in the clip.
    "human_guard_species": ["Tyto alba", "Strix aluco", "Athene noctua", "Asio otus",
                            "Asio flammeus", "Melanitta nigra"],
    "human_guard_threshold": 0.25,

    # --- storage
    "clip_days": 30,
    "spectro_days": 365,
    "max_clip_mb": 4000,
    "min_free_gb": 5,
    "fetch_photos": True,

    # --- BirdWeather (optional; see README). Empty token = off.
    "birdweather_token": "",
    "birdweather_audio": False,   # also send a short FLAC clip (never ones containing speech)
}


def data_dir() -> str:
    return os.environ.get("BIRDSONG_DATA") or os.path.join(ROOT, "data")


def config_path(base: str = None) -> str:
    return os.path.join(base or data_dir(), "config.json")


def load(base: str = None) -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(config_path(base), encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            cfg.update(stored)
    except FileNotFoundError:
        pass
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_hex(32)
    return cfg


def save(cfg: dict, base: str = None) -> None:
    path = config_path(base)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Store only values that differ from the defaults (plus the secrets), so
    # future default changes reach existing installs.
    out = {k: v for k, v in cfg.items() if k not in DEFAULTS or DEFAULTS[k] != v}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def is_configured(cfg: dict) -> bool:
    return cfg.get("latitude") is not None and cfg.get("longitude") is not None and bool(cfg.get("admin_hash"))


# --- admin password -------------------------------------------------------

def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def set_password(cfg: dict, password: str) -> None:
    salt = secrets.token_bytes(16)
    cfg["admin_salt"] = salt.hex()
    cfg["admin_hash"] = _scrypt(password, salt).hex()


def check_password(cfg: dict, username: str, password: str) -> bool:
    try:
        salt = bytes.fromhex(cfg.get("admin_salt") or "")
        want = bytes.fromhex(cfg.get("admin_hash") or "")
    except ValueError:
        return False
    if not salt or not want:
        return False
    got = _scrypt(password, salt)          # always pay the cost, so timing says nothing
    return hmac.compare_digest(got, want) and hmac.compare_digest(
        username.strip().lower().encode(), str(cfg.get("admin_user", "admin")).lower().encode())


def normalise_base(path: str) -> str:
    path = "/" + (path or "").strip().strip("/")
    return path if path == "/" else path + "/"
