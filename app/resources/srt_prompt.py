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
        "Upload the video to ChatGPT and ask it to write a voice-over script "
        "with timings, as a .srt file — it will watch the video and tell you "
        "what to say and when. Paste the prompt below at the same time and it "
        "will also tidy the wording so it reads naturally aloud. Then drop that "
        "file in above.",
    ),
    (
        "4",
        "Pick a voice and listen",
        "Choose a voice, generate, play the whole thing back, then export the "
        "audio for your editor. The timings never move.",
    ),
)
