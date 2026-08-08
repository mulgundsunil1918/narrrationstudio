"""Configurable text replacement (§4).

This module only ever applies rules the user can see and edit. It contains no
spell-checker, no language model, and no medical dictionary -- a transcription
of "ACM3" becomes "Acme" because a rule says so, and for no other reason.
That constraint is deliberate: silently "correcting" a drug name or a lab value
would be far worse than leaving a brand name misspelt.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from app.core.errors import StudioError

# Characters that may not appear immediately beside a whole-word match. Uses an
# explicit class rather than \b so rules containing spaces ("Pedi aid") still
# behave as whole-word matches.
_WORD_CHARS = r"A-Za-z0-9"


@dataclass
class ReplacementRule:
    """One find/replace rule."""

    pattern: str
    replacement: str
    whole_word: bool = True
    case_sensitive: bool = False
    is_regex: bool = False
    enabled: bool = True
    note: str = ""

    def compile(self) -> re.Pattern[str]:
        if self.is_regex:
            body = self.pattern
        else:
            body = re.escape(self.pattern)
            if self.whole_word:
                body = f"(?<![{_WORD_CHARS}]){body}(?![{_WORD_CHARS}])"
        flags = 0 if self.case_sensitive else re.IGNORECASE
        try:
            return re.compile(body, flags)
        except re.error as exc:
            raise StudioError(
                f"The rule “{self.pattern}” is not a valid search pattern.",
                suggestion="Turn off “regular expression” for this rule, or fix the pattern.",
                cause=exc,
            ) from exc


@dataclass(frozen=True)
class Replacement:
    """A single applied substitution, for the preview."""

    segment_index: int
    rule: ReplacementRule
    found: str
    start: int
    end: int


@dataclass(frozen=True)
class SegmentPreview:
    segment_index: int
    before: str
    after: str
    replacements: tuple[Replacement, ...]

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass
class CleanupPreview:
    """The full result of a dry run, shown before anything is applied."""

    previews: list[SegmentPreview] = field(default_factory=list)

    @property
    def changed_previews(self) -> list[SegmentPreview]:
        return [p for p in self.previews if p.changed]

    @property
    def segment_count(self) -> int:
        return len(self.changed_previews)

    @property
    def replacement_count(self) -> int:
        return sum(len(p.replacements) for p in self.changed_previews)

    def counts_by_rule(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for preview in self.changed_previews:
            for replacement in preview.replacements:
                key = f"{replacement.rule.pattern} → {replacement.rule.replacement}"
                counts[key] = counts.get(key, 0) + 1
        return counts

    def as_text_map(self) -> dict[int, str]:
        return {p.segment_index: p.after for p in self.changed_previews}


def apply_rules(
    text: str, rules: Sequence[ReplacementRule], segment_index: int = 0
) -> tuple[str, list[Replacement]]:
    """Apply every enabled rule to ``text``.

    Rules are applied in order, each over the result of the previous one. A match
    whose text already equals the replacement is skipped, so a case-insensitive
    rule does not report "Acme → Acme" as a change.
    """
    applied: list[Replacement] = []
    current = text

    for rule in rules:
        if not rule.enabled or not rule.pattern:
            continue
        compiled = rule.compile()
        result: list[str] = []
        cursor = 0
        for match in compiled.finditer(current):
            found = match.group(0)
            if found == rule.replacement:
                continue  # already correct
            result.append(current[cursor : match.start()])
            result.append(rule.replacement)
            applied.append(
                Replacement(
                    segment_index=segment_index,
                    rule=rule,
                    found=found,
                    start=match.start(),
                    end=match.end(),
                )
            )
            cursor = match.end()
        if cursor:
            result.append(current[cursor:])
            current = "".join(result)

    return current, applied


def preview(
    texts: Iterable[str], rules: Sequence[ReplacementRule]
) -> CleanupPreview:
    """Dry-run the rules over every segment's text (§4: preview before applying)."""
    result = CleanupPreview()
    for index, text in enumerate(texts):
        after, replacements = apply_rules(text, rules, segment_index=index)
        result.previews.append(
            SegmentPreview(
                segment_index=index,
                before=text,
                after=after,
                replacements=tuple(replacements),
            )
        )
    return result


# -- persistence ---------------------------------------------------------


def default_rules() -> list[ReplacementRule]:
    """No rules ship with the app.

    A replacement dictionary is inherently specific to one project's subject
    matter and vocabulary. Shipping someone else's terms as defaults would mean
    the app silently rewrites words the user never asked it to touch — the exact
    failure this module exists to prevent. Rules are added by the user, or
    imported from a file, and are visible in Settings before they ever run.
    """
    return []


def load_rules(path: Path) -> list[ReplacementRule]:
    """Load rules from a JSON file, falling back to defaults if absent."""
    if not path.exists():
        return default_rules()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise StudioError(
            "The text-cleanup rules file could not be read.",
            suggestion="Fix or delete it, and the built-in defaults will be used.",
            detail=str(path),
            cause=exc,
        ) from exc

    entries = payload.get("rules", payload) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise StudioError(
            "The text-cleanup rules file is not in the expected format.",
            suggestion="Delete it to restore the built-in defaults.",
            detail=str(path),
        )

    known = {f.name for f in ReplacementRule.__dataclass_fields__.values()}
    rules: list[ReplacementRule] = []
    for entry in entries:
        if not isinstance(entry, dict) or "pattern" not in entry:
            continue
        rules.append(ReplacementRule(**{k: v for k, v in entry.items() if k in known}))
    return rules


def save_rules(path: Path, rules: Sequence[ReplacementRule]) -> Path:
    """Persist rules as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "rules": [asdict(rule) for rule in rules]}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path
