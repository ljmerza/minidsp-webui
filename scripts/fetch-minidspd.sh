#!/usr/bin/env bash
# Download the minidspd release binary the image bundles (same version and SHA-256 pins as the Dockerfile) into a
# local cache, for the browser tests (tests/e2e), which run the app in embedded mode with minidspd's mock 2x4HD.
# Prints the binary's path. Nothing is downloaded when it is already cached.
#
#   scripts/fetch-minidspd.sh            -> ${XDG_CACHE_HOME:-~/.cache}/minidsp-webui/minidspd-<version>/minidspd
#   MINIDSPD_CACHE_DIR=/some/dir scripts/fetch-minidspd.sh
#
# The binary needs libusb-1.0 and libudev at run time (Debian/Ubuntu: libusb-1.0-0, libudev1).
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
arg() { sed -n "s/^ARG $1=//p" "$root/Dockerfile" | head -n1; }

version="$(arg MINIDSP_VERSION)"
case "$(uname -m)" in
    x86_64 | amd64) triple=x86_64-unknown-linux-gnu; sha="$(arg MINIDSP_SHA256_AMD64)" ;;
    aarch64 | arm64) triple=aarch64-unknown-linux-gnu; sha="$(arg MINIDSP_SHA256_ARM64)" ;;
    *) echo "fetch-minidspd: unsupported machine $(uname -m)" >&2; exit 1 ;;
esac
if [[ -z "$version" || -z "$sha" ]]; then
    echo "fetch-minidspd: couldn't read the MINIDSP_* pins from the Dockerfile" >&2
    exit 1
fi

cache="${MINIDSPD_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/minidsp-webui}"
dest="$cache/minidspd-$version"
if [[ -x "$dest/minidspd" ]]; then
    echo "$dest/minidspd"
    exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
url="https://github.com/mrene/minidsp-rs/releases/download/v${version}/minidsp.${triple}.tar.gz"
curl -fsSL --retry 3 -o "$tmp/minidsp.tar.gz" "$url"
echo "${sha}  $tmp/minidsp.tar.gz" | sha256sum -c - >&2
tar -xzf "$tmp/minidsp.tar.gz" -C "$tmp" minidspd
mkdir -p "$dest"
install -m 0755 "$tmp/minidspd" "$dest/minidspd"
echo "$dest/minidspd"
