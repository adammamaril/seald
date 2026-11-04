#!/usr/bin/env bash
# install-deps.sh — install what seal needs: sops, age (age-keygen) and jq.
#
# Usage:  ./scripts/install-deps.sh        (or: make deps)
#         DRY_RUN=1 ./scripts/install-deps.sh   print the commands, change nothing
#
# Uses brew, apt, pacman or dnf for age and jq. sops comes from the same package
# manager when it has it; otherwise the official release binary is downloaded,
# checked against the published SHA-256 checksums, and put in ~/.local/bin.
set -euo pipefail

SOPS_VERSION=${SOPS_VERSION:-3.13.3}
BIN_DIR=${BIN_DIR:-$HOME/.local/bin}

say() { printf 'install-deps: %s\n' "$*" >&2; }
run() { say "+ $*"; [ -n "${DRY_RUN:-}" ] || "$@"; }
have() { command -v "$1" >/dev/null 2>&1; }
sudo_() { if [ "$(id -u)" = 0 ]; then run "$@"; else run sudo "$@"; fi; }

missing=()
for tool in sops age age-keygen jq; do have "$tool" || missing+=("$tool"); done
if [ ${#missing[@]} -eq 0 ]; then say "sops, age and jq are already installed"; exit 0; fi
say "missing: ${missing[*]}"

if have brew; then
  run brew install sops age jq
  exit 0
elif have apt-get; then
  sudo_ apt-get update
  sudo_ apt-get install -y age jq
  sudo_ apt-get install -y sops 2>/dev/null || true
elif have pacman; then
  sudo_ pacman -S --needed --noconfirm sops age jq
  exit 0
elif have dnf; then
  sudo_ dnf install -y age jq
  sudo_ dnf install -y sops 2>/dev/null || true
else
  say "no supported package manager (brew, apt, pacman, dnf); install sops, age and jq by hand"
  exit 1
fi

[ -z "${DRY_RUN:-}" ] && have sops && exit 0

# sops is not in this distro's repositories: fetch the release binary and verify it.
case $(uname -m) in
  x86_64 | amd64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) say "no sops release binary for $(uname -m); install sops by hand"; exit 1 ;;
esac
have curl || { say "curl is needed to download sops"; exit 1; }
base=https://github.com/getsops/sops/releases/download/v$SOPS_VERSION
file=sops-v$SOPS_VERSION.linux.$arch
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
run curl -fsSLo "$tmp/$file" "$base/$file"
run curl -fsSLo "$tmp/checksums.txt" "$base/sops-v$SOPS_VERSION.checksums.txt"
if [ -z "${DRY_RUN:-}" ]; then
  (cd "$tmp" && grep " $file\$" checksums.txt | sha256sum -c --quiet -) \
    || { say "checksum mismatch for $file; not installing"; exit 1; }
fi
run install -d "$BIN_DIR"
run install -m 0755 "$tmp/$file" "$BIN_DIR/sops"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) say "add $BIN_DIR to your PATH to use sops" ;; esac
say "sops $SOPS_VERSION installed to $BIN_DIR/sops"
