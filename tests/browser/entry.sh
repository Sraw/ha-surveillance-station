# Headless Weston on the GPU; Chrome joins it over Wayland (headless Chrome can't use VA-API decode).
export XDG_RUNTIME_DIR=/tmp/xdg; mkdir -p -m 700 $XDG_RUNTIME_DIR
weston --backend=headless --renderer=gl --width=1920 --height=1080 --socket=wayland-1 >/tmp/weston.log 2>&1 &
for i in $(seq 50); do [ -S $XDG_RUNTIME_DIR/wayland-1 ] && break; sleep 0.1; done
export WAYLAND_DISPLAY=wayland-1
exec "$@"
