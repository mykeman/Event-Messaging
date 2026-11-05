#!/usr/bin/env bash
# Installs the CuePlayer system helper (used by the web GUI for the HDMI boot mode, updates
# and reboot) and records where the git clone is. Called by install.sh and update.sh:
#
#   deploy/install-helper.sh <source clone> <prefix> <service user>
set -euo pipefail
SRC="$1"; PREFIX="$2"; SVC_USER="${3:-cueplayer}"
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

# Root-owned and not writable by the service user: the sudo rule below trusts this file
install -m 0755 -o root -g root "$SRC/deploy/cueplayer-helper" /usr/local/sbin/cueplayer-helper
if command -v sudo >/dev/null 2>&1 && [[ -d /etc/sudoers.d ]]; then
  TMP=$(mktemp)
  echo "$SVC_USER ALL=(root) NOPASSWD: /usr/local/sbin/cueplayer-helper" > "$TMP"
  if visudo -cf "$TMP" >/dev/null; then
    install -m 0440 -o root -g root "$TMP" /etc/sudoers.d/cueplayer
  else
    echo "!! sudoers rule failed validation - GUI update/HDMI mode/reboot disabled"
  fi
  rm -f "$TMP"
else
  echo "!! sudo is not installed - GUI update/HDMI mode/reboot disabled (apt install sudo)"
fi

# Where to update from, and what is installed now (shown in Settings > System)
echo "$SRC" > "$PREFIX/source"
chmod 0644 "$PREFIX/source"
python3 - "$SRC" "$PREFIX/cueplayer/BUILD.json" <<'PY'
import json, subprocess, sys
src, out = sys.argv[1], sys.argv[2]
def git(*a):
    r = subprocess.run(["git", "-c", f"safe.directory={src}", "-C", src, *a], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""
info = {"commit": git("rev-parse", "--short", "HEAD"), "date": git("log", "-1", "--format=%cI"),
        "subject": git("log", "-1", "--format=%s"), "branch": git("rev-parse", "--abbrev-ref", "HEAD")}
json.dump(info, open(out, "w"))
PY
chmod 0644 "$PREFIX/cueplayer/BUILD.json"
