#!/bin/sh
set -eu

# `docker restart` preserves the container writable layer. Xvfb cannot clean these
# files after Docker terminates it, so remove the stale display artifacts first.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
xvfb_pid=$!
export DISPLAY=:99

attempt=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
        wait "$xvfb_pid"
        exit 1
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 50 ]; then
        echo "Xvfb did not create display :99 within 5 seconds." >&2
        kill "$xvfb_pid" 2>/dev/null || true
        exit 1
    fi
    sleep 0.1
done

exec python -m autosign
