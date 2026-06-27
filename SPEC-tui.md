# pivoice — Futuristic TUI spec

## Goal
Make the pivoice TUI look futuristic, with a persistent animated graph that
reacts to state while pi is speaking (streaming a reply). No new dependencies.

## REQ-1: persistent status panel (alternate screen, full redraw)

**Revised** from the original scroll-region design. The DECSTBM +
cursor-save/restore approach garbled in practice: streaming text and the
panel share overlapping screen models and fight even under a lock. The
implemented architecture is the standard TUI model:

- REQ-1-1: Enter the **alternate screen buffer** (`\033[?1049h`), hide the
  cursor, clear. On exit, leave alt screen (`\033[?1049l`) and show cursor —
  the user's prior terminal state is fully restored.
- REQ-1-2: The conversation is an **in-memory log** (`TUI.entries`), not
  streamed to the terminal. `TUI.stream()` accumulates the in-progress
  assistant reply; `TUI.commit_stream()` finalizes it on turn end.
- REQ-1-3: A single animation thread paints the **whole screen from scratch**
  each frame using absolute cursor addressing (`\033[r;1H`) for every row.
  No scroll region, no streaming writes, no save/restore → no interleave.
- REQ-1-4: Verify: launching and quitting leaves the shell usable (alt screen
  exited, prompt on a fresh line). Covered by the PTY smoke test.

## REQ-2: thread-safe rendering
- REQ-2-1: All terminal output goes through ONE writer (the animation thread's
  `_frame()`); no other code writes to the terminal while the TUI is active.
  Content mutation (`add`/`stream`/`commit_stream`/`set_state`) only touches
  in-memory state under a `threading.RLock`.
- REQ-2-2: Each emitted frame addresses every row absolutely and pads each to
  exactly the terminal width, so consecutive frames fully overwrite with no
  leftover bytes and no autowrap.
- REQ-2-3: Verify: headless render harness confirms every row is exactly `w`
  visible columns across all states; live PTY run boots, animates, and exits
  cleanly.

## REQ-3: animated equalizer graph
- REQ-3-1: A background thread renders ~24 fps; `_frame()` repaints header +
  body log + separator + status + hints.
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
