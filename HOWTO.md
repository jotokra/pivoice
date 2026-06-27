# pivoice — voice front-end for the pi agent

Speak a prompt → transcribed on-device → sent to `pi` → reply streamed back and
spoken aloud. No cloud, no speech API key. pi uses its own configured LLM.

```
mic ─▶ ffmpeg ─▶ whisper.cpp (STT) ─▶ pi --mode rpc ─▶ text stream ─▶ say (TTS)
```

## How to run

```sh
~/pivoice/pivoice.py
```

Optional alias:

```sh
echo "alias pivoice=~/pivoice/pivoice.py" >> ~/.zshrc
```

**First run:** macOS prompts *"Terminal wants to access the microphone"* — click **Allow**.
Re-grant later via *System Settings → Privacy & Security → Microphone* if you denied it.

## Keys

| Key           | Action                                          |
|---------------|-------------------------------------------------|
| `SPACE` / `r` | Push-to-talk: press to record, press to send    |
| `a`           | Abort pi turn and stop speech                   |
| `n`           | New session                                     |
| `c`           | Clear screen                                    |
| `q` / Ctrl-C  | Quit                                            |

## Config (env vars)

| Var                | Default                      | Meaning                                   |
|--------------------|------------------------------|-------------------------------------------|
| `PIVOICE_MODEL`    | `./models/ggml-small.en.bin` | ggml model path                           |
| `PIVOICE_MIC`      | auto (prefers MacBook mic)   | avfoundation audio index, e.g. `1`        |
| `PIVOICE_SAY_VOICE`| Samantha                     | `say` voice (`say -v '?'` to list)        |
| `PIVOICE_NO_SPEAK` | —                            | `1` = mute spoken replies                 |
| `PIVOICE_PI_CWD`   | `$PWD`                       | working directory for pi                  |
| `PIVOICE_PI_ARGS`  | —                            | extra args appended to `pi --mode rpc`    |

## Setup (already done on this machine)

```sh
brew install whisper-cpp
curl -L -o ~/pivoice/models/ggml-small.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin
```

Requires macOS (Apple Silicon), `ffmpeg`, and `pi` on PATH with a provider configured.

## Troubleshooting

- **"pi exited immediately"** → check `~/pivoice/pi.log`. Usually no provider/key;
  run plain `pi` once to finish onboarding.
- **Nothing transcribed** → check `~/pivoice/last.wav`; confirm mic index with
  `ffmpeg -f avfoundation -list_devices true -i ""` and set `PIVOICE_MIC`.
- **Slow whisper** → ensure Metal loads (you'll see `loaded MTL backend` in timings).

## Files

- `pivoice.py` — the app
- `models/` — ggml weights
- `pi.log` — pi stderr (per launch)
- `last.wav` — last recording (debugging)

## Notes

- Speech processing is fully local (whisper.cpp + `say`). Audio never leaves the machine.
- pi runs with your configured model/provider; prompt cost is per your pi setup.
- Conversation context persists across turns within a session (`n` starts fresh).
