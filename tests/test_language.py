"""Failing contract for `orchestrator.language` (D-184, D-185): a code, a
name in any language, or a folder token all resolve to one canonical
language tag or to an honest nothing -- and the four track-kind words
resolve in both interface languages.

Every subject is imported inside the test body so this module collects
cleanly while `orchestrator.language` does not exist yet.
"""


def test_a_two_letter_code_a_three_letter_code_and_a_name_all_agree():
    from orchestrator.language import resolve_language

    tag = resolve_language("ru")
    assert tag == resolve_language("RUS")
    assert tag == resolve_language("rus")
    assert tag == resolve_language("Russian")
    assert tag == resolve_language("русский")


def test_a_japanese_name_in_its_own_script_resolves():
    from orchestrator.language import resolve_language

    assert resolve_language("日本語") is not None
    assert resolve_language("日本語") == resolve_language("Japanese")


def test_garbage_tokens_resolve_to_nothing_never_a_guess():
    from orchestrator.language import resolve_language

    for token in ("Sound", "720p", "S1", "v2", "AniStar", "Дубляжная"):
        assert resolve_language(token) is None, token


def test_resolve_track_kind_covers_both_interface_languages():
    from orchestrator.language import resolve_track_kind

    assert resolve_track_kind("Надписи") == "signs_only"
    assert resolve_track_kind("Озвучка") == "audio"
    assert resolve_track_kind("Subs") == "subtitles"
    assert resolve_track_kind("Полные") == "full"
    assert resolve_track_kind("Sound") == "audio"
    assert resolve_track_kind("Дубляж") == "audio"
    assert resolve_track_kind("Subtitles") == "subtitles"
    assert resolve_track_kind("Субтитры") == "subtitles"
    assert resolve_track_kind("Signs") == "signs_only"
    assert resolve_track_kind("Full") == "full"


def test_resolve_track_kind_is_case_and_whitespace_insensitive():
    from orchestrator.language import resolve_track_kind

    assert resolve_track_kind("  SUBS  ") == "subtitles"


def test_resolve_track_kind_returns_nothing_for_an_unknown_word():
    from orchestrator.language import resolve_track_kind

    assert resolve_track_kind("Bonus") is None


def test_language_from_tokens_returns_the_first_token_that_resolves_and_which_one():
    from orchestrator.language import language_from_tokens

    tag, source = language_from_tokens(["RUS", "Sound", "AniStar"])
    assert tag == "ru"
    assert source == "RUS"


def test_language_from_tokens_skips_a_leading_unresolvable_token():
    from orchestrator.language import language_from_tokens

    tag, source = language_from_tokens(["Sound", "Russian", "AniStar"])
    assert tag == "ru"
    assert source == "Russian"


def test_language_from_tokens_returns_nothing_when_nothing_resolves():
    from orchestrator.language import language_from_tokens

    tag, source = language_from_tokens(["Sound", "720p", "S1"])
    assert tag is None
    assert source is None


def test_language_from_tokens_handles_an_empty_token_list():
    from orchestrator.language import language_from_tokens

    assert language_from_tokens([]) == (None, None)
