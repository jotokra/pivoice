# pivoice

Voice front-end for the [pi coding agent](https://github.com/earendil-works/pi-coding-agent).
Speak a prompt → transcribed on-device → sent to `pi` → the reply is streamed
back and spoken aloud. No cloud, no speech API key; pi uses its own configured LLM.

```
mic ─▶ ffmpeg ─▶ whisper.cpp (STT) ─▶ pi --mode rpc ─▶ text stream ─▶ say (TTS)
```

## Quick start

```sh
brew install whisper-cpp
curl -L -o models/ggml-small.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin
./pivoice.py
```

Requires: macOS (Apple Silicon), `ffmpeg`, and [`pi`](https://github.com/earendil-works/pi-coding-agent)
on PATH with a provider/model configured.

**First run:** macOS prompts *"Terminal wants to access the microphone"* — click **Allow**.

## How to use

It's **tap-to-talk** (not hold):

1. Tap `SPACE` (or `r`) once → recording starts (`● REC`).
2. Tap `SPACE` again → recording stops, transcribes, and sends to pi.
3. The reply streams to the terminal and is spoken aloud, sentence by sentence.

| Key           | Action                                          |
|---------------|-------------------------------------------------|
| `SPACE` / `r` | Push-to-talk: tap to record, tap again to send  |
| `a`           | Abort pi turn and stop speech                   |
| `n`           | New session                                     |
| `c`           | Clear screen                                    |
| `q` / Ctrl-C  | Quit                                            |

> Held-key repeats are suppressed automatically (terminals auto-repeat held
> keys); just use two distinct taps.

## Configuration (env vars)

| Var                | Default                      | Meaning                                   |
|--------------------|------------------------------|-------------------------------------------|
| `PIVOICE_MODEL`    | `./models/ggml-small.en.bin` | ggml model path                           |
| `PIVOICE_MIC`      | auto (prefers MacBook mic)   | avfoundation audio index, e.g. `1`        |
| `PIVOICE_SAY_VOICE`| Samantha                     | `say` voice (`say -v '?'` to list)        |
| `PIVOICE_NO_SPEAK` | —                            | `1` = mute spoken replies                 |
| `PIVOICE_PI_CWD`   | `$PWD`                       | working directory for pi                  |
| `PIVOICE_PI_ARGS`  | —                            | extra args appended to `pi --mode rpc`    |

### Examples

```sh
# A bigger / multilingual model
curl -L -o models/ggml-medium.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin
PIVOICE_MODEL=models/ggml-medium.bin ./pivoice.py

# Run pi against a specific project, muted
PIVOICE_PI_CWD=~/code/myapp PIVOICE_NO_SPEAK=1 ./pivoice.py

# Nicer voice
PIVOICE_SAY_VOICE=Ava ./pivoice.py
```

## Troubleshooting

- **Nothing transcribed / `(nothing heard)`** → check `last.wav` has audio;
  confirm the mic index with `ffmpeg -f avfoundation -list_devices true -i ""`
  and set `PIVOICE_MIC`.
- **`[recording error]`** → avfoundation couldn't open the mic. Check
  *System Settings → Privacy & Security → Microphone*, and confirm no other app
  holds the device.
- **"pi exited immediately"** → check `pi.log`. Usually no provider/key; run
  plain `pi` once to finish onboarding.
- **Slow whisper** → ensure Metal loads (`load_backend: loaded MTL backend` in
  the timings). `small.en` transcribes ~2s audio in ~0.3s on Apple Silicon.

## How it works

- **STT** — [`whisper.cpp`](https://github.com/ggml-org/whisper.cpp), on-device,
  Apple-Silicon GPU (Metal) accelerated. Audio never leaves the machine.
- **TTS** — macOS built-in `say`, streamed sentence-by-sentence as pi replies.
- **Agent bridge** — spawns `pi --mode rpc` as a persistent subprocess and speaks
  the [JSON-RPC protocol](https://github.com/earendil-works/pi-coding-agent/blob/main/docs/rpc.md).
  Conversation context persists across turns within a session (`n` starts fresh).

## Files

- `pivoice.py` — the app (single file, stdlib only).
- `models/` — ggml weights (gitignored — download per quick start).
- `pi.log` — pi's stderr (written on each launch).
- `last.wav` / `last.txt` — most recent recording + transcript (debugging).

See [`HOWTO.md`](HOWTO.md) for a condensed cheat sheet.

## License

MIT — see [LICENSE](LICENSE).
