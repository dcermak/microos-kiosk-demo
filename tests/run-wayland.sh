#!/bin/bash
# Run the production image inside the current user's Wayland desktop.
set -euo pipefail
if (( $# != 3 )); then
    echo 'Usage: tests/run-wayland.sh IMAGE LEFT_URL RIGHT_URL' >&2
    exit 2
fi
if (( EUID == 0 )); then
    echo 'Run this helper as your desktop user, without sudo.' >&2
    exit 1
fi
command -v podman >/dev/null
: "${WAYLAND_DISPLAY:?Run this from a Wayland desktop session}"
socket="$WAYLAND_DISPLAY"
if [[ "$socket" != /* ]]; then
    : "${XDG_RUNTIME_DIR:?Set XDG_RUNTIME_DIR for the desktop session}"
    socket="$XDG_RUNTIME_DIR/$socket"
fi
test -S "$socket" || { echo "Missing desktop Wayland socket: $socket" >&2; exit 1; }

# Do not relabel the desktop socket or expose the entire host runtime directory.
# Host networking lets preview URLs reach servers on the desktop's loopback.
# SYS_CHROOT allows Firefox's chroot helper through the default seccomp profile.
exec podman run --rm --init --network=host \
    --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
    --cap-drop=all --cap-add=SYS_CHROOT \
    --shm-size=512m --security-opt label=disable \
    --volume "$socket:/run/host-wayland.sock:ro" \
    --env WAYLAND_DISPLAY=/run/host-wayland.sock \
    --env WLR_BACKENDS=wayland --env WLR_RENDERER=pixman \
    --env LIBGL_ALWAYS_SOFTWARE=1 \
    --env "KIOSK_URL_LEFT=$2" --env "KIOSK_URL_RIGHT=$3" \
    "$1"
