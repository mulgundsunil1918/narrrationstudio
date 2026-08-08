#!/usr/bin/env python3
"""Turn any SRT into timestamp-locked narration.

    generate_natural_tts.py input.srt

Works with any valid SRT. Nothing about the file is hard-coded, and the output
name is derived from the input: ``Tutorial_01.srt`` → ``Tutorial_01_Kokoro_Final.wav``.

The ``--max-group`` cap bounds a single TTS generation, not the project. A
20-minute SRT produces 20 minutes of audio, in as many groups as it takes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.audio.assemble import DEFAULT_CROSSFADE_MS
from app.audio.timing import FillPolicy, FitOptions
from app.config import DEFAULT_VOICE
from app.core.errors import PediAidError
from app.core.timecode import format_display
from app.logging_setup import setup_logging
from app.narration.grouping import DEFAULT_MAX_GROUP_MS, GroupingOptions, build_plan
from app.narration.groups import NarrationMode, SpeedSafety
from app.narration.report import (
    format_outcome_table,
    format_preview_table,
    outcomes_from,
    preview_plan,
    silence_warnings,
)
from app.srt.parser import load as load_subtitles
from app.pipeline import (
    GenerationSettings,
    GroupProgress,
    derive_output_path,
    generate_from_file,
)
from app.tts.base import EngineUnavailable
from app.tts.registry import engine as get_engine

SAFETY_MARK = {
    SpeedSafety.SAFE: "ok",
    SpeedSafety.WARNING: "!",
    SpeedSafety.STRONG_WARNING: "!!",
    SpeedSafety.NEEDS_CONFIRMATION: "!!!",
}

PREVIEW_CHOICES = {
    "60": 60_000,
    "120": 120_000,
    "300": 300_000,
    "full": None,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_natural_tts.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", type=Path, nargs="?", help="Path to an .srt, .txt or .md file")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="Output WAV path or directory (default: alongside the input)",
    )
    parser.add_argument(
        "--voice", default=DEFAULT_VOICE,
        help=f"Voice identifier (default: {DEFAULT_VOICE})",
    )
    parser.add_argument("--engine", default="kokoro", help="TTS engine (default: kokoro)")
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Engine speaking rate before timeline fitting (default: 1.0)",
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in NarrationMode],
        default=NarrationMode.NATURAL.value,
        help="Narration grouping mode (default: natural)",
    )
    parser.add_argument(
        "--max-group", type=float, default=DEFAULT_MAX_GROUP_MS / 1000, metavar="SECONDS",
        help=(
            "Maximum duration of ONE narration group, in seconds "
            f"(default: {DEFAULT_MAX_GROUP_MS // 1000}). This is NOT a limit on the "
            "project: the full SRT is always generated."
        ),
    )
    parser.add_argument(
        "--test", choices=sorted(PREVIEW_CHOICES), default="full", metavar="LENGTH",
        help=(
            "Render only the first 60/120/300 seconds for a quick check, or "
            "'full' for the whole project (default: full). A test render never "
            "changes the project's own duration."
        ),
    )
    parser.add_argument("--mp3", action="store_true", help="Also write an MP3")
    parser.add_argument("--no-cache", action="store_true", help="Ignore cached audio")
    parser.add_argument(
        "--no-crossfade", action="store_true",
        help="Disable the micro-crossfade between narration groups",
    )
    parser.add_argument(
        "--no-pronunciation", action="store_true",
        help="Do not apply the pronunciation dictionary",
    )
    parser.add_argument(
        "--pad-only", action="store_true",
        help=(
            "Never slow speech to fill a window; append silence instead "
            "(reproduces the original proof-of-concept behaviour)"
        ),
    )
    parser.add_argument("--list-voices", action="store_true", help="List voices and exit")
    parser.add_argument(
        "--plan-only", action="store_true",
        help="Print the narration-group table and exit without generating audio",
    )
    parser.add_argument(
        "--quiet-tables", action="store_true", help="Skip the validation tables"
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    return parser


def list_voices(engine_id: str) -> int:
    try:
        backend = get_engine(engine_id)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    available, reason = backend.is_available()
    print(f"{backend.display_name}  [{backend.locality.badge}]")
    if not available:
        print(f"  unavailable: {reason}")
        return 1

    installed = getattr(backend, "installed_voice_files", lambda: set())()
    print(f"  {'VOICE':<14} {'GENDER':<10} {'LANGUAGE':<20} TAGS")
    for voice in backend.voices():
        mark = "*" if voice.identifier in installed else " "
        tags = ", ".join(voice.tags)
        print(
            f"{mark} {voice.identifier:<14} {voice.gender:<10} "
            f"{voice.language:<20} {tags}"
        )
    if installed:
        print("\n* already downloaded; others download on first use.")
    return 0


def on_progress(progress: GroupProgress) -> None:
    bar_width = 24
    filled = int(bar_width * (progress.index + 1) / max(1, progress.total))
    bar = "█" * filled + "░" * (bar_width - filled)
    source = "cache" if progress.from_cache else f"{progress.seconds_taken:.1f}s"
    mark = SAFETY_MARK[progress.safety]

    print(
        f"[{progress.index + 1:>3}/{progress.total}] {bar} "
        f"{format_display(progress.start_ms)}→{format_display(progress.end_ms)}  "
        f"target {progress.target_ms / 1000:6.2f}s  "
        f"tts {progress.generated_ms / 1000:6.2f}s  "
        f"speed {progress.speed_factor:4.2f}x {mark:<3} ({source})"
    )
    preview = progress.text[:88].replace("\n", " ")
    print(f"      {preview}{'…' if len(progress.text) > 88 else ''}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose)

    if args.list_voices:
        return list_voices(args.engine)
    if args.input is None:
        build_parser().print_help()
        return 2
    if not args.input.exists():
        print(f"error: no such file: {args.input}", file=sys.stderr)
        return 2

    settings = GenerationSettings(
        engine=args.engine,
        voice=args.voice,
        speed=args.speed,
        mode=NarrationMode(args.mode),
        grouping=GroupingOptions(max_group_ms=int(args.max_group * 1000)),
        fit=FitOptions(
            fill_policy=FillPolicy.PAD_ONLY if args.pad_only else FillPolicy.STRETCH_THEN_PAD
        ),
        crossfade_ms=0 if args.no_crossfade else DEFAULT_CROSSFADE_MS,
        use_cache=not args.no_cache,
        apply_pronunciation=not args.no_pronunciation,
        preview_until_ms=PREVIEW_CHOICES[args.test],
    )

    output = derive_output_path(args.input, args.output)
    print(f"Input   : {args.input}")
    print(f"Output  : {output}")
    print(f"Voice   : {args.voice}   Engine: {args.engine}   Mode: {args.mode}")
    print(
        f"Max narration group: {args.max_group:.0f}s "
        "(per-generation cap — the whole SRT is always rendered)"
    )
    if settings.preview_until_ms:
        print(
            f"TEST RENDER: only groups starting before "
            f"{settings.preview_until_ms // 1000}s. The project's own length is unchanged."
        )
    print("-" * 78)

    # -- pre-generation validation (§9) ---------------------------------
    try:
        parsed = load_subtitles(args.input)
    except PediAidError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    narration = build_plan(parsed.segments, settings.mode, settings.grouping)
    rows = preview_plan(narration, parsed.segments)
    timeline_ms = max(segment.end_ms for segment in parsed.segments)

    print(
        f"{len(parsed.segments)} captions  ·  timeline {format_display(timeline_ms)}  "
        f"·  {len(narration)} narration groups"
    )
    if not args.quiet_tables:
        print()
        print(format_preview_table(rows))
    unfinished = [row for row in rows if not row.natural_boundary]
    if unfinished:
        print(
            f"\n! {len(unfinished)} group(s) end on a continuation word. The next "
            "group continues the same sentence, so the join will be audible."
        )
    if args.plan_only:
        return 0
    print("-" * 78)

    try:
        path, outcome = generate_from_file(
            args.input, args.output, settings, on_progress, also_mp3=args.mp3
        )
    except (PediAidError, EngineUnavailable) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        suggestion = getattr(exc, "suggestion", "")
        if suggestion:
            print(f"       {suggestion}", file=sys.stderr)
        if args.verbose:
            detail = getattr(exc, "detail", "")
            if detail:
                print(f"\n{detail}", file=sys.stderr)
        return 1

    print("-" * 78)

    # -- post-generation validation (§10) -------------------------------
    outcomes = outcomes_from(outcome.fit_plans)
    if not args.quiet_tables:
        print(format_outcome_table(outcomes))
        print()

    holes = silence_warnings(outcomes)
    if holes:
        print(f"WARNING: unnatural internal silence detected in {len(holes)} group(s):")
        for line in holes:
            print(f"  - {line}")
        print()
    else:
        print("No unnatural internal silence: every group fills its own window.\n")

    print(f"Wrote        : {path}")
    print(
        f"Duration     : {outcome.duration_ms / 1000:.2f}s "
        f"(SRT timeline {outcome.timeline_ms / 1000:.2f}s)"
    )
    print(
        f"Groups       : {len(outcome.plan)}   "
        f"Captions: {sum(group.size for group in outcome.plan)}"
    )
    print(f"Cache        : {outcome.cache_hits} reused, {outcome.cache_misses} generated")
    print(f"Peak         : {outcome.report.peak_before_normalise:.3f} → gain {outcome.report.gain_applied:.3f}")
    print(f"Crossfades   : {outcome.report.crossfades_applied}")
    print(f"Elapsed      : {outcome.seconds_taken:.1f}s")

    if outcome.warnings:
        print(f"\n{len(outcome.warnings)} note(s):")
        for warning in outcome.warnings[:12]:
            print(f"  - {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
