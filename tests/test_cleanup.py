import pytest

from app.srt.cleanup import (
    ReplacementRule,
    apply_rules,
    default_rules,
    load_rules,
    preview,
    save_rules,
)


class TestApplyRules:
    def test_replaces_a_simple_word(self):
        rules = [ReplacementRule("Pediate", "PediAid")]
        after, applied = apply_rules("Welcome to Pediate, a platform.", rules)
        assert after == "Welcome to PediAid, a platform."
        assert len(applied) == 1

    def test_is_case_insensitive_by_default(self):
        rules = [ReplacementRule("pediate", "PediAid")]
        after, _ = apply_rules("PEDIATE and Pediate", rules)
        assert after == "PediAid and PediAid"

    def test_case_sensitive_rule_respects_case(self):
        rules = [ReplacementRule("PADI8", "PediAid", case_sensitive=True)]
        after, _ = apply_rules("PADI8 and padi8", rules)
        assert after == "PediAid and padi8"

    def test_whole_word_does_not_match_inside_a_word(self):
        rules = [ReplacementRule("aid", "PediAid")]
        after, applied = apply_rules("first aid and afraid", rules)
        assert after == "first PediAid and afraid"
        assert len(applied) == 1

    def test_multi_word_pattern_matches_as_a_whole_word(self):
        rules = [ReplacementRule("Pedi aid", "PediAid")]
        after, _ = apply_rules("The Pedi aid platform.", rules)
        assert after == "The PediAid platform."

    def test_skips_text_already_equal_to_the_replacement(self):
        # A case-insensitive rule sees "PediAid", but it is already correct.
        rules = [ReplacementRule("pediaid", "PediAid")]
        after, applied = apply_rules("Welcome to PediAid.", rules)
        assert after == "Welcome to PediAid."
        assert applied == []

    def test_applies_multiple_rules_in_order(self):
        rules = [
            ReplacementRule("Pediate", "PediAid"),
            ReplacementRule("neonatology", "neonatology"),
        ]
        after, _ = apply_rules("Pediate for neonatology.", rules)
        assert after == "PediAid for neonatology."

    def test_disabled_rule_is_skipped(self):
        rules = [ReplacementRule("Pediate", "PediAid", enabled=False)]
        after, applied = apply_rules("Pediate here.", rules)
        assert after == "Pediate here."
        assert applied == []

    def test_regex_rule(self):
        rules = [ReplacementRule(r"PADI\d", "PediAid", is_regex=True)]
        after, _ = apply_rules("PADI8 and PADI9.", rules)
        assert after == "PediAid and PediAid."

    def test_invalid_regex_raises_a_friendly_error(self):
        rules = [ReplacementRule("([unclosed", "x", is_regex=True)]
        with pytest.raises(Exception) as info:
            apply_rules("anything", rules)
        assert "not a valid search pattern" in str(info.value)

    def test_records_what_was_found(self):
        rules = [ReplacementRule("PADI8", "PediAid")]
        _, applied = apply_rules("That's PADI8.", rules, segment_index=7)
        assert applied[0].found == "PADI8"
        assert applied[0].segment_index == 7

    def test_leaves_medical_terminology_untouched(self):
        # Only explicitly configured rules ever fire (§4).
        texts = ["Administer 10 mg per kg of paracetamol.", "APGAR score was 9."]
        for text in texts:
            after, applied = apply_rules(text, default_rules())
            assert after == text
            assert applied == []


class TestDefaultRules:
    """No rules ship with the app -- the dictionary belongs to the user."""

    def test_no_rules_are_bundled(self):
        assert default_rules() == []

    def test_nothing_is_changed_without_user_rules(self):
        texts = [
            "Administer 10 mg per kg of paracetamol.",
            "Welcome to Acme, a platform for teams.",
            "The APGAR score was 9.",
        ]
        for text in texts:
            after, applied = apply_rules(text, default_rules())
            assert after == text
            assert applied == []

    @pytest.mark.parametrize(
        "before,after",
        [
            ("Welcome to Acmee, a platform.", "Welcome to Acme, a platform."),
            ("ACMEE is designed for teams.", "Acme is designed for teams."),
            ("and that's ACM3. A single place", "and that's Acme. A single place"),
            ("The Ac me app.", "The Acme app."),
        ],
    )
    def test_user_defined_rules_fix_transcription_errors(self, before, after):
        """The engine handles whatever terms the user configures, for any domain."""
        rules = [
            ReplacementRule(pattern, "Acme")
            for pattern in ("Acmee", "ACMEE", "ACM3", "Ac me")
        ]
        result, _ = apply_rules(before, rules)
        assert result == after

    def test_correct_spelling_is_a_no_op(self):
        rules = [ReplacementRule("Acmee", "Acme")]
        text = "Acme brings together the tools you need."
        result, applied = apply_rules(text, rules)
        assert result == text
        assert applied == []


BRAND = [ReplacementRule("Acmee", "Acme"), ReplacementRule("ACM3", "Acme")]


class TestPreview:
    def test_reports_only_changed_segments(self):
        texts = ["Acmee one.", "Nothing to change.", "ACM3 two."]
        result = preview(texts, BRAND)
        assert result.segment_count == 2
        assert result.replacement_count == 2
        assert [p.segment_index for p in result.changed_previews] == [0, 2]

    def test_text_map_targets_the_right_rows(self):
        texts = ["Acmee one.", "Nothing.", "ACM3 two."]
        assert preview(texts, BRAND).as_text_map() == {
            0: "Acme one.",
            2: "Acme two.",
        }

    def test_counts_by_rule(self):
        texts = ["Acmee.", "Acmee again.", "ACM3."]
        counts = preview(texts, BRAND).counts_by_rule()
        assert counts["Acmee → Acme"] == 2
        assert counts["ACM3 → Acme"] == 1

    def test_preview_does_not_mutate_input(self):
        texts = ["Acmee one."]
        preview(texts, BRAND)
        assert texts == ["Acmee one."]


class TestPersistence:
    def test_round_trips(self, tmp_path):
        path = tmp_path / "rules.json"
        rules = [ReplacementRule("Foo", "Bar", note="test")]
        save_rules(path, rules)
        loaded = load_rules(path)
        assert loaded[0].pattern == "Foo"
        assert loaded[0].replacement == "Bar"
        assert loaded[0].note == "test"

    def test_missing_file_returns_the_empty_default(self, tmp_path):
        assert load_rules(tmp_path / "absent.json") == []

    def test_ignores_unknown_keys(self, tmp_path):
        path = tmp_path / "rules.json"
        path.write_text('{"rules":[{"pattern":"A","replacement":"B","bogus":1}]}')
        assert load_rules(path)[0].pattern == "A"

    def test_corrupt_file_raises_friendly_error(self, tmp_path):
        path = tmp_path / "rules.json"
        path.write_text("{not json")
        with pytest.raises(Exception) as info:
            load_rules(path)
        assert "could not be read" in str(info.value)
