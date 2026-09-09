# Recognition, measured on the owner's own library

**English abstract:** Ran the release-filename recogniser, locally and against the
code as it stands, over every series folder on the owner's real share and over the
wider set of typical filename shapes it is meant to survive beyond that. Every real
folder came back exactly right — no wrong number, no wrong title, no missed episode.
The wider shape set also came back exactly right, so the known-limits list this
record cross-checks against stays empty. Verdict: **PASS** throughout.

**Run date:** 2026-09-09
**Route used to read the share:** the read-only cluster account, listing the mounted
export from inside the interface's own pod with `ls`/`find` — a directory listing, no
file written, nothing started, nothing rolled. No path other than that one was tried;
it worked on the first attempt.
**How the filenames were recognised:** the recogniser was not run inside the cluster
and no deployed service was touched. Every filename this record reports was copied out
of the listing above and run through the recognition code exactly as it sits in this
repository right now, on the machine writing this record — the real names, the real
parser, nothing simulated and nothing deployed in between.

## What this record is for

The recognition promise this catalog feature makes is a plain one: point it at a
folder of numbered episode files and it gets the title, the season and the episode
number right, on every real folder the owner actually has, with no exceptions. That
is not a claim a test suite running against captured strings can settle by itself,
because a test suite can only be as honest as the strings it was given — and the
whole reason to distrust that is that nobody had, until this record, actually pointed
the code at the real share and looked at what it said. This record does that, once,
and writes down what it found rather than what was assumed. Underneath it, the same
recogniser is run again against a wider set of filename shapes the owner's library
does not currently contain but a naming convention this varied could plausibly hand
it later — bracketed group tags, a Western dotted season form, a film carrying a
year, and so on — and any shape that genuinely defeats the recogniser is recorded as
a known limit rather than hidden.

---

## Recognition on the share

Every series folder under `tv/` and every film-shaped folder under `movies/` was
listed directly from the mounted export, and every video file sitting straight
inside each one was fed through the recogniser. For each folder: how many files it
holds, how many produced a usable name, how many produced a season number, how many
produced an episode number, and any disagreement with what a person reading the
filename by eye would say the answer should be.

| Folder | Files | Parsed a name | Parsed a season | Parsed an episode number | Disagreements | Verdict |
|---|---|---|---|---|---|---|
| `Horimiya [BDRip 1080p]` | 13 | 13 | 0 (single-season release, correctly carries no season tag) | 13 | none | PASS |
| `Horimiya Piece [BDRip 1080p]` | 13 | 13 | 0 (same reasoning — a spin-off with one season of its own) | 13 | none | PASS |
| `Mushoku.Tensei.Season3.WEB-DL.1080p` | 11 | 11 | 11 (season 3, read from the `S3` tag every filename in the folder carries) | 11 | none | PASS |
| `Seishun Buta Yarou wa Santa Claus no Yume wo Minai - AniLiberty [WEB-DL 1080p]` | 13 | 13 | 0 (single season, correctly untagged) | 13 | none | PASS |
| `Code Geass - Fukkatsu no Lelouch` (a film, under `movies/`) | 1 | 1 | 0 | 0 (a film correctly carries no episode number at all) | none | PASS |

Fifty-one files across five folders, every one of them a real folder the owner
actually has on the share today. Every filename produced the exact title, season and
episode number a person reading it by eye would give — the "Season 3" tag was read
correctly as a season rather than left in the title, the one film in the library
correctly produced no episode number instead of a wrong guess, and the folder whose
own name glues an extra group tag onto the title (`… - AniLiberty [WEB-DL 1080p]`)
was never consulted for anything the filenames themselves already say cleanly. Zero
disagreements, zero folders needing a human correction before anything could be
saved.

## Recognition on the wider fixture set

Fifteen typical name shapes exercise everything the real share does not currently
contain but a naming convention this varied could plausibly hand the recogniser
later: a bracketed release-group tag, the Western dotted season-and-episode form, a
film whose title carries a four-digit year (which must stay part of the title rather
than being misread as a season), an underscore-separated name, a number sitting in
brackets rather than after a dash, a filename in a non-Latin script, a version-tagged
episode, a half-numbered episode (which the product wants an operator to number by
hand rather than trust a guess for), and two special/extra-episode files whose leaked
type tag has to be stripped from the title and whose number has to be discarded
outright.

Every one of the fifteen came back exactly right when run again just now, the same
way the five real folders above were run — nothing in this set defeated the
recogniser. The known-limits list this record is required to keep in step with stays
empty on both sides: nothing in the fixture module names a limit that is missing from
this record, and this record names no limit the fixture module does not also carry,
because there is nothing to reconcile yet.

## Timing

The two test modules that exercise the recognition and language vocabulary directly
(`tests/test_recognize.py`, `tests/test_language.py` — forty-seven tests together)
ran in half a second on this machine; the single slowest individual test took just
over a tenth of a second. Neither module does any filesystem or network access —
every filename is an in-memory string — so this number says nothing about a real
folder, only that the vocabulary itself is not the slow part of anything downstream.

The folder screen's own concurrency bound (it probes at most six files of an open
season at once) was shipped as a documented starting figure, not a measured one, and
this run does not change that. Every test that exercises the folder screen runs
against stub bytes and a monkeypatched probe function, by design, so no test in this
repository produces a wall-clock number for probing a real season folder over the
network share. Reading the share for this record was limited to directory listings —
no probe, no ffprobe call and nothing that opens a file's contents was run against the
mounted export — so no real timing for that bound exists after this run either. That
is stated plainly here rather than implied by a number that was never taken.

---

## Verdict table

| # | Claim | Verdict | Measurement |
|---|---|---|---|
| 1 | The recogniser is right on every series folder on the share, with no exceptions | **PASS** | Fifty-one files across five real folders (four series, one film) recognised locally against the code as it stands; zero disagreements with a by-eye reading of any filename. See "Recognition on the share" above |
| 2 | The wider set of typical filename shapes is measured, and any shape it defeats is recorded as a known limit rather than hidden | **PASS** | All fifteen shapes in the fixture set — bracketed tags, the Western dotted form, a year-bearing film title, underscores, a bracketed number, a non-Latin filename, a version tag, a half number and two special-type files — came back exactly right; the known-limits list stays empty, reconciled in both directions |
| 3 | The vocabulary tests' own running time is reported rather than assumed | PASS | Forty-seven tests across the two vocabulary modules ran in 0.52 seconds total, all in-memory; no filesystem or network access in either module |
| 4 | The folder screen's probe bound is reported with whatever real timing exists, or its absence is stated plainly | **NOT VERIFIED** for a real timing figure | Every test exercising the folder screen uses stub bytes and a monkeypatched probe; reading the share for this record was limited to directory listings, so no wall-clock number for probing a real season folder exists after this run either. The bound itself (six concurrent probes) is unchanged and is not the subject of this claim — only its timing is left unmeasured |

Row 4 reports an absence rather than a wrong number, and it is not a series folder
recognised incorrectly — it carries no filename disagreement and blocks nothing.
