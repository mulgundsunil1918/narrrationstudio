"""The ChatGPT prompts: the rules that were learned from measured failures.

Prompt text is behaviour here — an AI given no timing arithmetic produced 3.1
seconds of words in a 1.7-second window and gave "with" 1.6 seconds of its
own. These tests pin the rules that prevent a regression to vibes.
"""

from __future__ import annotations

import pytest

from app.resources.srt_prompt import (
    BRAND_PLACEHOLDER,
    build_prompt,
    build_script_prompt,
)


def flat(text: str) -> str:
    """The prompt with its line-wrapping removed, so tests survive rewording."""
    return " ".join(text.split())


class TestScriptPrompt:
    def test_carries_the_timing_arithmetic(self):
        """The budget is the entire point of the prompt."""
        prompt = build_script_prompt()
        assert "2.4" in prompt
        assert "word_count / 2.4" in prompt
        assert "2.8 words per" in prompt          # the verification bound

    def test_bans_fragment_captions(self):
        """The measured failure: "with" given 1.6 seconds of its own."""
        prompt = build_script_prompt()
        assert "NEVER give one or two words their own caption" in prompt
        assert '"with"' in prompt

    def test_demands_gaps_rather_than_stretched_captions(self):
        assert "three words must never own ten seconds" in flat(build_script_prompt())

    def test_demands_one_complete_downloadable_file(self):
        prompt = build_script_prompt()
        assert ".srt" in prompt
        assert "not a code block" in prompt
        assert "[...continues...]" in prompt

    def test_fills_brand_and_terms(self):
        prompt = build_script_prompt(brand="PediAid", terms="bilirubin, TSB")
        assert "PediAid" in prompt
        assert "bilirubin, TSB" in prompt
        assert BRAND_PLACEHOLDER not in prompt

    def test_empty_fields_keep_visible_placeholders(self):
        assert BRAND_PLACEHOLDER in build_script_prompt()


class TestPolishPrompt:
    def test_has_a_pacing_check(self):
        prompt = build_prompt()
        assert "PACING CHECK" in prompt
        assert "2.4 words per second" in prompt

    def test_tells_the_ai_to_tighten_wording_not_timestamps(self):
        assert "TIGHTEN THE WORDING" in flat(build_prompt())

    def test_fragments_are_reported_not_silently_mangled(self):
        """The AI may not merge captions, so it must say what it cannot fix."""
        prompt = build_prompt()
        assert "PACING PROBLEMS" in prompt

    def test_timestamps_remain_sacred(self):
        prompt = build_prompt()
        assert "TIMESTAMPS ARE SACRED" in prompt


class TestPromptDialog:
    @pytest.fixture(scope="class")
    def qt_app(self):
        from PySide6.QtWidgets import QApplication

        return QApplication.instance() or QApplication([])

    def test_the_switch_swaps_the_prompt(self, qt_app):
        from app.ui.screens.prompt_dialog import PromptDialog

        dialog = PromptDialog(purpose="write")
        assert "voice-over writer" in dialog._text.toPlainText()

        dialog._purpose.select("polish")
        assert "subtitle editor" in dialog._text.toPlainText()

    def test_each_caller_opens_on_its_own_purpose(self, qt_app):
        from app.ui.screens.prompt_dialog import PromptDialog

        assert PromptDialog(purpose="write")._purpose.current() == "write"
        assert PromptDialog(purpose="polish")._purpose.current() == "polish"

    def test_fields_flow_into_both_prompts(self, qt_app):
        from app.ui.screens.prompt_dialog import PromptDialog

        dialog = PromptDialog(brand="PediAid", purpose="write")
        assert "PediAid" in dialog._text.toPlainText()
        dialog._purpose.select("polish")
        assert "PediAid" in dialog._text.toPlainText()
