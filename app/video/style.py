"""How burned-in subtitles look.

Sizes and margins are percentages of the video's own height, never pixels. A
28-pixel caption is comfortable on a 720p screen recording and nearly invisible
on a 4K one, so a style chosen on one video would silently be wrong on the next.
As a fraction of the frame, the same style looks the same everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

#: Positions, and what fraction of the frame the text sits from that edge.
POSITIONS = ("bottom", "middle", "top")
ALIGNMENTS = ("left", "center", "right")


@dataclass(frozen=True)
class SubtitleStyle:
    font_family: str = "Helvetica"
    #: Cap height as a percentage of video height. 4.5 is close to broadcast.
    size_percent: float = 4.5
    colour: str = "#FFFFFF"
    bold: bool = True

    #: An outline keeps white text readable over a white slide, which a plain
    #: fill does not. This is why subtitles almost always have one.
    outline: bool = True
    outline_colour: str = "#000000"
    #: As a fraction of the text size, so it scales with everything else.
    outline_width: float = 0.14

    #: A solid box is the fallback when the video is genuinely busy.
    box: bool = False
    box_colour: str = "#000000"
    box_opacity: float = 0.55

    position: str = "bottom"
    alignment: str = "center"
    #: Distance from the chosen edge, as a percentage of video height.
    margin_percent: float = 7.0
    #: Text wraps within this share of the width; full-bleed lines are unreadable.
    max_width_percent: float = 86.0
    line_spacing: float = 1.15

    def scaled_to(self, video_height: int) -> "RenderedMetrics":
        """Turn the percentages into pixels for one particular video."""
        size = max(10, round(video_height * self.size_percent / 100))
        return RenderedMetrics(
            font_size=size,
            outline_px=max(1, round(size * self.outline_width)) if self.outline else 0,
            margin=round(video_height * self.margin_percent / 100),
        )

    def with_changes(self, **changes) -> "SubtitleStyle":
        return replace(self, **changes)


@dataclass(frozen=True)
class RenderedMetrics:
    font_size: int
    outline_px: int
    margin: int


#: Ready-made looks, so nobody has to understand outlines to get a good result.
PRESETS: tuple[tuple[str, str, SubtitleStyle], ...] = (
    (
        "Clean",
        "White with a soft outline. Works on almost anything.",
        SubtitleStyle(),
    ),
    (
        "Boxed",
        "White on a dark band. Best over busy or pale footage.",
        SubtitleStyle(box=True, outline=False),
    ),
    (
        "Bold yellow",
        "High contrast, the classic look for teaching videos.",
        SubtitleStyle(colour="#FFD54A", outline_width=0.18),
    ),
    (
        "Subtle",
        "Smaller and lower, for when the picture matters more.",
        SubtitleStyle(size_percent=3.6, margin_percent=5.0, bold=False),
    ),
)


def preset(name: str) -> SubtitleStyle:
    for label, _description, style in PRESETS:
        if label == name:
            return style
    return SubtitleStyle()
