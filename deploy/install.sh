#!/usr/bin/env bash
# CuePlayer installer for Debian 12+/Ubuntu 22.04+/Raspberry Pi OS (Bookworm).
#
#   sudo ./deploy/install.sh                  # screen endpoint (player)
#   sudo ./deploy/install.sh --role master    # master server VM (no video stack)
#   sudo ./deploy/install.sh --with-ndi       # also install NDI output (see install-ndi.sh)
#   sudo ./deploy/install.sh --take-hdmi      # dedicate the HDMI output to CuePlayer (appliance mode)
#        [--hdmi-mode 1920x1080@60]           #   boot in this mode
#        [--hdmi-port HDMI-A-1]               #   which port (default: the one with a display plugged in)
#        [--hdmi-force]                       #   keep that port on even with nothing plugged in
#   sudo ./deploy/install.sh --release-hdmi   # undo --take-hdmi (restores the boot files), then reboot
#
# --take-hdmi: no boot text, logo, cursor or login prompt on HDMI, no desktop, the given
# mode at boot, and the player set to direct KMS output. Needs a reboot. Log in over SSH
# afterwards (the local console moves to tty3, Ctrl+Alt+F3).
# --hdmi-force is for LED processors that are powered up after the player or have no EDID.
# The forced port then always reports "connected", so only use it on the port in use.
#
# Re-running upgrades the app in place; media and settings in /var/lib/cueplayer are kept.
set -euo pipefail

ROLE=player
WITH_NDI=0
TAKE_HDMI=0
RELEASE_HDMI=0
HDMI_FORCE=0
HDMI_MODE=1920x1080@60
HDMI_PORT=""
PREFIX=/opt/cueplayer
DATA=/var/lib/cueplayer
SVC_USER=cueplayer

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --with-ndi) WITH_NDI=1; shift ;;
    --take-hdmi) TAKE_HDMI=1; shift ;;
    --release-hdmi) RELEASE_HDMI=1; shift ;;
    --hdmi-force) HDMI_FORCE=1; shift ;;
    --hdmi-mode) HDMI_MODE="$2"; shift 2 ;;
    --hdmi-port) HDMI_PORT="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --data) DATA="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done
[[ "$ROLE" == player || "$ROLE" == master ]] || { echo "--role must be player or master"; exit 1; }
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

# ---- undo --take-hdmi ----------------------------------------------------------
if [[ "$RELEASE_HDMI" == 1 ]]; then
  echo "==> Releasing HDMI (undoing --take-hdmi)"
  for f in /boot/firmware/cmdline.txt /boot/cmdline.txt /boot/firmware/config.txt /boot/config.txt; do
    if [[ -f "$f.cueplayer.bak" ]]; then
      cp "$f.cueplayer.bak" "$f" && rm -f "$f.cueplayer.bak" && echo "    restored $f"
    elif [[ -f "$f" && "$f" == *cmdline.txt ]]; then
      sed -i 's/ video=HDMI-A-[0-9]:[^ ]*//g' "$f" && echo "    removed video= from $f"
    fi
  done
  if [[ -f /etc/default/grub.cueplayer.bak ]]; then
    cp /etc/default/grub.cueplayer.bak /etc/default/grub && rm -f /etc/default/grub.cueplayer.bak
    update-grub
    echo "    restored /etc/default/grub"
  fi
  systemctl unmask getty@tty1.service autovt@tty1.service >/dev/null 2>&1 || true
  echo "==> Done. Reboot to apply:  sudo reboot"
  echo "    (a desktop that --take-hdmi disabled can be re-enabled with: sudo systemctl set-default graphical.target)"
  exit 0
fi
if [[ "$TAKE_HDMI" == 1 ]]; then
  [[ "$ROLE" == player ]] || { echo "--take-hdmi only applies to --role player"; exit 1; }
  [[ "$HDMI_MODE" =~ ^([0-9]+)x([0-9]+)@([0-9]+)$ ]] || { echo "--hdmi-mode must look like 1920x1080@60"; exit 1; }
  HDMI_W=${BASH_REMATCH[1]}; HDMI_H=${BASH_REMATCH[2]}
  if [[ -z "$HDMI_PORT" ]]; then
    # Use the port a display is actually plugged into (it has an EDID). Forcing the wrong
    # port on sends the video to an empty socket while the screen shows a blank console.
    for p in /sys/class/drm/card*-HDMI-A-*; do
      [[ -e "$p/edid" && -s "$p/edid" ]] && { HDMI_PORT=$(basename "$p" | sed 's/^card[0-9]*-//'); break; }
    done
    if [[ -n "$HDMI_PORT" ]]; then
      echo "==> Display detected on $HDMI_PORT"
    else
      HDMI_PORT=HDMI-A-1
      echo "!! No display detected on any HDMI port - using $HDMI_PORT (override with --hdmi-port)"
    fi
  fi
  [[ "$HDMI_PORT" =~ ^HDMI-A-[0-9]$ ]] || { echo "--hdmi-port must look like HDMI-A-1"; exit 1; }
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
echo "==> Installing CuePlayer ($ROLE) from $SRC to $PREFIX"

export DEBIAN_FRONTEND=noninteractive
apt-get update
PKGS=(python3 python3-venv python3-pip ffmpeg ca-certificates curl)
if [[ "$ROLE" == player ]]; then
  PKGS+=(python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
         gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good
         gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav
         gstreamer1.0-alsa gstreamer1.0-gl gstreamer1.0-x)
  # Optional on some distros
  apt-cache show gstreamer1.0-pulseaudio >/dev/null 2>&1 && PKGS+=(gstreamer1.0-pulseaudio)
fi
[[ "$ROLE" == player ]] && PKGS+=(libcap2-bin)
apt-get install -y --no-install-recommends "${PKGS[@]}"

# AES67 output: GStreamer's PTP clock runs gst-ptp-helper, which must bind the PTP ports
# 319/320. Give it the capabilities rather than running CuePlayer as root.
if [[ "$ROLE" == player ]]; then
  PTP_HELPER=$(find /usr/libexec /usr/lib -name gst-ptp-helper -type f 2>/dev/null | head -n1)
  if [[ -n "$PTP_HELPER" ]]; then
    setcap cap_net_bind_service,cap_net_admin+ep "$PTP_HELPER" \
      && echo "==> PTP helper ready: $PTP_HELPER" \
      || echo "!! could not setcap $PTP_HELPER - AES67 output will not get PTP"
  else
    echo "!! gst-ptp-helper not found - AES67 output will not get PTP"
  fi
fi

# Service user with access to the display (DRM/KMS) and audio devices
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$SVC_USER"
fi
for g in video render audio input; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$SVC_USER"
done

# App + virtualenv (system site packages give us python3-gi / GStreamer)
mkdir -p "$PREFIX" "$DATA"
rm -rf "$PREFIX/cueplayer"
cp -r "$SRC/cueplayer" "$SRC/requirements.txt" "$PREFIX/"
[[ -d "$SRC/qsys" ]] && cp -r "$SRC/qsys" "$PREFIX/"
if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
  python3 -m venv --system-site-packages "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install --upgrade pip >/dev/null
"$PREFIX/venv/bin/pip" install -r "$PREFIX/requirements.txt"
chown -R "$SVC_USER:$SVC_USER" "$DATA"

# systemd
cat > /etc/systemd/system/cueplayer.service <<EOF
[Unit]
Description=CuePlayer ($ROLE)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
SupplementaryGroups=video render audio input
Environment=CUEPLAYER_ROLE=$ROLE
Environment=CUEPLAYER_DATA=$DATA
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$PREFIX
ExecStart=$PREFIX/venv/bin/python -m cueplayer
Restart=always
RestartSec=3
# Allow binding the default ports as a normal user and keep the realtime-ish priority
Nice=-5
LimitMEMLOCK=infinity

[Install]
WantedBy=multi-user.target
EOF

if [[ "$ROLE" == player ]]; then
  # A screen endpoint should not blank or show a login prompt on the output.
  if [[ -f /boot/firmware/cmdline.txt ]] && ! grep -q consoleblank=0 /boot/firmware/cmdline.txt; then
    sed -i 's/$/ consoleblank=0/' /boot/firmware/cmdline.txt
  fi
  if [[ "$TAKE_HDMI" == 0 ]] && systemctl is-enabled display-manager >/dev/null 2>&1; then
    echo "!! A desktop (display manager) is enabled. For direct KMS output set the"
    echo "   video sink to 'kms' and boot to console:  sudo systemctl set-default multi-user.target"
    echo "   or keep the desktop and use video sink 'auto'/'gl'."
  fi
fi

# ---- appliance mode: dedicate HDMI to the player -------------------------------
NEED_REBOOT=0
if [[ "$TAKE_HDMI" == 1 ]]; then
  echo "==> Dedicating $HDMI_PORT to CuePlayer at $HDMI_MODE$([[ $HDMI_FORCE == 1 ]] && echo ' (forced on)')"
  # Kernel command line: console off tty1, quiet boot, no logo/cursor/blanking, HDMI forced on.
  # With --hdmi-force, the "D" suffix on video= keeps the port on even if no display is
  # detected (LED processors powered up after the player, or with unreliable EDID). The
  # port then always reports "connected", so it must be the port actually in use.
  edit_cmdline() {
    python3 - "$1" "$HDMI_PORT" "$HDMI_MODE" "$HDMI_FORCE" <<'EOF'
import sys
path, port, mode, force = sys.argv[1:5]
toks = open(path).read().split()
toks = [t for t in toks if not (t.startswith("video=HDMI-A-") or t in ("console=tty1", "console=tty3", "quiet")
        or t.split("=")[0] in ("loglevel", "logo.nologo", "vt.global_cursor_default", "consoleblank"))]
toks += ["console=tty3", "quiet", "loglevel=3", "logo.nologo", "vt.global_cursor_default=0",
         "consoleblank=0", f"video={port}:{mode}" + ("D" if force == "1" else "")]
open(path, "w").write(" ".join(toks) + "\n")
EOF
  }
  CMDLINE=""
  for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do [[ -f "$f" ]] && { CMDLINE=$f; break; }; done
  if [[ -n "$CMDLINE" ]]; then                       # Raspberry Pi
    [[ -f "$CMDLINE.cueplayer.bak" ]] || cp "$CMDLINE" "$CMDLINE.cueplayer.bak"
    edit_cmdline "$CMDLINE"
    CONFIG="$(dirname "$CMDLINE")/config.txt"
    if [[ -f "$CONFIG" ]]; then
      [[ -f "$CONFIG.cueplayer.bak" ]] || cp "$CONFIG" "$CONFIG.cueplayer.bak"
      grep -q '^disable_splash=1' "$CONFIG" || echo "disable_splash=1" >> "$CONFIG"   # no rainbow screen
      grep -q '^dtoverlay=vc4-kms-v3d' "$CONFIG" || \
        echo "!! $CONFIG has no dtoverlay=vc4-kms-v3d - direct KMS output needs it (Bookworm default)"
    fi
  elif [[ -f /etc/default/grub ]]; then              # x86 PC / VM with GRUB
    [[ -f /etc/default/grub.cueplayer.bak ]] || cp /etc/default/grub /etc/default/grub.cueplayer.bak
    TMP=$(mktemp)
    sed -n 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"$/\1/p' /etc/default/grub > "$TMP"
    edit_cmdline "$TMP"
    NEW=$(cat "$TMP"); rm -f "$TMP"
    sed -i "s|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT=\"$NEW\"|" /etc/default/grub
    update-grub
  else
    echo "!! No Pi cmdline.txt or GRUB config found - skipping boot-console changes"
  fi

  # No login prompt on the screen (SSH still works)
  systemctl mask getty@tty1.service autovt@tty1.service >/dev/null 2>&1 || true
  # No desktop competing for the display
  if [[ "$(systemctl get-default)" == graphical.target ]]; then
    systemctl set-default multi-user.target
    echo "    Desktop disabled (boot to console). Undo: sudo systemctl set-default graphical.target"
  fi

  # Player output: direct KMS at the HDMI mode (keeps any other existing settings)
  python3 - "$DATA/settings.json" "$HDMI_W" "$HDMI_H" <<'EOF'
import json, sys
path, w, h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    data = json.load(open(path))
except (OSError, ValueError):
    data = {}
data.setdefault("display", {}).update(enabled=True, sink="kms", width=w, height=h)
json.dump(data, open(path, "w"), indent=2)
EOF
  chown "$SVC_USER:$SVC_USER" "$DATA/settings.json"
  NEED_REBOOT=1
fi

systemctl daemon-reload
systemctl enable cueplayer.service
systemctl restart cueplayer.service

if [[ "$WITH_NDI" == 1 && "$ROLE" == player ]]; then
  "$SRC/deploy/install-ndi.sh"
  systemctl restart cueplayer.service
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "==> CuePlayer ($ROLE) is running:  http://${IP:-<this-host>}:8080/"
echo "    TCP control port 7700, UDP 7701.  Logs: journalctl -u cueplayer -f"
[[ "$ROLE" == player && "$WITH_NDI" == 0 ]] && echo "    NDI output: run  sudo $SRC/deploy/install-ndi.sh"
if [[ "$NEED_REBOOT" == 1 ]]; then
  echo
  echo "==> HDMI takeover configured. Reboot to apply:  sudo reboot"
  echo "    Undo: sudo $SRC/deploy/install.sh --release-hdmi && sudo reboot"
fi
exit 0
