# pivoice — Futuristic TUI spec

## Goal
Make the pivoice TUI look futuristic, with a persistent animated graph that
reacts to state while pi is speaking (streaming a reply). No new dependencies.

## REQ-1: persistent status panel (scroll region)
- REQ-1-1: Set a terminal scroll region (DECSTBM) reserving the bottom 3 rows
  for a status panel; conversation text scrolls within the top region only.
- REQ-1-2: On startup, after setting the region, draw the header (banner) at
  the top of the scroll region.
- REQ-1-3: On exit, reset the scroll region (`\033[r`) and restore cursor so
  the terminal is left clean.
- REQ-1-4: Verify: launching and quitting leaves the shell usable (prompt on
  a fresh line, no leftover scroll-region weirdness).

## REQ-2: thread-safe rendering
- REQ-2-1: Wrap `sys.stdout` in a `LockedStdout` proxy holding a
  `threading.Lock`; every `.write()`/`.flush()` acquires it.
- REQ-2-2: The animation frame acquires the same lock across the whole
  redraw (save-cursor → position → draw → restore-cursor) so frames are atomic
  and the cursor always returns to the conversation area.
- REQ-2-3: Verify: streaming text and animation never garble each other
  (visual check during a real turn).

## REQ-3: animated equalizer graph
- REQ-3-1: A background thread renders ~20 fps to the status panel row.
- REQ-3-2: Bars use block chars `▁▂▃▄▅▆▇█` (height 0-8) with a 256-color
  cyan→magenta gradient keyed by height.
- REQ-3-3: Amplitude/animation differs per state:
  - idle: dim shimmer (near-flat)
  - recording: red-tinted lively pulse
  - thinking: gentle low bars + braille spinner
  - speaking: lively full-range equalizer
- REQ-3-4: Bar count fills available width minus label/detail columns.
- REQ-3-5: Verify: each state visibly animates differently.

## REQ-4: state hooks
- REQ-4-1: push-to-talk start → `recording`; stop → `thinking`.
- REQ-4-2: `prompt()` sent → `thinking`.
- REQ-4-3: first `text_delta` after a prompt → `speaking`.
- REQ-4-4: `agent_end` → `idle`.
- REQ-4-5: abort → `idle`.
- REQ-4-6: Verify: state label + graph track the real flow end-to-end.

## REQ-5: futuristic header + hints
- REQ-5-1: Box-drawn header with cyan border; mic/stt/tts info inside.
- REQ-5-2: Hint line (SPACE talk / a abort / n new / c clear / q quit).
- REQ-5-3: Status panel: separator + `{spinner} {STATE} {bars} {detail}`.
- REQ-5-4: Verify: header renders cleanly at 80 cols; no wrapping artifacts.

## Non-goals
- Real audio-level FFT (would need loopback permissions). Animation is
  synthetic but reactive-looking. Can add real mic-level later.
- Full-screen curses rewrite (too invasive for this enhancement).

## Commit
`feat(tui): futuristic status panel with animated equalizer`
