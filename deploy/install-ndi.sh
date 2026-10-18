#!/usr/bin/env bash
# Installs NDI output support for CuePlayer:
#   1. the NDI runtime (libndi) from the official NDI SDK for Linux (you must accept its licence),
#   2. the GStreamer NDI plugin (ndisink) from gst-plugins-rs, built from source with Rust.
#
#   sudo ./deploy/install-ndi.sh
#
# Building the plugin takes ~5 min on x86 and ~20-40 min on a Raspberry Pi 4/5.
# Env overrides: NDI_SDK_URL, GST_RS_REF (gst-plugins-rs git tag/branch).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

NDI_SDK_URL=${NDI_SDK_URL:-https://downloads.ndi.tv/SDK/NDI_SDK_Linux/Install_NDI_SDK_v6_Linux.tar.gz}
WORK=$(mktemp -d /tmp/cueplayer-ndi.XXXX)
ARCH=$(uname -m)
GST_VER=$(pkg-config --modversion gstreamer-1.0 2>/dev/null || gst-inspect-1.0 --version | awk '/GStreamer/{print $2; exit}')
echo "==> GStreamer $GST_VER on $ARCH"

if gst-inspect-1.0 ndisink >/dev/null 2>&1 && ldconfig -p | grep -q libndi.so; then
  echo "==> ndisink and libndi already installed - nothing to do"; exit 0
fi

# ---- 1. NDI runtime --------------------------------------------------------
if ! ldconfig -p | grep -q 'libndi.so'; then
  echo "==> Downloading NDI SDK from $NDI_SDK_URL"
  curl -fL "$NDI_SDK_URL" -o "$WORK/ndi.tar.gz"
  tar -xzf "$WORK/ndi.tar.gz" -C "$WORK"
  INSTALLER=$(ls "$WORK"/Install_NDI_SDK_*_Linux.sh | head -1)
  echo "==> The NDI SDK licence will be shown. You must accept it to continue."
  (cd "$WORK" && bash "$INSTALLER")
  SDK_DIR=$(ls -d "$WORK"/NDI\ SDK\ for\ Linux* | head -1)
  case "$ARCH" in
    x86_64) LIBDIR="x86_64-linux-gnu" ;;
    aarch64) LIBDIR=$(ls "$SDK_DIR/lib" | grep -E '^aarch64' | head -1) ;;
    armv7l) LIBDIR=$(ls "$SDK_DIR/lib" | grep -E '^arm-rpi4|^arm-rpi3' | head -1) ;;
    *) echo "unsupported arch $ARCH"; exit 1 ;;
  esac
  echo "==> Installing libndi from $LIBDIR"
  cp -P "$SDK_DIR/lib/$LIBDIR/"libndi.so* /usr/local/lib/
  ldconfig
  # gst-plugin-ndi dlopens libndi.so.6 (or $NDI_RUNTIME_DIR_V6)
  ldconfig -p | grep -q 'libndi.so.6' || { echo "libndi.so.6 not found after install"; exit 1; }
fi

# ---- 2. GStreamer NDI plugin -------------------------------------------------
if ! gst-inspect-1.0 ndisink >/dev/null 2>&1; then
  # Some newer distros package it
  for pkg in gstreamer1.0-plugins-rs-ndi gst-plugin-ndi; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then apt-get install -y "$pkg" && break; fi
  done
fi
if ! gst-inspect-1.0 ndisink >/dev/null 2>&1; then
  echo "==> Building gst-plugin-ndi from source"
  apt-get install -y --no-install-recommends git build-essential pkg-config \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
  export CARGO_HOME="$WORK/cargo" RUSTUP_HOME="$WORK/rustup"
  curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal
  # shellcheck disable=SC1091
  source "$CARGO_HOME/env"
  # Pick a gst-plugins-rs release that supports the system GStreamer
  if [[ -z "${GST_RS_REF:-}" ]]; then
    case "$GST_VER" in
      1.20.*|1.22.*) GST_RS_REF=0.12 ;;
      1.24.*) GST_RS_REF=0.13 ;;
      *) GST_RS_REF=main ;;
    esac
  fi
  git clone --depth 1 --branch "$GST_RS_REF" https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git "$WORK/gst-plugins-rs"
  (cd "$WORK/gst-plugins-rs" && cargo build --release -p gst-plugin-ndi)
  PLUGDIR=$(pkg-config --variable=pluginsdir gstreamer-1.0)
  install -m 0644 "$WORK/gst-plugins-rs/target/release/libgstndi.so" "$PLUGDIR/"
  rm -rf ~/.cache/gstreamer-1.0 /var/lib/cueplayer/.cache/gstreamer-1.0 2>/dev/null || true
fi

if gst-inspect-1.0 ndisink >/dev/null 2>&1; then
  echo "==> NDI ready: ndisink available"
else
  echo "!! ndisink still not available - check the output above"; exit 1
fi
rm -rf "$WORK"
