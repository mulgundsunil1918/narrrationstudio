"""A still of the user's own video with their own caption drawn on it.

Styling controls that only describe themselves are guesswork — nobody can
picture "outline 0.14" or tell whether 4.5% is too big for their footage. So
every change redraws a real frame, at the real proportions, using exactly the
code the export uses. If it looks right here it will look right in the file.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from app.video.captions import blend, render_caption
from app.video.crop import CropSpec
from app.video.style import SubtitleStyle

logger = logging.getLogger(__name__)

#: Where to take the still from. A tenth of the way in skips title cards and
#: fades to black, which are the least representative frames in any video.
SAMPLE_POSITION = 0.1

#: Stand-in when there is no video yet: a mid grey, so both light and dark text
#: are judged fairly rather than flattered.
PLACEHOLDER_SIZE = (1280, 720)
PLACEHOLDER_TONE = 96


def preview_frame(
    video: Path | None,
    text: str,
    style: SubtitleStyle,
    target_width: int,
    crop: "CropSpec | None" = None,
    draw_caption: bool = True,
) -> tuple[QImage, str]:
    """Return a preview image and a line describing what is being shown.

    The crop is applied before the caption is drawn — the same order as the
    export — so the preview shows the caption at its size within the picture
    that will actually be kept.
    """
    frame, note = _source_frame(video)

    if crop is not None:
        x, y, crop_w, crop_h = crop.rect(frame.shape[1], frame.shape[0])
        frame = np.ascontiguousarray(frame[y : y + crop_h, x : x + crop_w])
        note += f" · cut to {crop_w}×{crop_h}"
    height, width = frame.shape[:2]

    if draw_caption:
        layer = render_caption(text, style, width, height)
        if layer is not None:
            blend(frame, layer)

    image = QImage(
        np.ascontiguousarray(frame).data, width, height, width * 3,
        QImage.Format.Format_RGB888,
    ).copy()
    # Fit a box rather than a width: a 9:16 crop scaled to full width would be
    # taller than the whole panel.
    shown = image.scaled(
        target_width, round(target_width * 0.75),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    scale = shown.width() / width
    return shown, f"{note} · shown at {round(scale * 100)}% of {width}×{height}"


def _source_frame(video: Path | None) -> tuple[np.ndarray, str]:
    """A real frame where possible, a neutral grey card where not."""
    if video is not None and Path(video).exists():
        try:
            return _decode_sample(Path(video)), f"A frame from {Path(video).name}"
        except Exception as exc:
            logger.debug("Could not read a preview frame: %s", exc)

    width, height = PLACEHOLDER_SIZE
    frame = np.full((height, width, 3), PLACEHOLDER_TONE, dtype=np.uint8)
    return frame, "No video chosen, so this is a plain grey card"


def _decode_sample(video: Path) -> np.ndarray:
    import av

    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        if container.duration:
            target = int(container.duration * SAMPLE_POSITION)
            try:
                container.seek(target)
            except Exception:
                pass    # some files will not seek; the first frame will do
        for frame in container.decode(stream):
            return frame.to_ndarray(format="rgb24")
    raise ValueError("no frames could be decoded")
