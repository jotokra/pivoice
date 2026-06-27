#!/usr/bin/env python3
"""
pivoice — voice front-end for the pi coding agent.

  speak -> transcribe (whisper.cpp) -> prompt pi (RPC) -> stream + speak reply (say)

Controls (shown on launch):
  SPACE / r   tap to start recording, tap again to stop & send
  a           abort the current pi turn (and stop any speech)
  n           new session
  c           clear the conversation
  q / Ctrl-C  quit

No cloud, no API key for speech. pi uses its own configured provider/auth.

Config via env:
  PIVOICE_MODEL     path to ggml model (default ./models/ggml-small.en.bin)
  PIVOICE_MIC       avfoundation audio index, e.g. "1" (default: auto-detect)
  PIVOICE_SAY_VOICE say voice (default: Samantha)
  PIVOICE_NO_SPEAK  "1" to disable spoken replies (text still shown)
  PIVOICE_PI_CWD    working directory for pi (default: $PWD)
  PIVOICE_PI_ARGS   extra args to `pi --mode rpc ...`
  PIVOICE_SESSION   path to a pi session JSONL to RESUME (voice-mode handoff
                    from the pi TUI; appended turns go to that same file).
                    When unset, pivoice starts a fresh "voice" session.
"""
from __future__ import annotations
import atexit
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path
from queue import Queue, Empty

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = HERE / "models" / "ggml-small.en.bin"

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

# Named ANSI helpers (kept for convenience).
ANSI = {
    "reset": RESET, "bold": BOLD, "dim": DIM,
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "grey": "\033[90m",
}

# 256-color gradient for the equalizer (height 0..8): dim → cyan → magenta.
BAR_COLORS = [236, 238, 51, 45, 39, 99, 135, 165, 201]
REC_COLOR = 196
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BLOCKS = " ▁▂▃▄▅▆▇█"


def c(name: str, text: str) -> str:
    return f"{ANSI[name]}{text}{ANSI['reset']}"


def color256(code: int, text: str) -> str:
    return f"\033[38;5;{code}m{text}{RESET}"


# Per-style foreground prefixes for log lines.
STYLE_FG = {
    "user": "\033[1m\033[38;5;46m",    # bold green
    "pi":   "\033[38;5;255m",          # near-white
    "pi_pre": "\033[38;5;39m",         # cyan marker
    "thinking": "\033[3m\033[38;5;245m", # italic grey (reasoning)
    "tool":  "\033[38;5;110m",         # soft blue (tool calls)
    "tool_ok": "\033[38;5;108m",       # muted green (tool success)
    "tool_err": "\033[38;5;174m",      # muted red (tool error)
    "sys":  "\033[38;5;244m",          # grey
    "warn": "\033[38;5;214m",          # amber
    "err":  "\033[38;5;203m",          # soft red
    "ok":   "\033[38;5;42m",           # green
}


# --------------------------------------------------------------------------- #
# Raw terminal input
# --------------------------------------------------------------------------- #

class RawTerm:
    """Switch the terminal to raw mode for single-keypress input."""

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self

    def __exit__(self, *exc):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def getkey(self) -> str:
        ch = sys.stdin.read(1)
        if ch == "\x03":  # Ctrl-C
            raise KeyboardInterrupt
        if ch == "\r":
            ch = "\n"
        return ch


def wrap_plain(text: str, width: int) -> list:
    """Word-wrap plain (no-ANSI) text to `width` columns. Honors newlines."""
    width = max(10, width)
    out = []
    for para in text.split("\n"):
        if not para:
            out.append("")
            continue
        cur = ""
        for word in para.split(" "):
            if not cur:
                cur = word
            elif len(cur) + 1 + len(word) <= width:
                cur += " " + word
            else:
                out.append(cur)
                cur = word
            while len(cur) > width:           # hard-break very long tokens
                out.append(cur[:width])
                cur = cur[width:]
        out.append(cur)
    return out or [""]


def _fmt_args(args) -> str:
    """Compact one-line rendering of a tool's args dict for the TUI."""
    if not args:
        return ""
    try:
        parts = []
        for k, v in args.items():
            if isinstance(v, str):
                s = v.replace("\n", "\\n")
                if len(s) > 60:
                    s = s[:57] + "…"
                parts.append(f"{k}={s!r}" if (" " in s or not s) else f"{k}={s}")
            else:
                parts.append(f"{k}={v}")
        line = "  ".join(parts)
        return line[:120] + ("…" if len(line) > 120 else "")
    except Exception:
        return repr(args)[:120]


# --------------------------------------------------------------------------- #
# Post-onboarding hardening: refuse to run against an untuned apple-pi config.
# --------------------------------------------------------------------------- #

def applepi_settings_path() -> Path:
    """Resolve pi's global settings.json (the apple-pi agent dir)."""
    return Path(os.path.expanduser("~/.pi/agent/settings.json"))


def require_applepi_tuned():
    """Hard gate: if settings.json still carries the onboarding seed marker,
    the config is untuned scaffolding (P3 self-assessment never ran). Refuse to
    start the voice session until it has been run.

    No bypass flag, no env override — the post-onboarding self-assessment is
    non-optional from this entry point. Resolution is always reachable: run the
    self-assessment (which clears the marker), then relaunch. Fails OPEN on a
    missing/unreadable/non-apple-pi settings file (we only block on the explicit
    `_applepi_seed: true` marker, so a genuinely tuned or non-apple-pi install
    is never bricked).
    """
    path = applepi_settings_path()
    try:
        d = json.loads(path.read_text())
    except Exception:
        return  # no settings / not apple-pi / unreadable -> don't block
    if d.get("_applepi_seed") is True:
        sys.stdout.write("\n")
        sys.stdout.write(c("red", "✗ apple-pi config is still onboarding scaffolding\n"))
        sys.stdout.write(c("grey", "  (settings.json has _applepi_seed=true; the Phase 3\n"))
        sys.stdout.write(c("grey", "   self-assessment that tunes the config to your model\n"))
        sys.stdout.write(c("grey", "   never ran.)\n\n"))
        sys.stdout.write(c("yellow", "Run the self-assessment, then relaunch pivoice:\n"))
        sys.stdout.write(c("bold", "    pi -p \"/skill:self-assess\"\n"))
        sys.stdout.write(c("grey", "  (or: run `pi`, then type  /skill:self-assess )\n\n"))
        sys.stdout.write(c("grey", "This gate is intentional and cannot be skipped — the\n"))
        sys.stdout.write(c("grey", "post-onboarding tune is required for correct behavior.\n"))
        sys.exit(2)


# --------------------------------------------------------------------------- #
# TUI — alternate-screen, full-frame redraw, single writer
# --------------------------------------------------------------------------- #

class TUI:
    """A self-contained futuristic TUI.

    Architecture (race-free): everything funnels into an in-memory log; a single
    animation thread paints the WHOLE screen from scratch each frame using
    absolute cursor addressing. There are no streaming writes to the terminal
    and no scroll regions, so conversation text and the status panel can never
    garble each other.
    """

    HEADER_ROWS = 4               # top border, title, meta, bottom border
    PANEL_ROWS = 3                 # separator + status + hints
    FPS = 24
    MAX_ENTRIES = 400

    STATE_META = {
        # state: (label, icon, accent_color, amp, speed, jitter, floor, red)
        "idle":         ("IDLE",  "◇", 51,  0.10, 1.2, 0, 0, False),
        "recording":    ("REC",   "●", 196, 0.85, 6.0, 2, 2, True),
        "transcribing": ("STT",   "◐", 214, 0.35, 3.5, 1, 1, False),
        "thinking":     ("THINK", "◆", 99,  0.25, 2.0, 0, 1, False),
        "speaking":     ("SPEAK", "◆", 201, 0.95, 5.5, 2, 1, False),
    }

    def __init__(self, info: dict):
        self.info = info
        self.state = "idle"
        self.detail = ""
        self.entries: list = []     # (style, plain_text)
        self._stream = ""           # in-progress assistant text
        self._stream_style = "pi"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._t0 = time.time()
        self._spin = 0
        self._cols, self._rows = 80, 24
        self._alt = False
        self._out = sys.stdout      # the real terminal stream; never reassigned

    # ---- lifecycle -------------------------------------------------------- #
    def setup(self):
        self._size()
        # Enter alternate screen, hide cursor, clear.
        self._emit("\033[?1049h\033[?25l\033[H\033[2J")
        self._alt = True
        self.start()
        atexit.register(self.teardown)

    def teardown(self):
        self.stop()
        if self._alt:
            self._emit("\033[?25h\033[?1049l")
            self._alt = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t, self._thread = self._thread, None
        if t and t.is_alive():
            t.join(timeout=1.0)

    # ---- content API (thread-safe, no terminal writes) -------------------- #
    def add(self, text: str, style: str = "sys"):
        text = text.rstrip("\n")
        if not text:
            return
        with self._lock:
            self.entries.append((style, text))
            if len(self.entries) > self.MAX_ENTRIES:
                del self.entries[: len(self.entries) - self.MAX_ENTRIES]
            self._stream = ""

    def stream(self, delta: str, style: str = "pi"):
        # Only one in-flight stream at a time; commit before switching styles.
        with self._lock:
            if self._stream and self._stream_style != style:
                self._commit_locked()
            self._stream_style = style
            self._stream += delta

    def _commit_locked(self):
        if self._stream.strip():
            self.entries.append((self._stream_style, self._stream.rstrip()))
            if len(self.entries) > self.MAX_ENTRIES:
                del self.entries[: len(self.entries) - self.MAX_ENTRIES]
        self._stream = ""

    def commit_stream(self):
        with self._lock:
            self._commit_locked()

    def tool_line(self, text: str, style: str = "tool"):
        text = text.rstrip("\n")
        if not text:
            return
        with self._lock:
            self._commit_locked()
            self.entries.append((style, text))
            if len(self.entries) > self.MAX_ENTRIES:
                del self.entries[: len(self.entries) - self.MAX_ENTRIES]

    def clear_log(self):
        with self._lock:
            self.entries = []
            self._stream = ""

    def set_state(self, state: str, detail: str = ""):
        with self._lock:
            self.state = state
            if detail is not None:
                self.detail = detail

    # ---- rendering -------------------------------------------------------- #
    def _size(self):
        try:
            sz = shutil.get_terminal_size((80, 24))
            self._cols = max(50, sz.columns)
            self._rows = max(18, sz.lines)
        except Exception:
            pass

    def _emit(self, s: str):
        self._out.write(s)
        self._out.flush()

    def _loop(self):
        period = 1.0 / self.FPS
        while not self._stop.is_set():
            try:
                self._frame()
            except Exception:
                pass
            time.sleep(period)

    def _frame(self):
        self._size()
        w, h = self._cols, self._rows
        self._spin = (self._spin + 1) % len(SPINNER)
        body_h = max(1, h - self.HEADER_ROWS - self.PANEL_ROWS)
        with self._lock:
            entries = list(self.entries)
            stream = self._stream
            state = self.state
            detail = self.detail
            info = self.info

        lines = []  # (style, plain)
        for style, text in entries:
            for ln in wrap_plain(text, w):
                lines.append((style, ln))
        if stream:
            for ln in wrap_plain(stream, w):
                lines.append(("pi", ln))
        visible = lines[-body_h:]

        frame = []
        # Header (3 rows)
        frame.append(self._header(w, info))
        # Body
        for i in range(body_h):
            row = self._pad(w, "")
            if i < len(visible):
                style, ln = visible[i]
                row = self._pad(w, STYLE_FG.get(style, "") + ln)
            frame.append(f"\033[{self.HEADER_ROWS + 1 + i};1H" + row)
        # Panel
        frame.append(f"\033[{h - 2};1H" + self._separator(w))
        frame.append(f"\033[{h - 1};1H" + self._status_row(w, state, detail))
        frame.append(f"\033[{h};1H" + self._hint_row(w))
        self._emit("".join(frame))

    @staticmethod
    def _pad(w: int, content: str) -> str:
        """Return `content` truncated/padded to exactly `w` visible columns.

        ANSI escape bytes are zero-width; we measure visible length by
        stripping them. Trailing pad uses spaces (invisible under any color).
        """
        visible = re.sub(r"\033\[[0-9;]*m", "", content)
        if len(visible) > w:
            # Truncate the visible portion; simplest: cut the whole string.
            content = content[: max(0, len(content) - (len(visible) - w))]
            visible = visible[:w]
        pad = " " * (w - len(visible))
        return content + pad + RESET

    def _brow(self, inner_text: str, w: int, border: int = 51,
              textcolor: int = 255) -> str:
        inner = max(10, w - 2)
        inner_text = inner_text[:inner].ljust(inner)
        return (color256(border, "│") + color256(textcolor, inner_text)
                + color256(border, "│"))

    def _header(self, w: int, info: dict) -> str:
        border = 51
        top = color256(border, "╭" + "─" * max(10, w - 2) + "╮")
        bot = color256(border, "╰" + "─" * max(10, w - 2) + "╯")
        title = self._brow("  " + "pivoice" + "  ·  " + "voice ⇄ pi", w,
                           border=border, textcolor=255)
        # colorize keywords inside the title row by post-processing is messy;
        # keep title uniform white for a clean look.
        meta_text = f"  mic {info['mic']}   stt {info['stt']}   tts {info['tts']}"
        meta = self._brow(meta_text, w, border=border, textcolor=244)
        return (f"\033[1;1H{self._pad(w, top)}"
                f"\033[2;1H{self._pad(w, title)}"
                f"\033[3;1H{self._pad(w, meta)}"
                f"\033[4;1H{self._pad(w, bot)}")

    def _separator(self, w: int) -> str:
        return self._pad(w, color256(236, "─" * w))

    def _status_row(self, w: int, state: str, detail: str) -> str:
        label, icon, accent, *_ = self.STATE_META.get(state, self.STATE_META["idle"])
        spin = SPINNER[self._spin]
        left = f"{color256(accent, icon)} {color256(accent, label)}"
        if state == "thinking":
            left += " " + color256(accent, spin)
        detail_s = color256(244, detail) if detail else ""
        left_vis = len(icon) + 1 + len(label) + (2 if state == "thinking" else 0)
        right_vis = len(detail) + (2 if detail else 0)
        bar_w = max(8, w - left_vis - right_vis - 3)
        bars = self._eq(state, bar_w)
        content = f"{left}  {bars}" + (f"  {detail_s}" if detail else "")
        return self._pad(w, content)

    def _hint_row(self, w: int) -> str:
        hint = "SPACE talk · a abort · n new · c clear · q quit"
        return self._pad(w, color256(238, hint))

    def _eq(self, state: str, width: int) -> str:
        _, _, accent, amp, speed, jitter, floor, red = self.STATE_META.get(
            state, self.STATE_META["idle"])
        t = time.time() - self._t0
        parts = []
        for i in range(width):
            phase = i * 0.42
            if state == "idle":
                h = 1 if (i + int(t * 2)) % 9 == 0 else 0
            elif state == "recording":
                pulse = 0.5 + 0.5 * math.sin(t * speed + phase)
                h = floor + int(round(pulse * (8 - floor) * amp))
            else:
                wave = 0.5 + 0.5 * math.sin(t * speed + phase)
                v = wave * (0.65 + 0.35 * math.sin(t * speed * 1.7 + i * 0.6))
                if jitter:
                    v += random.uniform(-0.12, 0.12) * jitter
                h = floor + int(round(v * (8 - floor) * amp))
            h = max(0, min(8, h))
            code = REC_COLOR if red else BAR_COLORS[h]
            parts.append(color256(code, BLOCKS[h]))
        return "".join(parts)


# --------------------------------------------------------------------------- #
# Microphone discovery + recording
# --------------------------------------------------------------------------- #

def discover_mic(preferred_hint: str = "") -> str:
    """Return the avfoundation audio index as a string (e.g. '1')."""
    forced = os.environ.get("PIVOICE_MIC")
    if forced is not None and forced != "":
        return forced
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        ).stderr
    except Exception:
        out = ""
    audio_section = False
    devices = []
    for line in out.splitlines():
        if "AVFoundation audio devices" in line:
            audio_section = True
            continue
        if audio_section and "AVFoundation video devices" in line:
            audio_section = False
            continue
        if audio_section:
            m = re.search(r"\[(\d+)]\s+(.*)", line)
            if m:
                devices.append((m.group(1), m.group(2).strip()))
    if not devices:
        return "0"
    for idx, name in devices:
        low = name.lower()
        if preferred_hint and preferred_hint.lower() in low:
            return idx
        if "macbook" in low:
            return idx
    return devices[0][0]


class RecorderError(RuntimeError):
    pass


class Recorder:
    """Record mono 16kHz PCM via ffmpeg's avfoundation input.

    ffmpeg writes the WAV header on graceful close, so we send SIGINT to stop
    and then verify the output is non-trivial. avfoundation open failures
    (device busy / no permission / wrong index) make ffmpeg exit immediately,
    so we sanity-check the process right after launch.
    """

    def __init__(self, mic_index: str):
        self.mic = mic_index
        self.proc = None
        self.path = None
        self.stderr = ""

    def start(self, path: Path):
        self.path = path
        if path.exists():
            path.unlink()
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "avfoundation", "-i", f":{self.mic}",
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
             str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        time.sleep(0.25)
        if self.proc.poll() is not None:
            try:
                self.stderr = self.proc.stderr.read().decode("utf-8", "ignore").strip()
            except Exception:
                self.stderr = ""
            self.proc = None
            raise RecorderError(self.stderr or "ffmpeg exited immediately")

    def stop(self) -> Path:
        proc = self.proc
        self.proc = None
        if proc is None:
            raise RecorderError(f"no active recording; last: {self.stderr or 'n/a'}")
        try:
            proc.send_signal(signal.SIGINT)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            self.stderr = proc.stderr.read().decode("utf-8", "ignore").strip()
        except Exception:
            pass
        if self.path is None or not self.path.exists():
            raise RecorderError(self.stderr or "no wav produced")
        if self.path.stat().st_size < 44:
            raise RecorderError(self.stderr or "wav too small (recording too brief?)")
        return self.path


# --------------------------------------------------------------------------- #
# Speech-to-text (whisper.cpp)
# --------------------------------------------------------------------------- #

def _clean_transcript(text: str) -> str:
    """Strip whisper.cpp artifacts and special tokens."""
    text = re.sub(r"\[[A-Z_ ]+\]", " ", text)                        # [BLANK_AUDIO]
    text = re.sub(r"\([a-z_ ]+\)", " ", text, flags=re.IGNORECASE)   # (blank_audio)
    text = re.sub(r"^\s*>+\s*", "", text)                            # leading >>
    return " ".join(text.split()).strip()


def transcribe(model: Path, wav: Path) -> str:
    out_base = wav.with_suffix("")
    txt = Path(str(out_base) + ".txt")
    if txt.exists():
        txt.unlink()
    proc = subprocess.run(
        ["whisper-cli", "-m", str(model), "-f", str(wav),
         "-l", "en", "-nt", "-otxt", "-of", str(out_base)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raw = (proc.stderr or proc.stdout or "")
        errors = [ln.strip() for ln in raw.splitlines()
                  if ln.strip().lower().startswith(("error", "failed", "fatal"))]
        raise RuntimeError("; ".join(errors) or "whisper-cli failed (nonzero exit)")
    if txt.exists():
        text = txt.read_text(encoding="utf-8", errors="ignore").strip()
        if text:
            return _clean_transcript(text)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return _clean_transcript("\n".join(lines))


# --------------------------------------------------------------------------- #
# Text-to-speech (say), sentence-streamed
# --------------------------------------------------------------------------- #

class Speaker:
    """Speak text with `say`, sentence-by-sentence, on a worker thread."""

    SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")

    def __init__(self, voice):
        self.voice = voice
        self.queue: Queue = Queue()
        self.proc = None
        self._lock = threading.Lock()
        self._running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _say(self, text: str):
        if not text.strip():
            return
        args = ["say"]
        if self.voice:
            args += ["-v", self.voice]
        args += ["-r", "200", text]
        with self._lock:
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        try:
            self.proc.wait()
        finally:
            with self._lock:
                self.proc = None

    def _loop(self):
        while self._running:
            try:
                item = self.queue.get(timeout=0.2)
            except Empty:
                continue
            if item is None:
                break
            self._say(item)

    def speak(self, text: str):
        for chunk in self.SENTENCE_END.split(text):
            if chunk.strip():
                self.queue.put(chunk.strip())

    def stop(self):
        while True:
            try:
                self.queue.get_nowait()
            except Empty:
                break
        with self._lock:
            if self.proc:
                try:
                    self.proc.terminate()
                except Exception:
                    pass

    def shutdown(self):
        self._running = False
        self.stop()
        self.queue.put(None)


# --------------------------------------------------------------------------- #
# pi agent bridge (RPC over stdin/stdout)
# --------------------------------------------------------------------------- #

class PiBridge:
    """Persistent `pi --mode rpc` subprocess. JSONL in/out. Pure-callback:
    never writes to the terminal itself — routes all output through callbacks.
    """

    SENTENCE_END_RE = Speaker.SENTENCE_END

    def __init__(self, cwd: str, extra_args=None, on_text=None,
                 on_thinking=None, on_tool=None, on_state=None,
                 on_turn_end=None, on_event=None):
        self.cwd = cwd
        self.extra_args = list(extra_args or [])
        self.on_text = on_text
        self.on_thinking = on_thinking
        self.on_tool = on_tool
        self.on_state = on_state
        self.on_turn_end = on_turn_end
        self.on_event = on_event
        self.proc = None
        self._req_id = 0
        self.streaming = False
        self._stop = threading.Event()
        self.tts_buffer = ""

    def start(self):
        # Resume an existing session if PIVOICE_SESSION is set (voice-mode handoff
        # from the pi TUI: same JSONL file, appended turns). Otherwise start fresh.
        session = os.environ.get("PIVOICE_SESSION")
        cmd = ["pi", "--mode", "rpc"]
        if session:
            cmd += ["--session", session]
        else:
            cmd += ["-n", "voice"]
        cmd += self.extra_args
        self.logfile = open(HERE / "pi.log", "w")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.logfile, text=True, bufsize=1, cwd=self.cwd,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "response":
            if self.on_event:
                self.on_event(("response", msg))
        elif mtype == "agent_start":
            self.streaming = True
            if self.on_state:
                self.on_state("thinking")
        elif mtype == "agent_end":
            self.streaming = False
            self._flush_tts()
            if self.on_turn_end:
                self.on_turn_end()
            if self.on_state:
                self.on_state("idle")
            if self.on_event:
                self.on_event(("agent_end", msg))
        elif mtype == "message_update":
            ev = msg.get("assistantMessageEvent", {})
            etype = ev.get("type")
            if etype == "text_delta":
                delta = ev.get("delta", "")
                if delta:
                    if self.on_text:
                        self.on_text(delta)
                    self._feed_tts(delta)
                    if self.on_state:
                        self.on_state("speaking")
            elif etype == "thinking_delta":
                delta = ev.get("delta", "")
                if delta and self.on_thinking:
                    self.on_thinking(delta)
        elif mtype == "tool_execution_start":
            name = msg.get("toolName", "tool")
            args = msg.get("args", {})
            if self.on_tool:
                self.on_tool("start", name, args, False)
        elif mtype == "tool_execution_end":
            name = msg.get("toolName", "tool")
            is_error = bool(msg.get("isError"))
            if self.on_tool:
                self.on_tool("end", name, {}, is_error)
        elif mtype == "extension_error":
            err = msg.get("error", "extension error")
            if self.on_event:
                self.on_event(("extension_error", err))

    def _feed_tts(self, delta: str):
        self.tts_buffer += delta
        parts = self.SENTENCE_END_RE.split(self.tts_buffer)
        if len(parts) > 1:
            for complete in parts[:-1]:
                if complete.strip():
                    speaker.speak(complete)
            self.tts_buffer = parts[-1]

    def _flush_tts(self):
        if self.tts_buffer.strip():
            speaker.speak(self.tts_buffer)
        self.tts_buffer = ""

    def _send(self, obj: dict):
        self._req_id += 1
        obj.setdefault("id", f"req-{self._req_id}")
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def get_state(self):
        self._send({"type": "get_state"})

    def prompt(self, message: str):
        body = {"type": "prompt", "message": message}
        if self.streaming:
            body["streamingBehavior"] = "steer"
        self.tts_buffer = ""
        self._send(body)

    def abort(self):
        if self.streaming:
            self._send({"type": "abort"})

    def new_session(self):
        self._send({"type": "new_session"})

    def shutdown(self):
        self._stop.set()
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

speaker: Speaker   # referenced by PiBridge._feed_tts; set in main()


def main():
    global speaker

    require_applepi_tuned()

    model = Path(os.environ.get("PIVOICE_MODEL", DEFAULT_MODEL)).expanduser()
    if not model.exists():
        sys.stdout.write(c("red", f"[!] Whisper model not found: {model}\n"))
        sys.stdout.write("    Download from https://huggingface.co/ggerganov/whisper.cpp\n")
        sys.exit(1)

    mic_index = discover_mic()
    say_voice = os.environ.get("PIVOICE_SAY_VOICE") or None
    speak_on = os.environ.get("PIVOICE_NO_SPEAK") != "1"
    speaker = Speaker(say_voice if speak_on else None)

    cwd = os.environ.get("PIVOICE_PI_CWD") or os.getcwd()
    extra_args = [a for a in os.environ.get("PIVOICE_PI_ARGS", "").split() if a]

    tts_label = ("say" + (f"/{say_voice}" if say_voice else "")
                 + (" (muted)" if not speak_on else ""))
    info = {"mic": f":{mic_index}", "stt": model.name, "tts": tts_label, "cwd": cwd}

    tui = TUI(info)

    def on_text(delta):
        tui.stream(delta, "pi")

    def on_thinking(delta):
        tui.stream(delta, "thinking")

    def on_tool(phase, name, args, is_error):
        if phase == "start":
            tui.tool_line(f"↳ {name}  {_fmt_args(args)}", "tool")
        else:
            tui.tool_line(("  ↳ ✓" if not is_error else "  ↳ ✗ failed"),
                          "tool_ok" if not is_error else "tool_err")

    def on_state(state):
        detail = {"thinking": "working — reasoning or running tools…",
                  "speaking": "streaming reply…",
                  "recording": "listening — tap SPACE to send",
                  "transcribing": "whisper.cpp decoding…"}.get(state, "")
        tui.set_state(state, detail)

    def on_turn_end():
        tui.commit_stream()

    def on_event(ev):
        kind, payload = ev
        if kind == "extension_error":
            tui.add(f"[pi extension] {payload}", "warn")

    # Boot pi before entering the alt screen, so boot errors print normally.
    sys.stdout.write(c("grey", f"starting pi (cwd={cwd}) …\n"))
    sys.stdout.flush()
    bridge = PiBridge(cwd=cwd, extra_args=extra_args, on_text=on_text,
                      on_thinking=on_thinking, on_tool=on_tool,
                      on_state=on_state, on_turn_end=on_turn_end,
                      on_event=on_event)
    try:
        bridge.start()
    except Exception as e:
        sys.stdout.write(c("red", f"[!] failed to start pi: {e}\n"))
        sys.exit(1)
    time.sleep(0.6)
    if bridge.proc.poll() is not None:
        log = (HERE / "pi.log").read_text(errors="ignore").strip()
        sys.stdout.write(c("red", "[!] pi exited immediately.\n"))
        if log:
            sys.stdout.write(c("grey", log[-800:] + "\n"))
        sys.exit(1)

    recorder = Recorder(mic_index)
    wav_path = HERE / "last.wav"

    # ---- enter the TUI ---------------------------------------------------- #
    with RawTerm():
        tui.setup()
        tui.add("ready · tap SPACE to talk · talk to the real pi agent (full tools + skills)", "ok")

        # ensure teardown even on signals
        def _on_sig(signum, frame):
            raise KeyboardInterrupt
        for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            signal.signal(s, _on_sig)

        state = "idle"
        rec_start_ts = 0.0
        REARM_GAP = 0.15
        last_key, last_key_ts = "", 0.0
        try:
            while True:
                key = term_getkey().lower()
                now = time.time()
                if key == last_key and now - last_key_ts < REARM_GAP:
                    last_key_ts = now
                    continue
                last_key, last_key_ts = key, now

                if key == "a":
                    speaker.stop()
                    if bridge.streaming:
                        bridge.abort()
                        tui.add("aborted", "warn")
                    continue
                if key == "q":
                    break
                if key == "c":
                    tui.clear_log()
                    tui.add("cleared", "sys")
                    continue
                if key == "n":
                    speaker.stop()
                    bridge.new_session()
                    tui.add("new session", "ok")
                    continue

                if key in (" ", "r"):
                    if state == "idle":
                        speaker.stop()
                        tui.set_state("recording", "listening — tap SPACE to send")
                        try:
                            recorder.start(wav_path)
                        except Exception as e:
                            tui.add(f"recording error: {e}", "err")
                            tui.add("check mic index / System Settings → Privacy → Microphone",
                                    "sys")
                            tui.set_state("idle")
                            continue
                        rec_start_ts = time.time()
                        state = "recording"
                    elif state == "recording":
                        try:
                            rec_path = recorder.stop()
                        except Exception as e:
                            state = "idle"
                            tui.set_state("idle")
                            tui.add(f"recording error: {e}", "err")
                            continue
                        state = "idle"
                        if time.time() - rec_start_ts < 0.4:
                            tui.add("too short — hold a little longer", "sys")
                            continue
                        tui.set_state("transcribing", "whisper.cpp decoding…")
                        if not rec_path.exists() or rec_path.stat().st_size < 44:
                            tui.set_state("idle")
                            tui.add("no audio captured — try again", "sys")
                            continue
                        t0 = time.time()
                        try:
                            text = transcribe(model, rec_path)
                        except Exception as e:
                            tui.set_state("idle")
                            tui.add(f"stt error: {e}", "err")
                            continue
                        dt = time.time() - t0
                        if not text:
                            tui.set_state("idle")
                            tui.add("(nothing heard)", "sys")
                            continue
                        tui.add(f"▸ {text}   {dt:.1f}s", "user")
                        tui.set_state("thinking", "waiting on model…")
                        bridge.prompt(text)
        except KeyboardInterrupt:
            pass
        finally:
            tui.teardown()

    sys.stdout.write(c("grey", "shutting down…\n"))
    if os.environ.get("PIVOICE_SESSION"):
        sys.stdout.write(c("cyan", "voice mode ended — resume your session in the pi TUI:\n"))
        sys.stdout.write(c("bold", f"  pi -c\n"))
        sys.stdout.write(c("grey", "  (voice turns are appended to the same session file.)\n"))
    speaker.shutdown()
    bridge.shutdown()


def term_getkey() -> str:
    """Read one key from the (already-raw) terminal."""
    ch = sys.stdin.read(1)
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch == "\r":
        ch = "\n"
    return ch


if __name__ == "__main__":
    main()
