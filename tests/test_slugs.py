import re

import pytest

from orchestrator.slugs import SlugError, normalize_slug, pick_unique_slug, similar_key


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
