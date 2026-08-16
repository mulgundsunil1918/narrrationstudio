"""The subtitle-preparation prompt the user hands to an AI before importing.

Kept as a template with fill-ins rather than one person's vocabulary, so it
works for a medical tutorial, a product demo or a lecture alike. The user's own
brand and terms go in the two placeholders and nowhere else.

The rules here exist because of what this app does downstream: narration is
generated per *sentence group*, not per subtitle, so the single most valuable
thing an editor can do is punctuate honestly — a full stop only where a sentence
truly ends. That is what lets the grouper find natural breaks instead of cutting
mid-phrase.
"""

from __future__ import annotations

BRAND_PLACEHOLDER = "[YOUR BRAND OR PRODUCT NAME]"
TERMS_PLACEHOLDER = "[list your specialist terms here, comma separated]"

SRT_PROMPT_TEMPLATE = """\
You are my subtitle editor. I am uploading an SRT that came out of speech-to-text.
Polish the wording so it reads naturally when spoken aloud, and give it back to
me as an SRT file I can download.

The result must be accurate, grammatically correct, natural when spoken, and
EXACTLY timestamp-preserved.

I am going to load your file straight back into the app that produced this one,
and it checks every timestamp against the original. If you change any of them,
your edits to those lines are discarded and I have to do this again — so treat
the timestamps as read-only no matter how tempting it is to adjust them.

==================================================
1. TIMESTAMPS ARE SACRED
==================================================

NEVER change:
- subtitle number
- start timestamp
- end timestamp
- subtitle count
- overall duration

Do not merge, delete, reorder or retime subtitles.
Do not change timings to make the grammar work.

ONLY EDIT THE TEXT.

==================================================
2. SOURCE OF TRUTH
==================================================

The SRT comes from my actual recording.

Do NOT invent information, add facts I did not say, or remove content.
Do NOT substantially rewrite my meaning or reorder my points.
Your job is to polish what I actually said.

==================================================
3. TRANSCRIPTION ERRORS
==================================================

Correct obvious speech-to-text errors using the surrounding subtitles for
context: misheard words, accidental duplication, missing small words, wrong
capitalisation, punctuation, and clearly wrong terminology or names.

If a correction is uncertain, do NOT confidently invent one. Keep the closest
meaning the text supports.

==================================================
4. NAMES AND TERMINOLOGY
==================================================

Always write my brand exactly as:

{brand}

Correct obvious mis-hearings of it.

Pay particular attention to these specialist terms, checking context carefully:

{terms}

Do not change numbers, dates, doses, measurements, versions, scores or other
values unless the source text clearly supports the correction.

==================================================
5. NATURAL SPOKEN FLOW  (the most important section)
==================================================

Subtitle boundaries frequently fall in the MIDDLE of a sentence.

Do NOT treat each subtitle as a standalone sentence.
Do NOT add a full stop merely because a subtitle ends.

Read each subtitle together with the one before and after it, and make the text
flow continuously across the boundary.

GOOD:
  1: "Welcome to Acme, a platform built specifically for"
  2: "small teams and independent studios."

BAD:
  1: "Welcome to Acme, a platform built specifically for."
  2: "Small teams and independent studios."

==================================================
5b. PACING CHECK
==================================================

People speak at about 2.4 words per second. For every caption, divide its word
count by its duration in seconds:

- Over 2.8 words/second: the wording is too dense for its window. TIGHTEN THE
  WORDING until it fits. Never touch the timestamp to make room.
- A caption that is only a dangling fragment ("with", "the", "of") cannot be
  fixed by rewording, and you may not merge or retime captions. List every
  such caption under the heading "PACING PROBLEMS" in your summary instead,
  so I can repair the timings in my app.

==================================================
6. PUNCTUATION FOR SPEECH
==================================================

Punctuation tells the speech engine where to breathe.

- Commas where a short natural pause belongs.
- Full stops ONLY where a sentence genuinely ends.
- Keep a spoken list as one flowing list; do not break it into fragments.
- Where the recording genuinely changes topic, a real sentence boundary is
  correct and wanted.

The goal is natural narration, not one giant sentence.

==================================================
7. STYLE
==================================================

Preserve my voice: clear, confident, conversational, explaining something to a
peer. Not a research paper, and not marketing copy. Avoid added adjectives and
corporate phrasing. Remove accidental stammered repetition, but keep deliberate
emphasis.

==================================================
8. VERIFY BEFORE RETURNING
==================================================

Check that:
- subtitle count is unchanged
- every subtitle number is unchanged
- every start and end timestamp is unchanged, character for character
- the overall duration is unchanged

Then read the whole thing as one continuous script and ask: if the subtitle
boundaries vanished and a narrator read this aloud, would it sound natural?
If not, improve the TEXT — never the timestamps.

==================================================
9. OUTPUT
==================================================

Return:
1. The enhanced file as a downloadable .srt, named <original_name>_enhanced.srt
2. A short summary of the important corrections you made
3. The confirmation line:
   "All original timestamps and subtitle numbering have been preserved."

Give me the file as an actual download, not as text in the chat. Do not wrap it
in a code block for me to copy. Do not split it across several messages. If it
is long, still return it as one complete file — every subtitle from the first to
the last, with none summarised, skipped or replaced by a comment such as
"[...continues...]".
"""


def build_prompt(brand: str = "", terms: str = "") -> str:
    """Fill the template. Empty fields keep the visible placeholders."""
    return SRT_PROMPT_TEMPLATE.format(
        brand=brand.strip() or BRAND_PLACEHOLDER,
        terms=terms.strip() or TERMS_PLACEHOLDER,
    )


#: The authoring prompt, for a silent video with no script at all.
#:
#: This exists because "ask ChatGPT to write a script with timings" without
#: rules produces exactly what was measured in the field: 3.1 seconds of words
#: in a 1.7-second window next to seconds of surplus, and single words like
#: "with" given 1.6 seconds of their own. The timing budget below is the whole
#: point of the prompt — an AI follows arithmetic it is given far better than
#: pacing it is left to imagine.
SCRIPT_PROMPT_TEMPLATE = """\
You are my voice-over writer. I am uploading a video that has no narration.
Watch it and write the script a narrator should read over it, with timings, and
give it back as a downloadable .srt file.

The script will be read aloud by a text-to-speech narrator exactly as written,
at the exact times you set — so the timing arithmetic below matters more than
anything else. Timings that ignore it make the narration rush or drag.

==================================================
1. THE TIMING BUDGET  (the most important section)
==================================================

A narrator speaks about 2.4 words per second. For EVERY caption:

- duration_in_seconds must be at least (word_count / 2.4) + 0.4
  A 12-word caption therefore needs at least 5.4 seconds. Check the arithmetic
  for every single caption before you return the file.
- 4 to 16 words per caption. NEVER give one or two words their own caption —
  no caption may be just "with" or "the" or "Link in bio" stretched over
  seconds. Fold small phrases into the caption before or after them.
- A caption is a complete phrase or sentence, never cut mid-phrase.
- Leave a 0.3 to 0.8 second gap between captions for breathing.
- Where the screen should speak for itself, leave a LONGER gap with no caption
  at all. Never stretch a short line over a long stretch of video — three
  words must never own ten seconds.

==================================================
2. WHAT TO WRITE
==================================================

Watch what is actually on screen and narrate it: clear, confident,
conversational, explaining to a peer. Not marketing copy. Short sentences
spoken language handles well.

Always write my brand exactly as:

{brand}

Use these specialist terms correctly where they apply:

{terms}

==================================================
3. VERIFY BEFORE RETURNING
==================================================

For every caption, recompute words ÷ duration: none may exceed 2.8 words per
second. Confirm no caption has fewer than 4 words, captions never overlap, and
gaps exist between them. Fix violations before returning the file — do not
return a file with a violation and a note about it.

==================================================
4. OUTPUT
==================================================

Return the script as a downloadable .srt file — numbered captions,
HH:MM:SS,mmm --> HH:MM:SS,mmm timestamps. One complete file, first caption to
last: not text in the chat, not a code block, never split across messages, and
never summarised with "[...continues...]".
"""


def build_script_prompt(brand: str = "", terms: str = "") -> str:
    """The authoring prompt, filled the same way as the polishing one."""
    return SCRIPT_PROMPT_TEMPLATE.format(
        brand=brand.strip() or BRAND_PLACEHOLDER,
        terms=terms.strip() or TERMS_PLACEHOLDER,
    )


#: The workflow shown on the Home screen, before a file is ever imported.
#:
#: Two routes, because there are two situations, and step 2 is the fork. If your
#: voice is already on the video the app can listen to it and write the script
#: itself; if the video is silent there is nothing to hear, and the words have to
#: be written before anything can be spoken.
WORKFLOW_STEPS: tuple[tuple[str, str, str], ...] = (
    (
        "1",
        "Start with your video",
        "A screen recording is fine. Drop it in above — Narration Studio will "
        "check whether there is any voice on it and take the right route from "
        "there.",
    ),
    (
        "2",
        "If your voice is already on it, you are done here",
        "The app listens to the video, writes down every word with the exact "
        "time it was said, and shows you the result to correct. Nothing is "
        "uploaded; the listening happens on this Mac. The first time takes a "
        "minute longer while it downloads what it needs.",
    ),
    (
        "3",
        "If the video is silent, get the words from ChatGPT",
        "Upload the video to ChatGPT with the script-writing prompt below — it "
        "watches the video and writes the narration with properly paced "
        "timings, about 2.4 words a second with room to breathe, so the voice "
        "neither rushes nor drags. Then drop the .srt it returns in above.",
    ),
    (
        "4",
        "Pick a voice and listen",
        "Choose a voice, generate, play the whole thing back, then export the "
        "audio for your editor. The timings never move.",
    ),
)
