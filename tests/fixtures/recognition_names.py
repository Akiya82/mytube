"""Plain data: what "recognition is right" means for this library, stated
as filename -> (name, season, episode) triples. No import beyond typing --
this module is read by the tests and by anyone auditing what the
recogniser promises, without needing to run anything.

`REAL_SHARE_NAMES` are the blocking set: five filename shapes measured on
the owner's own share, every one of which must pass. `SHAPE_FIXTURES` are
the typical name shapes a release-naming corpus is expected to contain --
bracketed group tags, a Western dotted season/episode, a film carrying a
year, underscores, a number in brackets, a non-Latin filename, a
version-tagged episode, a half-numbered episode and two special-type
files. `KNOWN_LIMITS` records a shape the recogniser genuinely cannot get
right, with the plain-English reason why -- empty until a real attempt
proves one belongs here.
"""
from typing import NamedTuple


class Expectation(NamedTuple):
    filename: str
    name: str
    season: int | None
    episode: int | None


# Measured on the owner's own share (read-only NFS mount) -- every one of
# these must resolve exactly right, no exceptions.
REAL_SHARE_NAMES: list[Expectation] = [
    Expectation(
        "Horimiya - 01 [BDRip 1080p HEVC 10bits FLAC].mkv", "Horimiya", None, 1
    ),
    Expectation(
        "Horimiya Piece - 02 [BDRip 1080p HEVC 10bits FLAC].mkv",
        "Horimiya Piece",
        None,
        2,
    ),
    Expectation("Mushoku Tensei S3 - 01 [1080p].mkv", "Mushoku Tensei", 3, 1),
    Expectation(
        "Seishun_Buta_Yarou_wa_Santa_Claus_no_Yume_wo_Minai_[01].mkv",
        "Seishun Buta Yarou wa Santa Claus no Yume wo Minai",
        None,
        1,
    ),
    # A film: no episode number is the correct answer, not a failure to parse.
    Expectation(
        "Code Geass - Fukkatsu no Lelouch [BDRip 1080p].mkv",
        "Code Geass - Fukkatsu no Lelouch",
        None,
        None,
    ),
]

# Typical name shapes, not necessarily from the owner's own share -- what
# the wrapper has to handle for a library this narrow but a naming
# convention this varied.
SHAPE_FIXTURES: list[Expectation] = [
    # Bracketed group-and-tags anime form.
    Expectation("[Group] Title - 01 [tags].mkv", "Title", None, 1),
    # Western dotted season-and-episode form.
    Expectation("Title.S01E05.mkv", "Title", 1, 5),
    # A film carrying a four-digit year -- must stay in the name, not split
    # into a season and an episode.
    Expectation("Blade Runner 2049.mkv", "Blade Runner 2049", None, None),
    # Underscore-separated name.
    Expectation("Some_Underscore_Title_-_07.mkv", "Some Underscore Title", None, 7),
    # The number in brackets rather than after a dash.
    Expectation("Title [07].mkv", "Title", None, 7),
    # A Cyrillic filename -- anitopy does no charset-specific handling.
    Expectation("Хоримия - 05 [1080p].mkv", "Хоримия", None, 5),
    # A version-tagged episode: the version is a separate field, the
    # episode number is the plain integer.
    Expectation("Show - 13v2 [1080p].mkv", "Show", None, 13),
    # A half-numbered episode -- the operator assigns the number by hand.
    Expectation("Show - 05.5 [1080p].mkv", "Show", None, None),
    # Two special-type files -- a type tag leaks into anitopy's own title
    # field and must be stripped; the number it also returns is discarded.
    Expectation("Title - OVA1 [1080p].mkv", "Title", None, None),
    Expectation("Title - SP2 [1080p].mkv", "Title", None, None),
]

# A shape the recogniser genuinely cannot get right, with the reason why.
# Empty until a real attempt (not a guess) proves one belongs here -- see
# the summary for what task 2 found.
KNOWN_LIMITS: list[tuple[str, str]] = []
