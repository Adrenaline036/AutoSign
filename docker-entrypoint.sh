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

# The VNC server is reachable only from inside this container. AutoSign exposes
# it through its authenticated WebSocket bridge, so no raw VNC port or separate
# VNC password is exposed to the LAN.
x11vnc -display :99 -rfbport 5900 -localhost -forever -shared -nopw \
    -noxdamage -quiet >/tmp/autosign-x11vnc.log 2>&1 &
x11vnc_pid=$!
if ! kill -0 "$x11vnc_pid" 2>/dev/null; then
    wait "$x11vnc_pid"
    exit 1
fi

exec python -m autosign
