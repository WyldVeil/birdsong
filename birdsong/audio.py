"""Microphone capture, mono 48 kHz int16, from whichever backend works here.

  sounddevice  PortAudio (bundled in the wheel on Windows and macOS; on
               Linux it needs libportaudio2). Works everywhere.
  pulse        `parecord` - PulseAudio / PipeWire, the usual Linux desktop.
  arecord      raw ALSA - headless Linux such as a Raspberry Pi.

"auto" picks pulse on Linux desktops, then sounddevice, then arecord, and
sounddevice on Windows/macOS.
"""

import shutil
import subprocess
import sys
import threading
import time

import numpy as np

SR = 48000


def _have_pulse() -> bool:
    if not shutil.which("parecord") or not shutil.which("pactl"):
        return False
    try:
        return subprocess.run(["pactl", "info"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _sounddevice():
    try:
        import sounddevice
        sounddevice.query_devices()
        return sounddevice
    except Exception:
        return None


def pick_backend(name: str = "auto") -> str:
    if name and name != "auto":
        return name
    if sys.platform.startswith("linux"):
        if _have_pulse():
            return "pulse"
        if _sounddevice():
            return "sounddevice"
        if shutil.which("arecord"):
            return "arecord"
    return "sounddevice"


def list_devices(backend: str = "auto"):
    """[(id, description, is_default)] for input devices on this backend."""
    backend = pick_backend(backend)
    out = []
    if backend == "pulse":
        default = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True).stdout.strip()
        r = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True)
        try:
            import json
            for s in json.loads(r.stdout):
                if str(s.get("name", "")).endswith(".monitor"):     # speaker loopbacks
                    continue
                out.append((s["name"], s.get("description") or s["name"], s["name"] == default))
        except Exception:
            for line in subprocess.run(["pactl", "list", "short", "sources"], capture_output=True,
                                       text=True).stdout.splitlines():
                parts = line.split("\t")
                if len(parts) > 1 and not parts[1].endswith(".monitor"):
                    out.append((parts[1], parts[1], parts[1] == default))
    elif backend == "sounddevice":
        sd = _sounddevice()
        if sd is None:
            return []
        try:
            default_in = sd.default.device[0]
        except Exception:
            default_in = None
        apis = sd.query_hostapis()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                api = apis[d["hostapi"]]["name"] if d["hostapi"] < len(apis) else ""
                out.append((i, f"{d['name']}  [{api}]", i == default_in))
    elif backend == "arecord":
        r = subprocess.run(["arecord", "-L"], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if line and not line.startswith(" ") and line not in ("null",):
                out.append((line, line, line == "default"))
    return out


class Capture(threading.Thread):
    """Feeds `push(int16 ndarray, wall_time)` until stopped. Restarts the
    device with backoff when it goes away (unplugged, sound server restart)."""

    def __init__(self, push, backend="auto", device=None, status=None, log=None):
        super().__init__(name="capture", daemon=True)
        self.push = push
        self.backend = pick_backend(backend)
        self.device = device
        self.status = status if status is not None else {}
        self.log = log
        self.stop_event = threading.Event()
        self.proc = None

    def _warn(self, msg):
        self.status["mic_error"] = msg
        if self.log:
            self.log.warning(msg)

    def run(self):
        backoff = 2
        while not self.stop_event.is_set():
            started = time.time()
            try:
                if self.backend == "sounddevice":
                    self._run_sounddevice()
                else:
                    self._run_subprocess()
            except Exception as e:
                self._warn(f"microphone ({self.backend}): {e}")
            if self.stop_event.is_set():
                break
            if time.time() - started > 30:
                backoff = 2
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, 60)

    # --- PortAudio
    def _run_sounddevice(self):
        sd = _sounddevice()
        if sd is None:
            raise RuntimeError("sounddevice/PortAudio is not available")
        dev = self.device
        if isinstance(dev, str) and dev.isdigit():
            dev = int(dev)
        rate = SR
        try:
            sd.check_input_settings(device=dev, samplerate=SR, channels=1, dtype="int16")
        except Exception:
            rate = int(sd.query_devices(dev, "input")["default_samplerate"])
        ok = {"n": 0}

        def cb(indata, frames, t, flags):
            x = indata[:, 0].copy()
            if rate != SR:
                x = resample(x, rate, SR)
            ok["n"] += len(x)
            if ok["n"] > SR * 5:
                self.status.pop("mic_error", None)
            self.push(x, time.time())

        with sd.InputStream(device=dev, samplerate=rate, channels=1, dtype="int16",
                            blocksize=rate // 10, callback=cb):
            while not self.stop_event.wait(1.0):
                pass

    # --- parecord / arecord
    def _run_subprocess(self):
        if self.backend == "pulse":
            cmd = ["parecord", "--raw", "--rate=48000", "--channels=1", "--format=s16le",
                   "--latency-msec=200", "--client-name=birdsong", "--stream-name=listener"]
            if self.device:
                cmd.insert(1, f"--device={self.device}")
        elif self.backend == "arecord":
            cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", "48000", "-c", "1"]
            if self.device:
                cmd += ["-D", str(self.device)]
        else:
            raise RuntimeError(f"unknown audio backend {self.backend!r}")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        got = 0
        try:
            while not self.stop_event.is_set():
                raw = self.proc.stdout.read(SR // 10 * 2)
                if not raw:
                    break
                if len(raw) % 2:
                    raw = raw[:-1]
                got += len(raw)
                if got > SR * 2 * 5:
                    self.status.pop("mic_error", None)
                self.push(np.frombuffer(raw, dtype="<i2"), time.time())
        finally:
            err = b""
            try:
                self.proc.kill()
                err = self.proc.stderr.read()[-300:]
            except Exception:
                pass
        if not self.stop_event.is_set():
            raise RuntimeError(err.decode(errors="replace").strip() or "recording stopped")

    def stop(self):
        self.stop_event.set()
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Band-limited resample of one block (FFT method). Good enough for
    44.1 -> 48 kHz on 100 ms blocks; BirdNET only looks below 15 kHz."""
    if src == dst or len(x) == 0:
        return x
    n_out = int(round(len(x) * dst / src))
    spec = np.fft.rfft(x.astype(np.float32))
    out_spec = np.zeros(n_out // 2 + 1, dtype=complex)
    k = min(len(spec), len(out_spec))
    out_spec[:k] = spec[:k]
    y = np.fft.irfft(out_spec, n_out) * (n_out / len(x))
    return np.clip(y, -32768, 32767).astype(np.int16)


def measure(seconds: float = 4.0, backend="auto", device=None):
    """Record a few seconds and return (rms_dbfs, peak_dbfs, clipped_fraction)."""
    buf = []
    status = {}
    cap = Capture(lambda x, t: buf.append(x), backend, device, status)
    cap.start()
    time.sleep(seconds + 1.0)
    cap.stop()
    if not buf:
        raise RuntimeError(status.get("mic_error") or "no audio received")
    x = np.concatenate(buf)[SR // 2:].astype(np.float32)   # skip the switch-on thump
    if len(x) == 0:
        raise RuntimeError("no audio received")
    x -= x.mean()
    rms = float(np.sqrt(np.mean(x ** 2))) / 32768
    peak = float(np.abs(x).max()) / 32768
    clipped = float(np.mean(np.abs(x) >= 32760))
    db = lambda v: 20 * np.log10(v + 1e-9)
    return db(rms), db(peak), clipped
