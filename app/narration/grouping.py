"""Automatic narration grouping.

For every boundary between consecutive captions the grouper computes a
**continuation score** in ``0.0 .. 1.0``: how strongly the evidence says the
speaker carried straight on rather than finishing a thought. Boundaries at or
above the join threshold are absorbed into one group.

The score combines four independent kinds of evidence, so no single one decides:

1. how the previous caption ends (punctuation, and *which word* it ends on)
2. how the next caption begins (case, continuation words, topic markers)
3. the silent gap between them
4. a length cap, applied afterwards

Point 4 matters more than it looks. A transcript line-wrapped by Whisper almost
never ends at a sentence, so on a real file nearly every boundary scores as a
continuation and the naive result is one group covering the entire video. The
cap re-splits over-long runs at their *weakest* internal boundary, which keeps
groups naturally phrased while staying small enough to cache, regenerate and
recover from errors individually.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from app.core.models import Segment
from app.narration.groups import (
    NarrationGroup,
    NarrationMode,
    NarrationPlan,
    build_narration_text,
)

# -- tuning --------------------------------------------------------------

JOIN_THRESHOLD = 0.5

#: Longest a single group may be, and the app's central quality dial.
#:
#: A transcript line-wrapped by Whisper almost never ends where a sentence ends,
#: so on real files there are very few natural break points -- one 5.5-minute
#: tutorial transcript had exactly two. Without a cap the whole
#: video becomes one utterance: perfectly continuous, but because TTS speaks at
#: its own rate inside a group, captions drift from what is being said (~7.7 s
#: worst case on that file). A tighter cap holds captions close (~2.3 s at 20 s)
#: at the cost of an audible break at each forced cut.
#:
#: 60 s sits at the knee of that curve: ~4.2 s worst-case caption drift with only
#: a handful of forced cuts. Groups created by the cap rather than by a real
#: pause are flagged ``forced_cut`` so they can be reviewed or merged by hand.
DEFAULT_MAX_GROUP_MS = 60_000
#: Generous enough that the duration cap above is what actually binds.
DEFAULT_MAX_GROUP_SEGMENTS = 16

# Words a sentence essentially cannot end on: if a caption ends here, the
# thought is unfinished no matter what punctuation says.
_DANGLING_WORDS = {
    # conjunctions
    "and", "or", "but", "nor", "yet", "so", "for", "because", "since", "although",
    "though", "while", "whereas", "unless", "until", "if", "whether",
    # prepositions
    "of", "to", "in", "on", "at", "by", "with", "from", "into", "onto", "upon",
    "about", "above", "across", "after", "against", "along", "among", "around",
    "before", "behind", "below", "beneath", "beside", "between", "beyond",
    "during", "except", "inside", "near", "outside", "over", "through",
    "throughout", "toward", "towards", "under", "underneath", "within", "without",
    # articles and determiners
    "a", "an", "the", "this", "that", "these", "those", "each", "every", "any",
    "some", "no", "both", "either", "neither", "another", "such", "its", "their",
    "our", "your", "his", "her", "my",
    # auxiliaries and common verbs that take a complement
    "is", "are", "was", "were", "be", "been", "being", "am", "has", "have", "had",
    "do", "does", "did", "will", "would", "can", "could", "shall", "should",
    "may", "might", "must", "includes", "including", "contains", "provides",
    "allows", "helps", "lets", "uses", "based",
    # relatives and connectives
    "which", "who", "whom", "whose", "what", "when", "where", "how", "than",
    "as", "also", "very", "more", "most", "not",
}

# Words that, when a caption *starts* with them, usually continue the previous
# clause rather than open a new one.
_CONTINUATION_STARTERS = {
    "and", "or", "but", "nor", "yet", "so", "because", "since", "although",
    "though", "while", "whereas", "unless", "until", "which", "who", "whom",
    "whose", "that", "than", "as", "with", "including", "such", "plus",
}

# Words that, when a caption starts with them *capitalised*, usually announce a
# new section of the narration.
_TOPIC_MARKERS = {
    "let", "lets", "now", "next", "first", "firstly", "second", "secondly",
    "third", "finally", "lastly", "moving", "turning", "here", "this", "these",
    "in", "at", "on", "from", "under", "within", "once", "after", "before",
    "when", "if", "you", "we", "the", "there", "it", "for", "coming", "another",
    "beyond", "additionally", "similarly", "likewise", "meanwhile", "overall",
}

# Abbreviations that end in a period without ending a sentence.
_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof", "st", "vs", "etc", "e.g", "i.e", "fig",
    "approx", "inc", "ltd", "dept", "jr", "sr", "al", "no", "kg", "mg", "ml",
}

_WORD_RE = re.compile(r"[A-Za-z']+")


@dataclass(frozen=True)
class BoundaryAnalysis:
    """Why the grouper did or did not join caption ``index`` with ``index + 1``."""

    index: int
    #: Clamped to 0..1. Drives the join/break decision.
    score: float
    #: Unclamped. Two boundaries can both clamp to 1.0 while one is far more
    #: strongly a continuation ("…ends on 'the'") than the other. Cut selection
    #: needs that difference, so it is preserved here.
    raw_score: float
    gap_ms: int
    join_reasons: tuple[str, ...]
    break_reasons: tuple[str, ...]

    @property
    def joins(self) -> bool:
        return self.score >= JOIN_THRESHOLD

    @property
    def explanation(self) -> str:
        reasons = self.join_reasons if self.joins else self.break_reasons
        return "; ".join(reasons) if reasons else "no strong signal either way"


#: How far a group may run past the cap to reach a materially better boundary.
DEFAULT_OVERFLOW_MS = 12_000
#: How much better a boundary past the cap must be before the overflow is worth
#: taking. Expressed in continuation score.
OVERFLOW_ADVANTAGE = 0.15


@dataclass(frozen=True)
class GroupingOptions:
    #: The cap on a single **TTS generation group**, not on the project. Total
    #: length is whatever the SRT says: a 20-minute SRT produces 20 minutes of
    #: audio across as many groups as it takes.
    max_group_ms: int = DEFAULT_MAX_GROUP_MS
    max_group_segments: int = DEFAULT_MAX_GROUP_SEGMENTS
    join_threshold: float = JOIN_THRESHOLD
    #: A silence at least this long is treated as a deliberate pause.
    meaningful_gap_ms: int = 500
    #: The cap is a ceiling to aim under, not a target to fill. A group may run
    #: up to this far past it when doing so reaches a genuinely better boundary
    #: instead of cutting mid-sentence.
    overflow_ms: int = DEFAULT_OVERFLOW_MS
    #: A cap-driven cut must not carve off a stub. Every boundary inside a run
    #: is a continuation, so there is no "good" early cut to be had -- taking
    #: one only adds an extra join. Fraction of ``max_group_ms``.
    min_group_fraction: float = 0.5

    @property
    def min_group_ms(self) -> int:
        return int(self.max_group_ms * self.min_group_fraction)


# -- boundary scoring ----------------------------------------------------


def analyse_boundary(
    previous: Segment, following: Segment, index: int, options: GroupingOptions
) -> BoundaryAnalysis:
    """Score one caption boundary. Higher means "the speaker carried on"."""
    score = 0.5
    join: list[str] = []
    brk: list[str] = []

    previous_text = previous.text.strip()
    next_text = following.text.strip()
    gap = following.start_ms - previous.end_ms

    # -- 1. how the previous caption ends --------------------------------
    if not previous_text:
        brk.append("previous caption is empty")
        score -= 0.4
    elif _ends_sentence(previous_text):
        score -= 0.55
        brk.append("previous caption ends a sentence")
    elif previous_text.endswith((":", ";")):
        score -= 0.10
        brk.append("previous caption ends on a clause break")
    elif previous_text.endswith(","):
        score += 0.25
        join.append("previous caption ends mid-clause on a comma")
    elif previous_text.endswith(("…", "-", "—", "–")):
        score += 0.35
        join.append("previous caption trails off")
    else:
        score += 0.45
        join.append("previous caption has no end punctuation")

    last_word = _last_word(previous_text)
    if last_word in _DANGLING_WORDS:
        score += 0.35
        join.append(f"previous caption ends on “{last_word}”")

    # -- 2. how the next caption begins ----------------------------------
    first_word = _first_word(next_text)
    if next_text[:1].islower():
        score += 0.40
        join.append("next caption starts in lower case")
    if first_word in _CONTINUATION_STARTERS:
        score += 0.25
        join.append(f"next caption starts with “{first_word}”")
    elif next_text[:1].isupper() and first_word in _TOPIC_MARKERS:
        score -= 0.30
        brk.append(f"next caption opens a new thought with “{first_word.title()}”")

    # -- 3. the silence between them -------------------------------------
    if gap <= 0:
        score += 0.20
        join.append("no gap between captions")
    elif gap < 200:
        score += 0.10
        join.append("captions are nearly contiguous")
    elif gap < options.meaningful_gap_ms:
        pass  # ambiguous
    elif gap < 1000:
        score -= 0.30
        brk.append(f"{gap} ms pause between captions")
    else:
        score -= 0.70
        brk.append(f"{gap / 1000:.1f} s pause between captions")

    return BoundaryAnalysis(
        index=index,
        score=max(0.0, min(1.0, score)),
        raw_score=score,
        gap_ms=gap,
        join_reasons=tuple(join),
        break_reasons=tuple(brk),
    )


def analyse_all(
    segments: Sequence[Segment], options: GroupingOptions | None = None
) -> list[BoundaryAnalysis]:
    """Score every boundary in the document."""
    options = options or GroupingOptions()
    return [
        analyse_boundary(segments[i], segments[i + 1], i, options)
        for i in range(len(segments) - 1)
    ]


# -- plan construction ---------------------------------------------------


def build_plan(
    segments: Sequence[Segment],
    mode: NarrationMode = NarrationMode.NATURAL,
    options: GroupingOptions | None = None,
) -> NarrationPlan:
    """Group ``segments`` according to ``mode``."""
    options = options or GroupingOptions()
    if not segments:
        return NarrationPlan(groups=[], mode=mode)

    if mode is NarrationMode.EXACT:
        return _exact_plan(segments)

    analyses = analyse_all(segments, options)
    runs = _runs_from_analyses(len(segments), analyses, options)
    runs = _apply_length_cap(runs, segments, analyses, options)

    groups: list[NarrationGroup] = []
    for position, run in enumerate(runs):
        members = [segments[i] for i in run]
        groups.append(
            NarrationGroup(
                segment_uids=[s.uid for s in members],
                narration_text=build_narration_text(members),
                reasons=_reasons_for_run(run, analyses),
                forced_cut=_is_forced_cut(run, analyses, options) if position else False,
            )
        )
    return NarrationPlan(groups=groups, mode=mode)


def _is_forced_cut(
    run: Sequence[int], analyses: Sequence[BoundaryAnalysis], options: GroupingOptions
) -> bool:
    """True when the boundary opening this run wanted to join but was cut anyway.

    A cut the signals asked for is a natural pause. A cut the length cap imposed
    lands mid-sentence and will be audible, so it is worth reporting.
    """
    boundary = run[0] - 1
    if boundary < 0 or boundary >= len(analyses):
        return False
    return analyses[boundary].score >= options.join_threshold


def _exact_plan(segments: Sequence[Segment]) -> NarrationPlan:
    """One group per caption -- the original proof-of-concept's behaviour."""
    return NarrationPlan(
        groups=[
            NarrationGroup(
                segment_uids=[segment.uid],
                narration_text=build_narration_text([segment]),
                reasons=("exact subtitle timing",),
            )
            for segment in segments
        ],
        mode=NarrationMode.EXACT,
    )


def _runs_from_analyses(
    count: int, analyses: Sequence[BoundaryAnalysis], options: GroupingOptions
) -> list[list[int]]:
    """Split indices wherever a boundary scores below the join threshold."""
    runs: list[list[int]] = []
    current = [0]
    for analysis in analyses:
        if analysis.score >= options.join_threshold:
            current.append(analysis.index + 1)
        else:
            runs.append(current)
            current = [analysis.index + 1]
    runs.append(current)
    return [run for run in runs if run][:count] or [list(range(count))]


def _apply_length_cap(
    runs: list[list[int]],
    segments: Sequence[Segment],
    analyses: Sequence[BoundaryAnalysis],
    options: GroupingOptions,
) -> list[list[int]]:
    """Recursively split any run that is too long, at its weakest boundary."""
    result: list[list[int]] = []
    for run in runs:
        result.extend(_split_run(run, segments, analyses, options))
    return result


def _split_run(
    run: list[int],
    segments: Sequence[Segment],
    analyses: Sequence[BoundaryAnalysis],
    options: GroupingOptions,
) -> list[list[int]]:
    """Cut an over-long run into groups, greedily and as few times as possible.

    Each cut walks as far as the cap allows, then picks the weakest boundary in
    reach. Every cut inside a run is by definition mid-sentence -- the run only
    exists because all its boundaries scored as continuations -- so the goal is
    to make as few of them as possible and put each one where it hurts least.

    Balanced splitting would be wrong here: on a line-wrapped transcript nearly
    every boundary scores identically, so halving repeatedly just makes more
    equally-bad cuts than filling each group to the cap does.
    """
    groups: list[list[int]] = []
    start = 0
    total = len(run)

    while start < total:
        within_cap: list[int] = []
        past_cap: list[int] = []
        for position in range(start, total):
            duration = segments[run[position]].end_ms - segments[run[start]].start_ms
            if position - start + 1 > options.max_group_segments:
                break
            if duration <= options.max_group_ms:
                within_cap.append(position)
            elif duration <= options.max_group_ms + options.overflow_ms:
                past_cap.append(position)
            else:
                break

        if not within_cap and not past_cap:
            # A single caption longer than the whole cap. It still has to be
            # spoken, so it becomes a group of its own and exceeds the cap.
            within_cap = [start]

        reachable = within_cap or past_cap
        if reachable[-1] == total - 1:
            groups.append(run[start:])
            break

        def long_enough(candidates: list[int]) -> list[int]:
            kept = [
                position
                for position in candidates
                if segments[run[position]].end_ms - segments[run[start]].start_ms
                >= options.min_group_ms
            ]
            return kept or candidates

        cut = _choose_cut(
            run, long_enough(within_cap), long_enough(past_cap), analyses, options
        )
        groups.append(run[start : cut + 1])
        start = cut + 1

    return groups


def _choose_cut(
    run: Sequence[int],
    within_cap: Sequence[int],
    past_cap: Sequence[int],
    analyses: Sequence[BoundaryAnalysis],
    options: GroupingOptions,
) -> int:
    """Pick where to end a group, preferring the weakest boundary in reach.

    Ties go to the *latest* candidate, which makes groups as long as the cap
    allows and therefore minimises the total number of cuts. A boundary past the
    cap only wins if it is clearly better, not merely later.
    """

    def score_at(position: int) -> float:
        # Uses the *unclamped* score: inside a run every boundary clamps to 1.0,
        # so the clamped value cannot tell "…opportunities" from "…for the".
        boundary = run[position]
        return analyses[boundary].raw_score if boundary < len(analyses) else 99.0

    def best_of(candidates: Sequence[int]) -> tuple[int, float] | None:
        if not candidates:
            return None
        best = min(candidates, key=lambda p: (score_at(p), -p))
        return best, score_at(best)

    inside = best_of(within_cap)
    outside = best_of(past_cap)

    if inside is None:
        assert outside is not None
        return outside[0]
    if outside is not None and outside[1] <= inside[1] - OVERFLOW_ADVANTAGE:
        return outside[0]
    return inside[0]


def _reasons_for_run(
    run: Sequence[int], analyses: Sequence[BoundaryAnalysis]
) -> tuple[str, ...]:
    """Collect the distinct reasons the captions in ``run`` were joined."""
    if len(run) < 2:
        return ()
    seen: list[str] = []
    for index in run[:-1]:
        if index >= len(analyses):
            continue
        for reason in analyses[index].join_reasons:
            if reason not in seen:
                seen.append(reason)
    return tuple(seen)


# -- manual editing ------------------------------------------------------


def split_group(
    plan: NarrationPlan,
    group_index: int,
    after_member: int,
    segments: Sequence[Segment],
) -> NarrationPlan:
    """Split one group in two after its ``after_member``-th caption."""
    updated = plan.copy()
    group = updated.groups[group_index]
    if not 0 <= after_member < group.size - 1:
        raise ValueError("Choose a split point inside the group.")

    by_uid = {segment.uid: segment for segment in segments}
    left_uids = group.segment_uids[: after_member + 1]
    right_uids = group.segment_uids[after_member + 1 :]

    def make(uids: list[str]) -> NarrationGroup:
        members = [by_uid[uid] for uid in uids if uid in by_uid]
        return NarrationGroup(
            segment_uids=uids,
            narration_text=build_narration_text(members),
            reasons=("split by hand",),
        )

    updated.groups[group_index : group_index + 1] = [make(left_uids), make(right_uids)]
    updated.mode = NarrationMode.MANUAL
    return updated


def merge_groups(
    plan: NarrationPlan, group_indices: Sequence[int], segments: Sequence[Segment]
) -> NarrationPlan:
    """Merge consecutive groups into one."""
    ordered = sorted(set(group_indices))
    if len(ordered) < 2:
        raise ValueError("Select at least two narration groups to merge.")
    if ordered != list(range(ordered[0], ordered[-1] + 1)):
        raise ValueError("Only consecutive narration groups can be merged.")

    updated = plan.copy()
    by_uid = {segment.uid: segment for segment in segments}
    uids: list[str] = []
    for index in ordered:
        uids.extend(updated.groups[index].segment_uids)

    members = [by_uid[uid] for uid in uids if uid in by_uid]
    merged = NarrationGroup(
        segment_uids=uids,
        narration_text=build_narration_text(members),
        reasons=("merged by hand",),
    )
    updated.groups[ordered[0] : ordered[-1] + 1] = [merged]
    updated.mode = NarrationMode.MANUAL
    return updated


# -- helpers -------------------------------------------------------------


def _ends_sentence(text: str) -> bool:
    """True when the text ends on a real sentence terminator.

    Excludes abbreviations ("Dr.") and decimals ("0.5") that merely end in a dot.
    """
    stripped = text.rstrip().rstrip('"\'”’)]')
    if not stripped:
        return False
    if stripped.endswith(("!", "?")):
        return True
    if not stripped.endswith("."):
        return False

    token = stripped.rsplit(" ", 1)[-1].rstrip(".").lower()
    if token in _ABBREVIATIONS:
        return False
    if len(token) == 1 and token.isalpha():
        return False  # a single initial, e.g. "J."
    if token and token[-1].isdigit():
        return False  # a numbered item or decimal
    return True


def _last_word(text: str) -> str:
    words = _WORD_RE.findall(text)
    return words[-1].lower() if words else ""


def _first_word(text: str) -> str:
    match = _WORD_RE.search(text)
    return match.group(0).lower() if match else ""
