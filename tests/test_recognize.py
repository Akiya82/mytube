"""Failing contract for `orchestrator.recognize` (D-151, D-168, D-170): a
release filename becomes a name, a season, an episode number and a
provenance key -- or an honest blank, never a guess.

Every subject is imported inside the test body so this module collects
cleanly while `orchestrator.recognize` does not exist yet.
"""
from unittest.mock import patch

import pytest

from tests.fixtures.recognition_names import KNOWN_LIMITS, REAL_SHARE_NAMES, SHAPE_FIXTURES

_ALL_FIXTURES = REAL_SHARE_NAMES + SHAPE_FIXTURES
_KNOWN_LIMIT_NAMES = {name for name, _reason in KNOWN_LIMITS}


@pytest.mark.parametrize(
    "expectation",
    [e for e in _ALL_FIXTURES if e.filename not in _KNOWN_LIMIT_NAMES],
    ids=lambda e: e.filename,
)
def test_a_fixture_shape_recognises_name_season_and_episode(expectation):
    from orchestrator.recognize import recognize_filename

    result = recognize_filename(expectation.filename)
    assert result.name == expectation.name
    assert result.season == expectation.season
    assert result.episode == expectation.episode


# --- direct tests of the branches, decoupled from anitopy's own quirks ---


def test_roman_numeral_season_split():
    from orchestrator.recognize import normalize_season

    name, season, provenance = normalize_season("Grand Blue Dreaming II", None)
    assert name == "Grand Blue Dreaming"
    assert season == 2
    assert provenance == "detected_numeral"


def test_ordinal_season_split():
    from orchestrator.recognize import normalize_season

    name, season, provenance = normalize_season("Grand Blue Dreaming 3rd Season", None)
    assert name == "Grand Blue Dreaming"
    assert season == 3
    assert provenance == "detected_ordinal"


def test_trailing_small_number_becomes_a_season():
    from orchestrator.recognize import normalize_season

    name, season, provenance = normalize_season("Grand Blue Dreaming 2", None)
    assert name == "Grand Blue Dreaming"
    assert season == 2
    assert provenance == "detected_trailing_digit"


def test_a_four_digit_year_stays_inside_the_name():
    from orchestrator.recognize import normalize_season

    name, season, provenance = normalize_season("Blade Runner 2049", None)
    assert name == "Blade Runner 2049"
    assert season is None
    assert provenance == "none"


def test_a_number_above_twenty_stays_inside_the_name():
    from orchestrator.recognize import normalize_season

    name, season, provenance = normalize_season("Area 88", None)
    assert name == "Area 88"
    assert season is None
    assert provenance == "none"


def test_the_parser_own_season_field_wins_first():
    from orchestrator.recognize import normalize_season

    name, season, provenance = normalize_season("Mushoku Tensei", "3")
    assert name == "Mushoku Tensei"
    assert season == 3
    assert provenance == "detected_tag"


def test_a_named_season_with_no_number_stays_in_the_name():
    from orchestrator.recognize import normalize_season

    name, season, provenance = normalize_season("Naruto Shippuden", None)
    assert name == "Naruto Shippuden"
    assert season is None
    assert provenance == "none"


# --- specials, half numbers, version tags ---


def test_a_special_type_file_returns_no_episode_number_and_no_leaked_tag():
    from orchestrator.recognize import recognize_filename

    result = recognize_filename("Title - OVA1 [1080p].mkv")
    assert result.episode is None
    assert "OVA" not in result.name


def test_a_half_number_returns_no_episode_number():
    from orchestrator.recognize import recognize_filename

    assert recognize_filename("Show - 05.5 [1080p].mkv").episode is None


def test_a_version_tagged_episode_returns_the_plain_integer():
    from orchestrator.recognize import recognize_filename

    assert recognize_filename("Show - 13v2 [1080p].mkv").episode == 13


@pytest.mark.parametrize(
    "filename",
    [
        "Title - OVA1 [1080p].mkv",
        "Show - 05.5 [1080p].mkv",
        "Show - 13v2 [1080p].mkv",
        "Mushoku Tensei S3 - 01 [1080p].mkv",
        "Grand_Blue_Dreaming_II_-_01.mkv",
        "Blade Runner 2049.mkv",
    ],
)
def test_every_branch_reports_its_own_season_provenance_key(filename):
    from orchestrator.recognize import recognize_filename

    result = recognize_filename(filename)
    assert result.season_provenance in {
        "detected_tag",
        "detected_numeral",
        "detected_ordinal",
        "detected_trailing_digit",
        "none",
    }


def test_roman_numeral_round_trip_through_recognize_filename():
    from orchestrator.recognize import recognize_filename

    result = recognize_filename("Grand_Blue_Dreaming_II_-_01.mkv")
    assert result.season == 2
    assert result.episode == 1
    assert "II" not in result.name


# --- the general-purpose fallback: refused when it cannot be trusted ---


def test_fallback_refuses_an_ambiguous_list_of_candidate_episodes():
    from orchestrator.recognize import guessit_fallback

    with patch("orchestrator.recognize.guessit.guessit") as mock_guessit:
        mock_guessit.return_value = {"title": "Area", "episode": [88, 1]}
        assert guessit_fallback("Area 88 - 01 [1080p].mkv") == (None, None, None)


def test_fallback_refuses_a_season_at_or_above_the_guard():
    from orchestrator.recognize import guessit_fallback

    with patch("orchestrator.recognize.guessit.guessit") as mock_guessit:
        mock_guessit.return_value = {"title": "Blade Runner", "season": 20, "episode": 49}
        assert guessit_fallback("Blade Runner 2049.mkv") == (None, None, None)


def test_fallback_accepts_a_plausible_season_and_episode():
    from orchestrator.recognize import guessit_fallback

    with patch("orchestrator.recognize.guessit.guessit") as mock_guessit:
        mock_guessit.return_value = {"title": "Some Show", "season": 2, "episode": 5}
        assert guessit_fallback("Some Show - S02E05.mkv") == ("Some Show", 2, 5)


def test_a_year_shaped_filename_never_reaches_the_operator_as_a_season_and_episode():
    """The end-to-end regression this whole guard exists for: a real
    `guessit` run on a four-digit year hallucinates season=20/episode=49
    (measured live), landing exactly on the season-20 boundary the
    trailing-digit path would otherwise accept -- recognize_filename must
    still come back blank.
    """
    from orchestrator.recognize import recognize_filename

    result = recognize_filename("Blade Runner 2049.mkv")
    assert result.season is None
    assert result.episode is None
    assert "2049" in result.name


def test_recognize_filename_never_raises_for_an_unreadable_name():
    from orchestrator.recognize import recognize_filename

    result = recognize_filename("")
    assert result.raw_name == ""
