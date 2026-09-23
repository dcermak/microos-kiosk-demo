#!/bin/bash
# Exercise the real entrypoint, Cage and Firefox without graphics devices.
set -euo pipefail

if (( $# != 1 )); then
    echo 'Usage: CONTAINER_ENGINE=podman|docker tests/container-smoke.sh IMAGE' >&2
    exit 2
fi
engine=${CONTAINER_ENGINE:-podman}
case "$engine" in podman|docker) ;; *) echo "Unsupported engine: $engine" >&2; exit 2 ;; esac
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# Resolve the tag to the exact image under test.
image=$("$engine" image inspect --format '{{.Id}}' "$1")
name="kiosk-smoke-$$-$RANDOM"
cleanup() {
    local status=$?
    if (( status != 0 )); then
        "$engine" logs "$name" >&2 || true
        "$engine" inspect "$name" >&2 || true
        "$engine" top "$name" >&2 || true
    fi
    "$engine" rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# SYS_CHROOT allows Firefox's chroot helper through the default seccomp profile.
"$engine" run --detach --name "$name" --init --network=none \
    --user=1000:1000 --cap-drop=all --cap-add=SYS_CHROOT \
    --shm-size=512m --stop-timeout=10 \
    --volume "$root/tests/http_fixture.py:/opt/kiosk-test.py:ro,Z" \
    --env WLR_BACKENDS=headless --env WLR_HEADLESS_OUTPUTS=1 \
    --env WLR_RENDERER=pixman --env LIBGL_ALWAYS_SOFTWARE=1 \
    --env KIOSK_URL_LEFT=http://127.0.0.1:8000/left \
    --env KIOSK_URL_RIGHT=http://127.0.0.1:8000/right \
    --entrypoint /bin/bash "$image" -euc '
        python3 -u /opt/kiosk-test.py serve &
        python3 /opt/kiosk-test.py ready
        exec /usr/local/bin/kiosk-start
    ' >/dev/null
"$engine" exec "$name" python3 /opt/kiosk-test.py loaded
sleep 3
"$engine" exec "$name" python3 /opt/kiosk-test.py check

# A surviving Firefox parent does not prove its sandboxed children are healthy.
# Check before stopping: shutdown itself can close IPC connections abnormally.
logs=$("$engine" logs "$name" 2>&1)
if grep -Eq 'Sandbox: chroot:|exited on signal (6|11)([^0-9]|$)|VideoBridgeParent.*reason=AbnormalShutdown' <<< "$logs"; then
    echo 'Firefox sandbox initialization or child process failed; see container logs.' >&2
    exit 1
fi

started=$SECONDS
"$engine" stop --time=10 "$name" >/dev/null
elapsed=$((SECONDS - started))
exit_code=$("$engine" inspect --format '{{.State.ExitCode}}' "$name")
running=$("$engine" inspect --format '{{.State.Running}}' "$name")
if [[ "$running" != false || "$exit_code" == 137 ]] || (( elapsed >= 10 )); then
    echo "Container did not stop promptly: running=$running exit=$exit_code elapsed=${elapsed}s" >&2
    exit 1
fi
"$engine" rm "$name" >/dev/null
echo "Both pages loaded; stop took ${elapsed}s."
echo 'Headless smoke test passed. Check native split-view layout visually before deployment.'
