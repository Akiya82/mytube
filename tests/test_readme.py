"""Tripwires over `README.md` and its Russian twin.

The three limits a new user trips over first -- the VRChat setting every
viewer must enable, the public-instance restriction, and the absence of
adaptive bitrate -- have to stay on the front page, ahead of the quick
start. Nothing else stops an edit from quietly tidying one of them away.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
README_RU = REPO_ROOT / "README.ru.md"

QUICK_START_HEADING = "## Quick start"


def _english() -> str:
    return README.read_text(encoding="utf-8")


def _above_the_quick_start() -> str:
    text = _english()
    assert QUICK_START_HEADING in text, (
        "README.md has no quick start heading, so 'above the fold' cannot be checked"
    )
    return text.split(QUICK_START_HEADING, 1)[0]


def test_the_untrusted_urls_setting_is_named_before_the_quick_start():
    assert "Allow Untrusted URLs" in _above_the_quick_start(), (
        "every viewer must enable this VRChat setting; a reader who misses it sees only a black screen"
    )


def test_public_instances_are_called_unsupported_with_the_build_number():
    head = _above_the_quick_start()
    assert "group-public" in head and "1546" in head, (
        "the public-instance limit must name group-public instances and the client build that changed it"
    )


def test_the_lack_of_adaptive_bitrate_is_stated_before_the_quick_start():
    head = _above_the_quick_start().lower()
    assert "adaptive bitrate" in head and "separate" in head, (
        "readers expect a quality switch; the README must say qualities are separate videos with separate links"
    )


def test_the_readme_links_the_operator_guide():
    assert "SETUP.md" in _english(), (
        "the landing page links the operator guide instead of restating it"
    )


def test_the_two_languages_link_each_other():
    assert "(README.ru.md)" in _english(), (
        "the English README must link its Russian twin"
    )
    assert "(README.md)" in README_RU.read_text(encoding="utf-8"), (
        "the Russian README must link back to the English one"
    )


def test_each_document_is_in_its_own_language():
    def cyrillic(text: str) -> int:
        return sum("Ѐ" <= ch <= "ӿ" for ch in text)

    other_lines = [
        line for line in _english().splitlines() if "README.ru.md" not in line
    ]
    assert cyrillic("\n".join(other_lines)) == 0, (
        "the English README may carry Cyrillic only on its link to the Russian twin"
    )
    russian = README_RU.read_text(encoding="utf-8")
    assert cyrillic(russian) > len(russian) // 4, (
        "README.ru.md must actually be Russian, not a copy of the English text"
    )
