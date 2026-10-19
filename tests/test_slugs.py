import re

import pytest

from orchestrator.slugs import (
    AddressRefused,
    EditionSlugRefused,
    PublicAddress,
    SlugError,
    normalize_edition_slug,
    normalize_slug,
    parse_public_address,
    pick_unique_slug,
    similar_key,
)


def test_transliterates_cyrillic_title():
    assert normalize_slug("Атака Титанов") == "ataka-titanov"


def test_transliterates_cyrillic_title_with_genitive_case():
    assert normalize_slug("Пираты Карибского моря") == "piraty-karibskogo-moria"


def test_empty_after_transliteration_raises_slug_error():
    with pytest.raises(SlugError):
        normalize_slug("🎬🎥📼")


def test_punctuation_only_raises_slug_error():
    with pytest.raises(SlugError):
        normalize_slug("???")


def test_result_matches_slug_shape_and_length():
    slug = normalize_slug("Атака Титанов: Финал — Часть 2")
    assert re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", slug)
    assert len(slug) <= 80


def test_re_zero_and_rezero_are_different_slugs():
    assert normalize_slug("Re:Zero") == "re-zero"
    assert normalize_slug("ReZero") == "rezero"


def test_re_zero_and_rezero_share_a_similar_key():
    re_zero = normalize_slug("Re:Zero")
    rezero = normalize_slug("ReZero")
    assert re_zero != rezero
    assert similar_key(re_zero) == similar_key(rezero)


def test_pick_unique_slug_returns_candidate_when_free():
    slug, similar = pick_unique_slug("ataka-titanov", taken=set())
    assert slug == "ataka-titanov"
    assert similar == []


def test_pick_unique_slug_suffixes_deterministically_on_collision():
    taken = {"ataka-titanov"}
    slug, _ = pick_unique_slug("ataka-titanov", taken)
    assert slug == "ataka-titanov-2"

    taken.add("ataka-titanov-2")
    slug, _ = pick_unique_slug("ataka-titanov", taken)
    assert slug == "ataka-titanov-3"


def test_pick_unique_slug_is_deterministic_for_the_same_input():
    taken = {"foo", "foo-2"}
    assert pick_unique_slug("foo", taken) == pick_unique_slug("foo", taken)


def test_pick_unique_slug_reports_similar_existing_slugs_rezero_case():
    taken = {"rezero"}
    slug, similar = pick_unique_slug("re-zero", taken)
    assert slug == "re-zero"
    assert similar == ["rezero"]


def test_pick_unique_slug_no_similar_when_none_match():
    slug, similar = pick_unique_slug("brand-new", {"something-else"})
    assert slug == "brand-new"
    assert similar == []


def test_normalize_edition_slug_passes_a_normal_name_through():
    assert normalize_edition_slug("Dub RU") == "dub-ru"


def test_normalize_edition_slug_refuses_an_all_digit_slug():
    with pytest.raises(EditionSlugRefused):
        normalize_edition_slug("2024")


def test_normalize_edition_slug_refuses_a_slug_that_normalises_to_digits_only():
    with pytest.raises(EditionSlugRefused):
        normalize_edition_slug("  007  ")


def test_normalize_edition_slug_refuses_an_empty_result():
    with pytest.raises(SlugError):
        normalize_edition_slug("???")


# --- parse_public_address ---------------------------------------------------


def test_parse_public_address_bare_title():
    assert parse_public_address("horimiya") == PublicAddress(slug="horimiya")


def test_parse_public_address_title_and_number():
    assert parse_public_address("horimiya/5") == PublicAddress(slug="horimiya", number=5)


def test_parse_public_address_title_and_edition_slug():
    assert parse_public_address("some-film/directors-cut") == PublicAddress(
        slug="some-film", edition_slug="directors-cut"
    )


def test_parse_public_address_title_number_and_edition_slug():
    assert parse_public_address("horimiya/5/dub-ru") == PublicAddress(
        slug="horimiya", number=5, edition_slug="dub-ru"
    )


def test_parse_public_address_every_leading_zero_form_gives_the_same_result():
    assert (
        parse_public_address("horimiya/05")
        == parse_public_address("horimiya/5")
        == parse_public_address("/v/horimiya/005")
    )


def test_parse_public_address_accepts_a_pasted_address_with_the_leading_marker():
    assert parse_public_address("/v/horimiya/5") == parse_public_address("horimiya/5")


def test_parse_public_address_refuses_a_four_segment_address():
    with pytest.raises(AddressRefused):
        parse_public_address("a/b/c/d")


def test_parse_public_address_refuses_an_empty_segment():
    with pytest.raises(AddressRefused):
        parse_public_address("a//b")


def test_parse_public_address_refuses_an_all_digit_edition_slug():
    with pytest.raises(AddressRefused):
        parse_public_address("a/1/2")


def test_parse_public_address_refuses_an_absurdly_long_numeric_segment():
    with pytest.raises(AddressRefused):
        parse_public_address(f"horimiya/{'9' * 40}")


def test_parse_public_address_refuses_an_empty_address():
    with pytest.raises(AddressRefused):
        parse_public_address("")


def test_parse_public_address_refuses_a_non_slug_shaped_edition_segment():
    with pytest.raises(AddressRefused):
        parse_public_address("horimiya/Not A Slug!")
