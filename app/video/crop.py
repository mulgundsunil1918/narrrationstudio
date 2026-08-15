"""Cut the picture to a different shape.

A crop is a choice of shape plus a choice of what to keep, and those are kept
separate: the shape comes from where the video is going (9:16 for Reels and
Shorts, 1:1 for feeds), and ``pan`` says which part of the frame survives the
cut, because the interesting part of a screen recording is rarely dead centre.

Everything is computed from the source's own dimensions at export time, so the
same crop choice does the right thing on a 720p recording and a 4K one.
"""

from __future__ import annotations

from dataclasses import dataclass


def _even(value: float) -> int:
    """Video encoders want even dimensions; yuv420p subsampling requires it."""
    return max(2, (int(value) // 2) * 2)


@dataclass(frozen=True)
class CropSpec:
    """A target shape and which slice of the frame to keep."""

    aspect_w: int
    aspect_h: int
    #: 0.0 keeps the left/top edge, 1.0 the right/bottom, 0.5 the centre.
    #: It slides along whichever axis is being cut; the other is centred.
    pan: float = 0.5

    @property
    def label(self) -> str:
        return f"{self.aspect_w}:{self.aspect_h}"

    def rect(self, width: int, height: int) -> tuple[int, int, int, int]:
        """The (x, y, w, h) to keep from a ``width`` x ``height`` frame."""
        pan = min(1.0, max(0.0, self.pan))
        target = self.aspect_w / self.aspect_h
        source = width / height

        if target < source:
            # The new shape is narrower: the sides are cut, pan slides sideways.
            crop_h = _even(height)
            crop_w = min(_even(height * target), _even(width))
            x = round((width - crop_w) * pan)
            y = (height - crop_h) // 2
        else:
            # The new shape is wider (or the same): top and bottom are cut.
            crop_w = _even(width)
            crop_h = min(_even(width / target), _even(height))
            x = (width - crop_w) // 2
            y = round((height - crop_h) * pan)

        x = max(0, min(width - crop_w, x))
        y = max(0, min(height - crop_h, y))
        return x, y, crop_w, crop_h


@dataclass(frozen=True)
class FreeCrop:
    """A hand-drawn crop: any rectangle, held as fractions of the frame.

    Fractions rather than pixels for the same reason subtitle sizes are
    percentages — the rectangle is drawn on a scaled-down preview, and it has
    to mean the same thing on the full-resolution frame at export time.
    """

    left: float = 0.1
    top: float = 0.1
    width: float = 0.8
    height: float = 0.8

    #: Below this the encoder is fine but the result is a postage stamp.
    MIN_FRACTION = 0.05

    @property
    def label(self) -> str:
        return "custom"

    def normalised(self) -> "FreeCrop":
        """The same rectangle with every value pulled back into the frame."""
        width = min(1.0, max(self.MIN_FRACTION, self.width))
        height = min(1.0, max(self.MIN_FRACTION, self.height))
        left = min(1.0 - width, max(0.0, self.left))
        top = min(1.0 - height, max(0.0, self.top))
        return FreeCrop(left, top, width, height)

    def rect(self, width: int, height: int) -> tuple[int, int, int, int]:
        """The (x, y, w, h) to keep from a ``width`` x ``height`` frame."""
        spec = self.normalised()
        crop_w = min(_even(width), max(16, _even(width * spec.width)))
        crop_h = min(_even(height), max(16, _even(height * spec.height)))
        x = max(0, min(width - crop_w, round(width * spec.left)))
        y = max(0, min(height - crop_h, round(height * spec.top)))
        return x, y, crop_w, crop_h


#: The shapes people actually export for, with where each one goes.
CROP_CHOICES: tuple[tuple[str, str, tuple[int, int] | None], ...] = (
    ("original", "Original", None),
    ("9:16", "Vertical", (9, 16)),
    ("1:1", "Square", (1, 1)),
    ("4:3", "4:3", (4, 3)),
    ("16:9", "Wide", (16, 9)),
)


def crop_for(key: str, pan: float = 0.5) -> CropSpec | None:
    for choice_key, _label, aspect in CROP_CHOICES:
        if choice_key == key:
            return None if aspect is None else CropSpec(aspect[0], aspect[1], pan)
    return None
