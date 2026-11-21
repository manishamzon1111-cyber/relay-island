#!/usr/bin/env bash
set -Eeuo pipefail
echo "JIDU6101 OpenWrt: download-only setup (NO FLASHING)"
if ! command -v git >/dev/null || ! command -v curl >/dev/null || ! command -v python3 >/dev/null; then
  echo "Installing prerequisites (may ask for your Linux login password)..."
  sudo apt-get update
  sudo apt-get install -y git curl python3 openssh-client
fi
cd "$HOME"
if [ -d idu-unlock/.git ]; then
  echo "Using existing ~/idu-unlock checkout (not overwriting files)"
else
  git clone https://github.com/wpfyorg/idu-unlock.git
fi
cd idu-unlock
bash firmware/fwflash.sh 6101 --download-only
echo "Completed download-only preparation. NOTHING has been flashed."
echo "Files: $HOME/idu-unlock/firmware/downloads/6101/"
