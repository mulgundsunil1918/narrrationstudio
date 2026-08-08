# Narration Studio

Turns any SRT subtitle file into timestamp-locked narration, entirely on your
own machine. No cloud, no account, no telemetry. macOS and Windows.

```
SRT → natural narration grouping → local TTS → timestamp-perfect audio → WAV
```

---

## The one idea that matters

**A caption boundary is not a speech boundary.**

Whisper wraps subtitle lines at a fixed width, so a sentence routinely spans two
or three captions:

```
Caption 30:  "…The Guide section brings together practical clinical references across"
Caption 31:  "pediatrics and neonatology. These include emergency resources, neonatal"
```

Generating one audio clip per caption makes the narrator stop mid-sentence and
pad the rest of the window with silence. That is what produced the six-second
hole at 2:43 in the original proof-of-concept.

This app keeps two separate timelines:

| | Owns | Never moves |
|---|---|---|
| **Caption timeline** | the SRT: numbers, start, end, text | ✅ master clock |
| **Narration timeline** | groups of consecutive captions spoken as one utterance | derived |

Captions still change on their own timestamps. The voice does not stop when they
do.

---

## Install

Download the latest build from
[Releases](https://github.com/mulgundsunil1918/narrrationstudio/releases):

* **macOS** — unzip, drag *Narration Studio.app* to Applications.
  Right-click ▸ Open the first time (the app is not notarised).
* **Windows** — unzip anywhere, run `NarrationStudio.bat`.

The first launch downloads the speech engine (~2 GB) and takes a few minutes.
Every launch after that is immediate.

You also need **[FFmpeg](https://ffmpeg.org/download.html)** — it fits the
speech to your subtitle timings:

```bash
brew install ffmpeg          # macOS
winget install Gyan.FFmpeg   # Windows
```

## Run from source

```bash
./setup.sh && ./run.sh
```

Or use the command line with any SRT:

```bash
./.venv/bin/python generate_natural_tts.py path/to/your.srt
```

`Tutorial_01.srt` → `Tutorial_01_Narration.wav`, written beside the input.

## Building the packages

```bash
./packaging/build_macos.sh --install      # macOS
pwsh ./packaging/build_windows.ps1        # Windows
```

Windows cannot be built from macOS. Pushing a `v*` tag makes GitHub Actions
build both on their own runners and attach them to a release:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

---

## The 60-second cap is not a project limit

`MAX_NARRATION_GROUP_DURATION = 60s` bounds **one TTS generation**, nothing else.

- A 349-second SRT produces ~349 seconds of audio.
- A 20-minute SRT produces ~20 minutes of audio.
- The app creates as many groups as the project needs.

The cap exists so a single failure, edit or cache miss costs one group rather
than the whole video. It is a ceiling to aim under, not a target to fill: a
group may run slightly past it to finish on a better linguistic boundary, and
will end early when a real sentence break arrives first.

To change it:

```bash
--max-group 90          # seconds, per group
```

---

## Common commands

```bash
# See how the narration will be grouped, without generating anything
./.venv/bin/python generate_natural_tts.py input.srt --plan-only

# Quick test renders (these never change the project's own duration)
./.venv/bin/python generate_natural_tts.py input.srt --test 60
./.venv/bin/python generate_natural_tts.py input.srt --test 120
./.venv/bin/python generate_natural_tts.py input.srt --test 300

# The whole project
./.venv/bin/python generate_natural_tts.py input.srt --test full

# A different voice
./.venv/bin/python generate_natural_tts.py --list-voices
./.venv/bin/python generate_natural_tts.py input.srt --voice am_michael

# Speak more slowly at the engine, which sounds better than stretching later
./.venv/bin/python generate_natural_tts.py input.srt --speed 0.9

# Also write an MP3
./.venv/bin/python generate_natural_tts.py input.srt --mp3
```

---

## How timing works

For every narration group:

```
group_start = first caption's start        ← from the SRT
group_end   = last caption's end           ← from the SRT
```

The group's audio is written at `group_start`, always. It is **never** placed
after the previous group's audio ended — that is what makes cumulative drift
impossible. If group 3 renders short, group 4 still begins exactly on time.

Then:

| Situation | What happens |
|---|---|
| Speech longer than the window | Pitch-preserving compression (ffmpeg `atempo`). Never truncated. |
| Speech shorter than the window | **Stretched to fill.** Silence is a last resort, not the default. |
| Still short after stretching to the floor | Padded, and reported as *unnatural internal silence*. |
| Real gap between subtitles in the SRT | Preserved — it is genuine source silence. |

Compression safety, flagged in the output:

| Factor | Verdict |
|---|---|
| ≤ 1.08× | safe |
| 1.08–1.15× | slightly fast |
| 1.15–1.30× | noticeably fast |
| > 1.30× | needs confirmation |

A 30–80 ms equal-power crossfade smooths group joins. It never overlaps words
and never changes the timeline length. Disable with `--no-crossfade`.

---

## Text handling

Two separate layers, deliberately:

- **Text cleanup** rewrites the *captions*. Only rules you configure are applied
  — there is no spell-checker and no medical dictionary, because silently
  "correcting" a drug name would be far worse than leaving a brand name
  misspelt. Preview before applying.
- **Pronunciation** rewrites only what the *engine* receives. Captions are
  untouched. Use it for initialisms (`TPN`, `IAP`, `NNF`, `CME`) and for the
  brand. Disable with `--no-pronunciation`.

---

## Caching

A group's audio is keyed by engine, model, voice, language, text and speed.
Editing one caption regenerates that group only. The key deliberately excludes
the group's *window*, so audio can be refitted to new timings without asking the
engine again.

---

## Layout

```
app/
  core/        timecode, models, document, undo, validation   (no Qt, no audio)
  srt/         parser, writer, text cleanup
  narration/   grouping heuristics, groups, validation reports
  tts/         base.py (interface), kokoro_engine.py, registry, pronunciation
  audio/       timing (fitting maths), assemble (placement, crossfade, export)
  cache/       content-addressed audio store
  ui/          theme, screens, widgets, workers, state
generate_natural_tts.py   command line
tests/                    352 tests
```

Adding an engine means one subclass of `TTSEngine` and one `register()` call.
Nothing above `app/tts/` knows what Kokoro is.

---

## Testing

```bash
./.venv/bin/python -m pytest
```

Covers SRT parsing, timecode conversion, text cleanup, narration grouping,
atempo chaining, fitting, silence policy, timeline placement, drift, crossfade,
normalisation, and project length at 1/5/20/60-minute scales.

---

## Environment

Pinned to a proven combination: Python 3.12, kokoro 0.9.4, torch 2.2.2,
numpy 1.26.4, transformers 4.49.0, soundfile, PySide6 6.11.1, plus FFmpeg as a
system install. Do not bump these casually — `transformers` 5.x in particular
breaks the API kokoro 0.9.4 expects.

Developed on macOS Intel x86_64; the Windows package is built by CI.

## Licence

MIT. See [LICENSE](LICENSE).
