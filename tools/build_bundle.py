"""Build a self-contained release zip for the platform this runs on.

    uv run --no-project python tools/build_bundle.py --name linux-x64 --version 1.0.0

The zip holds the source, a relocatable CPython (python-build-standalone,
via uv) with the locked dependencies installed into it, and the BirdNET
model, so a user unzips it and runs run.bat / run.sh with no downloads.

Before zipping, the bundle is copied to a different folder (with a space in
its name) and its own launcher runs the full self-test there, which proves
it is relocatable and complete.
"""

import argparse
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = sys.platform == "win32"
PY = "3.12"

TRIM = ["test", "idlelib", "tkinter", "turtledemo", "lib2to3", "ensurepip", "pydoc_data"]

MODEL_NOTICE = """BirdNET V2.4 model and labels
by the K. Lisa Yang Center for Conservation Bioacoustics (Cornell Lab of
Ornithology) and Chemnitz University of Technology.

Licensed under Creative Commons Attribution-NonCommercial-ShareAlike 4.0
International (CC BY-NC-SA 4.0): https://creativecommons.org/licenses/by-nc-sa/4.0/
Unmodified copies of the files published at
https://github.com/birdnet-team/BirdNET-Analyzer

Kahl, S., Wood, C. M., Eibl, M., & Klinck, H. (2021). BirdNET: A deep learning
solution for avian diversity monitoring. Ecological Informatics, 61, 101236.

Non-commercial use only.
"""


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def python_in(runtime):
    return os.path.join(runtime, "python.exe") if WINDOWS else os.path.join(runtime, "bin", "python3")


def build(name: str, version: str, out: str, skip_test: bool):
    build_dir = os.path.join(ROOT, "build", name)
    shutil.rmtree(build_dir, ignore_errors=True)
    stage = os.path.join(build_dir, f"birdsong-{version}")
    os.makedirs(stage)

    # 1. source exactly as committed
    tar_bytes = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, check=True,
                               capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        tf.extractall(stage, filter="data")
    shutil.rmtree(os.path.join(stage, ".github"), ignore_errors=True)
    shutil.rmtree(os.path.join(stage, "tools"), ignore_errors=True)

    # 2. a relocatable Python
    pydir = os.path.join(build_dir, "py")
    run(["uv", "python", "install", PY, "--install-dir", pydir])
    cands = [d for d in os.listdir(pydir) if d.startswith("cpython-") and not os.path.islink(os.path.join(pydir, d))]
    if len(cands) != 1:
        sys.exit(f"expected one Python in {pydir}, found {cands}")
    runtime = os.path.join(stage, "runtime")
    shutil.move(os.path.join(pydir, cands[0]), runtime)
    py = python_in(runtime)

    # 3. the locked dependencies, installed into that Python
    req = os.path.join(build_dir, "requirements.lock.txt")
    run(["uv", "export", "--frozen", "--no-hashes", "--no-emit-project", "--no-header",
         "--format", "requirements.txt", "-o", req], cwd=ROOT)
    run(["uv", "pip", "install", "--python", py, "--break-system-packages", "--no-cache", "-r", req])

    # 4. trim what a background service never uses
    lib = os.path.join(runtime, "Lib") if WINDOWS else os.path.join(runtime, "lib", f"python{PY}")
    for d in TRIM:
        shutil.rmtree(os.path.join(lib, d), ignore_errors=True)
    for d in ("pip", "setuptools"):
        for entry in os.listdir(os.path.join(lib, "site-packages")):
            if entry == d or entry.startswith(d + "-"):
                shutil.rmtree(os.path.join(lib, "site-packages", entry), ignore_errors=True)
    for extra in ("include", "share", "tcl", os.path.join("libs")):
        shutil.rmtree(os.path.join(runtime, extra), ignore_errors=True)
    if not WINDOWS:
        for f in os.listdir(os.path.join(runtime, "lib")):
            if f.startswith(("libtcl", "libtk", "tcl", "tk", "itcl", "thread")):
                p = os.path.join(runtime, "lib", f)
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)
    # Console-script launchers (f2py, tqdm, numpy-config...) embed the build
    # path in their shebang and nothing here uses them; keep only Python.
    bindir = os.path.join(runtime, "Scripts") if WINDOWS else os.path.join(runtime, "bin")
    if WINDOWS:
        shutil.rmtree(bindir, ignore_errors=True)
    else:
        for f in os.listdir(bindir):
            if not f.startswith("python") or f.endswith("-config"):
                os.unlink(os.path.join(bindir, f))
    for dirpath, dirnames, _files in os.walk(runtime):
        for d in list(dirnames):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, d))
                dirnames.remove(d)

    # 5. the BirdNET model
    run([py, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from birdsong import engine; "
                   "engine.download_model(sys.argv[2])", stage, os.path.join(stage, "data")])
    with open(os.path.join(stage, "data", "model", "LICENSE-BirdNET.txt"), "w", encoding="utf-8") as fh:
        fh.write(MODEL_NOTICE)

    # 6. prove it: copy elsewhere and run the real launcher's self-test
    if not skip_test:
        moved = os.path.join(build_dir, "moved here", f"birdsong-{version}")
        shutil.copytree(stage, moved, symlinks=True)
        env = dict(os.environ, BIRDSONG_NOPAUSE="1", PYTHONDONTWRITEBYTECODE="1")
        env.pop("VIRTUAL_ENV", None)
        if WINDOWS:
            run(["cmd", "/c", "run.bat", "selftest"], cwd=moved, env=env)
        else:
            run(["/bin/sh", "run.sh", "selftest"], cwd=moved, env=env)
        if os.path.exists(os.path.join(moved, ".runtime")):
            sys.exit("the launcher ignored the bundled runtime and bootstrapped uv")
        shutil.rmtree(os.path.dirname(moved))

    # 7. zip (symlinks kept as links, Unix permissions kept)
    os.makedirs(out, exist_ok=True)
    zpath = os.path.join(out, f"birdsong-{version}-{name}.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for dirpath, dirnames, files in os.walk(stage):
            dirnames.sort()
            for f in sorted(files) + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
                full = os.path.join(dirpath, f)
                arc = os.path.relpath(full, build_dir).replace(os.sep, "/")
                if os.path.islink(full):
                    zi = zipfile.ZipInfo(arc)
                    zi.create_system = 3
                    zi.external_attr = (stat.S_IFLNK | 0o777) << 16
                    zf.writestr(zi, os.readlink(full))
                elif os.path.isfile(full):
                    zi = zipfile.ZipInfo.from_file(full, arc)
                    if not WINDOWS:
                        zi.create_system = 3
                    big = f.endswith((".tflite", ".woff2", ".png", ".zip"))
                    with open(full, "rb") as fh:
                        zf.writestr(zi, fh.read(), zipfile.ZIP_STORED if big else zipfile.ZIP_DEFLATED)
    h = hashlib.sha256()
    with open(zpath, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    with open(zpath + ".sha256", "w") as fh:
        fh.write(f"{h.hexdigest()}  {os.path.basename(zpath)}\n")
    print(f"built {zpath} ({os.path.getsize(zpath) / 1e6:.0f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="e.g. windows-x64, linux-x64, linux-arm64, macos-arm64")
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--skip-test", action="store_true")
    a = ap.parse_args()
    build(a.name, a.version.lstrip("v"), a.out, a.skip_test)
