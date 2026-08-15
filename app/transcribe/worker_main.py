"""The transcription child process.

Run as ``python -m app.transcribe.worker_main <audio.wav> --model small`` and it
writes one JSON object per line to stdout: the language it detected, each
utterance as it is recognised, then a final result.

This exists as a separate process for a specific reason. Kokoro brings PyTorch,
faster-whisper brings CTranslate2, and each ships its own copy of the Intel
OpenMP runtime. With both in one process, loading a Whisper model does not raise
— it calls ``abort()`` and takes the whole application down with it, no
exception, no error dialog, nothing. Isolating it also means cancelling is a
real kill rather than a polite request, and a crash inside the model costs the
user a transcription instead of their session.

So the first thing this module does, before importing anything that could pull
PyTorch in, is make PyTorch unimportable.
"""

from __future__ import annotations

import sys

# Must happen before faster_whisper. ctranslate2 imports its optional
# transformers converter, which imports torch when torch is installed, and that
# is what drags the second OpenMP runtime in. Setting the entry to None makes
# ``import torch`` raise ImportError, which is exactly what that import site
# already handles — the converters are for preparing models, and nothing here
# uses them.
for _blocked in ("torch", "transformers"):
    sys.modules.setdefault(_blocked, None)  # type: ignore[assignment]

import argparse  # noqa: E402
import json  # noqa: E402
import traceback  # noqa: E402


def emit(payload: dict) -> None:
    """One JSON object, one line, flushed — the parent reads this live."""
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe an audio file.")
    parser.add_argument("audio", help="Path to a 16 kHz mono WAV file")
    parser.add_argument("--model", default="small")
    parser.add_argument("--language", default="")
    options = parser.parse_args(argv)

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        emit({"event": "error", "kind": "unavailable", "message": str(exc)})
        return 2

    try:
        # int8 on CPU: several times faster than float32, with no meaningful
        # accuracy cost at these model sizes.
        model = WhisperModel(options.model, device="cpu", compute_type="int8")
    except Exception as exc:
        emit({
            "event": "error",
            "kind": "model",
            "message": str(exc),
            "detail": traceback.format_exc(),
        })
        return 3

    try:
        segments, info = model.transcribe(
            options.audio,
            language=options.language or None,
            vad_filter=True,            # skip silence rather than hallucinate in it
            beam_size=5,
            condition_on_previous_text=False,  # one bad line cannot derail the rest
        )
        emit({
            "event": "ready",
            "language": getattr(info, "language", "") or "",
            "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
            "duration": float(getattr(info, "duration", 0.0) or 0.0),
        })

        count = 0
        # The generator does the actual work, so results stream out as they come.
        for segment in segments:
            text = (segment.text or "").strip()
            if not text:
                continue
            count += 1
            emit({
                "event": "utterance",
                "start": float(segment.start),
                "end": float(segment.end),
                "text": text,
            })
        emit({"event": "done", "count": count})
    except BrokenPipeError:
        # The parent stopped listening: it cancelled. Nothing to report to.
        return 0
    except Exception as exc:
        emit({
            "event": "error",
            "kind": "transcribe",
            "message": str(exc),
            "detail": traceback.format_exc(),
        })
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
