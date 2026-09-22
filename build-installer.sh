#!/bin/bash
set -euo pipefail

if (( $# < 2 || $# > 3 )); then
    echo 'Usage: build-installer.sh INPUT-SelfInstall.iso OUTPUT.iso [CONFIG-DIRECTORY]' >&2
    exit 2
fi

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
input=$(realpath -e -- "$1")
output=$(realpath -m -- "$2")
config=$(realpath -e -- "${3:-$root/build/nuc-config}")

test -f "$input"
test -f "$config/combustion/script"
if [[ -e "$output" ]]; then
    echo "Refusing to overwrite $output" >&2
    exit 1
fi
mkdir -p -- "$(dirname -- "$output")" "$root/build/mkmedia-tmp"

# Rootless Podman works: mkmedia extracts the ISO instead of loop-mounting it.
# Host root and --privileged are unnecessary for this ISO creation step.
buildah bud -t localhost/nuc-kiosk-mkmedia -f "$root/installer/Containerfile" "$root/installer"
podman run --rm \
    -v "$input:/input/source.iso:ro,Z" \
    -v "$config:/config:ro,Z" \
    -v "$root/build/mkmedia-tmp:/scratch:Z" \
    -v "$(dirname -- "$output"):/output:Z" \
    localhost/nuc-kiosk-mkmedia \
    --no-mount-iso --no-rebuild-initrd --xorriso --tmp-dir /scratch \
    --create "/output/$(basename -- "$output")" --volume INSTALL \
    /input/source.iso /config
echo "Created $output"
