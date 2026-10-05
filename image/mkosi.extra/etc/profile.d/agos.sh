# shellcheck shell=sh
# agos: login shells of the agent user (SSH, consoles) see the same desktop and
# secrets as the desktop session. Rendered values live in
# ~/.config/environment.d (written by agos-firstboot).
if [ "$(id -u)" = 1000 ]; then
    export DISPLAY="${DISPLAY:-:1}"
    for _agos_env in "$HOME"/.config/environment.d/*.conf; do
        [ -r "$_agos_env" ] || continue
        set -a
        # shellcheck disable=SC1090
        . "$_agos_env"
        set +a
    done
    unset _agos_env
fi
