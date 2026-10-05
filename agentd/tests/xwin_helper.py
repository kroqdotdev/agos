"""Test helper: map a window, focus it, print key and button events.

Output lines: "READY <id>", "CHAR <keysym> <char>", "KEY <keysym>",
"BUTTON <button> <x> <y> <state>".
"""

import sys

from Xlib import XK, X, Xatom
from Xlib import display as xdisplay


def main() -> None:
    d = xdisplay.Display(sys.argv[1])
    title = sys.argv[2]
    x, y, w, h = (int(v) for v in sys.argv[3].split(","))
    screen = d.screen()
    win = screen.root.create_window(
        x,
        y,
        w,
        h,
        0,
        screen.root_depth,
        X.InputOutput,
        X.CopyFromParent,
        background_pixel=screen.white_pixel,
        event_mask=X.KeyPressMask | X.ButtonPressMask | X.StructureNotifyMask | X.ExposureMask,
    )
    win.set_wm_name(title)
    win.set_wm_class("agentdtest", "AgentdTest")
    win.change_property(d.intern_atom("_NET_WM_NAME"), d.intern_atom("UTF8_STRING"), 8, title.encode())
    win.change_property(d.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [__import__("os").getpid()])
    win.map()
    d.sync()
    while True:
        ev = d.next_event()
        if ev.type == X.MapNotify:
            break
    win.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()
    print(f"READY {win.id}", flush=True)
    while True:
        ev = d.next_event()
        if ev.type == X.KeyPress:
            shift = 1 if ev.state & X.ShiftMask else 0
            sym = d.keycode_to_keysym(ev.detail, shift) or d.keycode_to_keysym(ev.detail, 0)
            name = XK.keysym_to_string(sym)
            if name is not None and len(name) == 1 and name.isprintable():
                print(f"CHAR {sym} {name}", flush=True)
            else:
                print(f"KEY {sym}", flush=True)
        elif ev.type == X.ButtonPress:
            print(f"BUTTON {ev.detail} {ev.event_x} {ev.event_y} {ev.state}", flush=True)


if __name__ == "__main__":
    main()
