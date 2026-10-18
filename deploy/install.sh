#!/usr/bin/env bash
# CuePlayer installer for Debian 12+/Ubuntu 22.04+/Raspberry Pi OS (Bookworm).
#
#   sudo ./deploy/install.sh                  # screen endpoint (player)
#   sudo ./deploy/install.sh --role master    # master server VM (no video stack)
#   sudo ./deploy/install.sh --with-ndi       # also install NDI output (see install-ndi.sh)
#
# Re-running upgrades the app in place; media and settings in /var/lib/cueplayer are kept.
set -euo pipefail

ROLE=player
WITH_NDI=0
PREFIX=/opt/cueplayer
DATA=/var/lib/cueplayer
SVC_USER=cueplayer

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --with-ndi) WITH_NDI=1; shift ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --data) DATA="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done
[[ "$ROLE" == player || "$ROLE" == master ]] || { echo "--role must be player or master"; exit 1; }
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

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
apt-get install -y --no-install-recommends "${PKGS[@]}"

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
  if systemctl is-enabled display-manager >/dev/null 2>&1; then
    echo "!! A desktop (display manager) is enabled. For direct KMS output set the"
    echo "   video sink to 'kms' and boot to console:  sudo systemctl set-default multi-user.target"
    echo "   or keep the desktop and use video sink 'auto'/'gl'."
  fi
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
exit 0
