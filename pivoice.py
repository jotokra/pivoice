#!/usr/bin/env python3
"""
pivoice — voice front-end for the pi coding agent.

  speak -> transcribe (whisper.cpp) -> prompt pi (RPC) -> stream + speak reply (say)

Controls (shown on launch):
  SPACE / r   push to talk (press once to start, again to stop & send)
  a           abort the current pi turn (and stop any speech)
  n           new session (/new)
  c           clear screen
  q / Ctrl-C  quit

No cloud, no API key for speech. pi uses its own configured provider/auth.

Config via env:
  PIVOICE_MODEL     path to ggml model (default ./models/ggml-small.en.bin)
  PIVOICE_MIC       avfoundation audio index, e.g. "1" (default: auto-detect)
  PIVOICE_SAY_VOICE say voice (default: Samantha)
  PIVOICE_NO_SPEAK  "1" to disable spoken replies (text still shown)
  PIVOICE_PI_CWD    working directory for pi (default: $PWD)
  PIVOICE_PI_ARGS   extra args to `pi --mode rpc ...`
"""
from __future__ import annotations
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

# --------------------------------------------------------------------------- #
# Terminal helpers
# --------------------------------------------------------------------------- #

ANSI = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "grey": "\033[90m",
    "bg_cyan": "\033[46m", "bg_magenta": "\033[45m", "bg_grey": "\033[100m",
}

# 256-color codes for the equalizer gradient (height 0..8): dim → cyan → magenta.
BAR_COLORS = [238, 244, 51, 45, 39, 99, 135, 165, 201]
REC_COLOR = 196  # bright red for recording pulse

# Braille spinner frames for the "thinking" state.
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Block characters for equalizer bar heights 0..8.
BLOCKS = " ▁▂▃▄▅▆▇█"


def c(name: str, text: str) -> str:
    return f"{ANSI[name]}{text}{ANSI['reset']}"


def color256(code: int, text: str) -> str:
    return f"\033[38;5;{code}m{text}\033[0m"


def bold_color256(code: int, text: str) -> str:
    return f"\033[1m\033[38;5;{code}m{text}\033[0m"


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
    and then verify the output is non-trivial. avfoundation open failures (device
    busy / no permission / wrong index) make ffmpeg exit immediately, so we
    sanity-check the process right after launch.
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
        # Give avfoundation a moment to open the device; if it died here,
        # capture stderr so we can report the real reason.
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
        # SIGINT makes ffmpeg finalize and close the WAV header cleanly.
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
    import re as _re
    text = _re.sub(r"\[[A-Z_ ]+\]", " ", text)              # [BLANK_AUDIO], [MUSIC]
    text = _re.sub(r"\([a-z_ ]+\)", " ", text, flags=_re.IGNORECASE)  # (blank_audio)
    text = _re.sub(r"^\s*>+\s*", "", text)                  # leading >>
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
        # Filter the Metal-init chatter; keep only real error lines.
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
    """Speak text with `say`, sentence-by-sentce, on a worker thread."""

    SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")

    def __init__(self, voice: str | None):
        self.voice = voice
        self.queue: Queue[str | None] = Queue()
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
        """Stop current speech and clear the queue."""
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
    """Persistent `pi --mode rpc` subprocess. JSONL in/out."""

    def __init__(self, on_text, on_event, cwd: str, extra_args=None):
        self.on_text = on_text
        self.on_event = on_event
        self.cwd = cwd
        self.extra_args = list(extra_args or [])
        self.proc = None
        self._req_id = 0
        self.streaming = False
        self._stop = threading.Event()
        self.tts_buffer = ""

    def start(self) -> str:
        cmd = ["pi", "--mode", "rpc", "-n", "voice"] + self.extra_args
        self.logfile = open(HERE / "pi.log", "w")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.logfile, text=True, bufsize=1,
            cwd=self.cwd,
        )
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()
        return ""

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
            self.on_event(("response", msg))
        elif mtype == "agent_start":
            self.streaming = True
        elif mtype == "agent_end":
            self.streaming = False
            self._flush_tts()
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.on_event(("agent_end", msg))
        elif mtype == "message_update":
            ev = msg.get("assistantMessageEvent", {})
            if ev.get("type") == "text_delta":
                delta = ev.get("delta", "")
                if delta:
                    self.on_text(delta)
                    self._feed_tts(delta)
        elif mtype == "extension_error":
            err = msg.get("error", "extension error")
            self.on_text(c("red", f"\n[pi extension error] {err}\n"))
        else:
            # tool activity, compaction, retry, etc.
            if mtype in ("tool_execution_start", "compaction_start",
                         "auto_retry_start"):
                self.on_event((mtype, msg))

    # --- streaming TTS plumbing -------------------------------------------- #
    def _feed_tts(self, delta: str):
        self.tts_buffer += delta
        parts = self.SENTENCE_END_RE.split(self.tts_buffer)
        if len(parts) > 1:
            for complete in parts[:-1]:
                if complete.strip():
                    speaker.speak(complete)
            self.tts_buffer = parts[-1]

    SENTENCE_END_RE = Speaker.SENTENCE_END

    def _flush_tts(self):
        if self.tts_buffer.strip():
            speaker.speak(self.tts_buffer)
        self.tts_buffer = ""

    # --- commands ---------------------------------------------------------- #
    def _send(self, obj: dict):
        self._req_id += 1
        obj.setdefault("id", f"req-{self._req_id}")
        line = json.dumps(obj) + "\n"
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def get_state(self) -> str:
        self._send({"type": "get_state"})
        return ""

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

# Speaker is referenced by PiBridge; instantiate after definition.
speaker: Speaker  # set in main


def banner(mic_index: str, model: Path, say_voice: str | None, speak_on: bool):
    print(c("cyan", "╭─ pivoice ────────────────────────────────────────────"))
    print(c("cyan", "│") + f"  mic      : avfoundation :{mic_index}")
    print(c("cyan", "│") + f"  stt      : whisper.cpp  {model.name}")
    print(c("cyan", "│") + f"  tts      : say"
          + (f"  voice={say_voice}" if say_voice else "")
          + ("  (muted)" if not speak_on else ""))
    print(c("cyan", "│"))
    print(c("cyan", "│") + "  " + c("bold", "SPACE/r") + " talk   "
          + c("bold", "a") + " abort   " + c("bold", "n") + " new   "
          + c("bold", "c") + " clear   " + c("bold", "q") + " quit")
    print(c("cyan", "╰─────────────────────────────────────────────────────"))


def main():
    global speaker
    model = Path(os.environ.get("PIVOICE_MODEL", DEFAULT_MODEL)).expanduser()
    if not model.exists():
        print(c("red", f"[!] Whisper model not found: {model}"))
        print("    Download from https://huggingface.co/ggerganov/whisper.cpp")
        print("    e.g. curl -L -o models/ggml-small.en.bin \\")
        print("      https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin")
        sys.exit(1)

    mic_index = discover_mic()
    say_voice = os.environ.get("PIVOICE_SAY_VOICE") or None
    speak_on = os.environ.get("PIVOICE_NO_SPEAK") != "1"
    speaker = Speaker(say_voice if speak_on else None)

    cwd = os.environ.get("PIVOICE_PI_CWD") or os.getcwd()
    extra_args = os.environ.get("PIVOICE_PI_ARGS", "").split()

    def on_text(delta):
        sys.stdout.write(delta)
        sys.stdout.flush()

    print(c("grey", f"starting pi (cwd={cwd}) …"))
    bridge = PiBridge(on_text=on_text, on_event=lambda e: None,
                      cwd=cwd, extra_args=extra_args)
    try:
        bridge.start()
    except Exception as e:
        print(c("red", f"[!] failed to start pi: {e}"))
        sys.exit(1)
    time.sleep(0.6)
    if bridge.proc.poll() is not None:
        log = (HERE / "pi.log").read_text(errors="ignore").strip()
        print(c("red", f"[!] pi exited immediately."))
        if log:
            print(c("grey", log[-1000:]))
        print(c("grey", "  check `pi` works on its own, or set PIVOICE_PI_ARGS."))
        sys.exit(1)

    recorder = Recorder(mic_index)
    wav_path = HERE / "last.wav"  # always overwritten; symlink-like convenience

    def redraw():
        print()
        banner(mic_index, model, say_voice, speak_on)

    redraw()

    state = "idle"  # idle | recording
    rec_start_ts = 0.0
    REARM_GAP = 0.15        # held keys repeat ~30ms; a real re-press has a wider gap
    last_key = ""
    last_key_ts = 0.0
    with RawTerm() as term:
        try:
            while True:
                key = term.getkey().lower()
                # Suppress held-key repeats: a held key auto-repeats ~30ms,
                # but a genuine re-press has a wider gap. Per-key re-arm.
                now = time.time()
                if key == last_key and now - last_key_ts < REARM_GAP:
                    last_key_ts = now
                    continue
                last_key = key
                last_key_ts = now
                # Abort / stop talking always available
                if key in ("a",):
                    speaker.stop()
                    if bridge.streaming:
                        bridge.abort()
                        print(c("yellow", "\n[aborted]"))
                    continue
                if key in ("q",):
                    break
                if key in ("c",):
                    print("\033[2J\033[H", end="")
                    redraw()
                    continue
                if key in ("n",):
                    speaker.stop()
                    bridge.new_session()
                    print(c("green", "\n[new session]"))
                    continue

                # Push-to-talk (held-key repeats suppressed above)
                if key in (" ", "r"):
                    if state == "idle":
                        speaker.stop()
                        print()
                        sys.stdout.write(c("magenta", "● REC ") + c("grey", "(press again to send) "))
                        sys.stdout.flush()
                        try:
                            recorder.start(wav_path)
                        except Exception as e:
                            print(c("red", f"\n[recording error] {e}"))
                            print(c("grey", "  check mic index / permission (System Settings → Privacy → Microphone)"))
                            continue
                        rec_start_ts = time.time()
                        state = "recording"
                    elif state == "recording":
                        try:
                            rec_path = recorder.stop()
                        except Exception as e:
                            state = "idle"
                            print(c("red", f"\n[recording error] {e}"))
                            continue
                        state = "idle"
                        if time.time() - rec_start_ts < 0.4:
                            print(c("grey", "\n(too short — hold a little longer)"))
                            continue
                        print("\r" + " " * 50 + "\r", end="")
                        sys.stdout.write(c("grey", "transcribing… "))
                        sys.stdout.flush()
                        if not rec_path.exists() or rec_path.stat().st_size < 44:
                            print(c("grey", "(no audio captured — press and hold a moment)"))
                            continue
                        t0 = time.time()
                        try:
                            text = transcribe(model, rec_path)
                        except Exception as e:
                            print(c("red", f"\n[stt error] {e}"))
                            continue
                        dt = time.time() - t0
                        if not text:
                            print(c("grey", "(nothing heard)"))
                            continue
                        print(c("green", f"{text}") + c("grey", f"  [{dt:.1f}s]"))
                        print(c("blue", "pi » ") + c("grey", "(thinking…) "), end="", flush=True)
                        bridge.prompt(text)
        except KeyboardInterrupt:
            pass

    print(c("grey", "\nshutting down…"))
    speaker.shutdown()
    bridge.shutdown()


if __name__ == "__main__":
    main()
