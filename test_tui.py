#!/usr/bin/env python3
"""Visual smoke test for the pivoice TUI. Drives the animated status panel
through every state while fake streaming text scrolls above it. No mic/pi.

Run: python3 test_tui.py
Quit: q / Ctrl-C
"""
import sys
import time
import threading
from pivoice import TUI, LockedStdout, RawTerm, c, color256

sys.stdout = LockedStdout(sys.stdout)
tui = TUI()
tui.setup()
sys.stdout.write("\033[1;1H")
sys.stdout.flush()

print(color256(51, "╭─ TUI smoke test ────────────────────────────────╮"))
print(color256(51, "│  watching the animated equalizer below.         │"))
print(color256(51, "╰─────────────────────────────────────────────────╯"))
print()
sys.stdout.flush()

tui.start()

states = [
    ("idle", "waiting for input"),
    ("recording", "press again to send"),
    ("transcribing", "whisper.cpp decoding"),
    ("thinking", "pi is reasoning"),
    ("speaking", "pi is responding — streaming text"),
]

done = threading.Event()


def driver():
    # cycle states a few times, streaming filler text during 'speaking'
    for cycle in range(3):
        for st, detail in states:
            tui.set_state(st, detail)
            if st == "speaking":
                msg = (f"[cycle {cycle}] The quick brown fox jumps over the lazy dog. "
                       "Pack my box with five dozen liquor jugs. " * 3)
                # stream word by word to simulate deltas
                words = msg.split()
                line = ""
                for w in words:
                    if done.is_set():
                        return
                    line += w + " "
                    sys.stdout.write(w + " ")
                    sys.stdout.flush()
                    time.sleep(0.03)
                sys.stdout.write("\n")
                sys.stdout.flush()
            else:
                time.sleep(1.2)
    tui.set_state("idle", "done — press q to quit")
    print(c("green", "\n[driver finished — press q to exit]"))
    sys.stdout.flush()


th = threading.Thread(target=driver, daemon=True)
th.start()

with RawTerm() as term:
    try:
        while True:
            k = term.getkey().lower()
            if k in ("q", "\x03"):
                break
    except KeyboardInterrupt:
        pass

done.set()
tui.teardown()
print(c("grey", "bye."))
