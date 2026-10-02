#!/bin/sh
# Birdsong launcher for Linux, macOS and Raspberry Pi.
#
# Release zips include a ready-made Python in runtime/ and just use that.
# From a git checkout, the first run downloads uv (a small Python manager) into .runtime/,
# which then fetches its own Python 3.12 and the libraries into .runtime/
# as well. Nothing is installed system-wide; delete the folder to remove it.
#
#   ./run.sh              listen + serve the page (runs setup the first time)
#   ./run.sh setup        change settings      ./run.sh demo    preview
#   ./run.sh --help       all commands
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"
RT="$HERE/.runtime"

# Release downloads ship a ready-made Python in runtime/: use it directly.
if [ -x "$HERE/runtime/bin/python3" ]; then
    if [ "$(uname -s)" = "Darwin" ]; then
        # Files from a downloaded zip carry Apple's quarantine flag, which
        # makes Gatekeeper refuse to load the bundled Python's libraries.
        xattr -dr com.apple.quarantine "$HERE" 2>/dev/null || true
    fi
    exec "$HERE/runtime/bin/python3" server.py "$@"
fi

if command -v uv >/dev/null 2>&1 && [ -z "$BIRDSONG_OWN_UV" ]; then
    UV=uv
else
    UV="$RT/uv/uv"
    if [ ! -x "$UV" ]; then
        case "$(uname -s)-$(uname -m)" in
            Linux-x86_64)               T=x86_64-unknown-linux-gnu ;;
            Linux-aarch64|Linux-arm64)  T=aarch64-unknown-linux-gnu ;;
            Darwin-arm64)               T=aarch64-apple-darwin ;;
            Darwin-x86_64)              T=x86_64-apple-darwin ;;
            *) echo "Sorry, $(uname -s) $(uname -m) isn't supported by the launcher."
               echo "Install Python 3.10-3.13, then: pip install -r requirements.txt && python3 server.py"
               exit 1 ;;
        esac
        URL="https://github.com/astral-sh/uv/releases/latest/download/uv-$T.tar.gz"
        echo "First run: downloading uv (Python manager) into .runtime/ ..."
        mkdir -p "$RT/uv"
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL "$URL" | tar xz -C "$RT/uv" --strip-components=1
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- "$URL" | tar xz -C "$RT/uv" --strip-components=1
        else
            echo "Need curl or wget to download uv."; exit 1
        fi
    fi
fi

# Keep Python, packages and caches inside this folder.
export UV_PYTHON_INSTALL_DIR="$RT/python"
export UV_CACHE_DIR="$RT/cache"
export UV_PROJECT_ENVIRONMENT="$RT/venv"
export UV_PYTHON_PREFERENCE=only-managed
export UV_NO_PROGRESS=1
exec "$UV" run --quiet --frozen python server.py "$@"
