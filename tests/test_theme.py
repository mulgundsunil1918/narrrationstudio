"""Switching appearance: the palette must follow the choice, not fight it.

The appearance arrives from a QComboBox's itemData, and Qt's round trip
flattens a str-based enum into an ordinary string. The old identity check
("dark" is Appearance.DARK — always False) then applied the light palette
whichever appearance was picked, and the ``.value`` call on the string raised
an error dialog on top of it. These tests hold the boundary: strings and enums
must both mean what they say.
"""

from __future__ import annotations

import pytest

from app.ui import theme
from app.ui.theme import Appearance


@pytest.fixture(autouse=True)
def _restore_palette():
    yield
    theme.set_appearance(Appearance.DARK)


def test_the_enum_selects_the_right_palette():
    assert theme.set_appearance(Appearance.DARK) is theme.DARK
    assert theme.set_appearance(Appearance.LIGHT) is theme.LIGHT


def test_the_flattened_string_selects_the_same_palette():
    """What Qt actually delivers. "dark" must never mean light."""
    assert theme.set_appearance("dark") is theme.DARK
    assert theme.set_appearance("light") is theme.LIGHT


def test_nonsense_is_an_error_not_a_silent_default():
    with pytest.raises(ValueError):
        theme.set_appearance("blurple")


def test_palette_reflects_the_last_choice():
    theme.set_appearance("light")
    assert theme.palette() is theme.LIGHT
    theme.set_appearance("dark")
    assert theme.palette() is theme.DARK


def test_appearance_round_trips_through_settings_storage():
    """The saved value is the enum's own string, and it must come back."""
    for appearance in Appearance:
        assert Appearance(appearance.value) is appearance
