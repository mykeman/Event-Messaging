#!/usr/bin/env bash
# Update an installed CuePlayer (player or master) to the latest code, keeping media,
# settings and the systemd service as they are. Run from the cloned repository:
#
#   cd Event-Messaging && sudo ./deploy/update.sh
#
# Use --no-pull to install the working copy as-is (e.g. after copying files over).
# For OS package changes or new install options, re-run deploy/install.sh instead.
set -euo pipefail

PULL=1
PREFIX=/opt/cueplayer
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull) PULL=0; shift ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
[[ -x "$PREFIX/venv/bin/python" ]] || { echo "CuePlayer is not installed in $PREFIX - run deploy/install.sh"; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$PULL" == 1 && -d "$SRC/.git" ]]; then
  echo "==> Pulling latest code"
  OWNER=$(stat -c %U "$SRC")
  sudo -u "$OWNER" git -C "$SRC" pull --ff-only
fi
echo "==> Updating $PREFIX from $SRC ($(git -C "$SRC" log -1 --format='%h %s' 2>/dev/null || echo 'working copy'))"

rm -rf "$PREFIX/cueplayer.new"
cp -r "$SRC/cueplayer" "$PREFIX/cueplayer.new"
find "$PREFIX/cueplayer.new" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$PREFIX/cueplayer.old"
[[ -d "$PREFIX/cueplayer" ]] && mv "$PREFIX/cueplayer" "$PREFIX/cueplayer.old"
mv "$PREFIX/cueplayer.new" "$PREFIX/cueplayer"
cp "$SRC/requirements.txt" "$PREFIX/"
[[ -d "$SRC/qsys" ]] && cp -r "$SRC/qsys" "$PREFIX/"
"$PREFIX/venv/bin/pip" install -q -r "$PREFIX/requirements.txt"

echo "==> Restarting service"
systemctl restart cueplayer.service
sleep 3
if systemctl is-active --quiet cueplayer.service; then
  rm -rf "$PREFIX/cueplayer.old"
  echo "==> Updated and running. Logs: journalctl -u cueplayer -f"
else
  echo "!! Service failed to start - rolling back"
  journalctl -u cueplayer -n 30 --no-pager || true
  if [[ -d "$PREFIX/cueplayer.old" ]]; then
    rm -rf "$PREFIX/cueplayer"; mv "$PREFIX/cueplayer.old" "$PREFIX/cueplayer"
    systemctl restart cueplayer.service
    echo "   Previous version restored."
  fi
  exit 1
fi
