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

---

## Deployment

With the recognition measurement above clean, the owner approved the four new
packages (`anitopy`, `guessit`, `langcodes`, `language_data` — see
`00-package-legitimacy.md`) and then approved a two-part deployment, converter
first and interface second, because the two now speak a raised protocol between
them and an old converter left running against a new interface exits at its
first claim rather than working quietly.

**Commit deployed:** `5d27962`.

**Images built and pushed:**

- `registry.example.com/mytube/mytube:5d27962`, digest
  `sha256:f66b1af73ae639aac27582c4716ae76a96a596567416903c184caf3328b69853`
- `registry.example.com/mytube/mytube-worker:5d27962`, digest
  `sha256:2c772a82533c1d6ae30d0b2ee4e0c40eaefc2b64a34f51a4494570fb286ae2b1`

**Before rolling anything**, the job queue was read back: 25 done, 1 failed,
nothing queued and nothing running — the converter was genuinely idle.

**The converter rolled first**, by the same ansible role the project's earlier
conversion-worker rollout used, pinning the new tag on the GPU host and
bringing the compose stack back up. Its very first polls afterward, read
straight from its own log, show exactly the disagreement the raised protocol
predicts:

```
INFO:__main__:worker 'ml-server' starting: service https://admin.mytube.example.com, card 6304 MiB free
INFO:httpx:HTTP Request: POST https://admin.mytube.example.com/worker/claim "HTTP/1.1 400 Bad Request"
ERROR:__main__:this converter speaks protocol 3, the service speaks protocol 2 -- deploy the converter and the service together
```

That is expected and temporary — it clears the moment the other half rolls, not
a defect in the converter.

**The interface rolled second**, together with both edge ConfigMaps (the
template carrying the one new `error_page` line, and the error clips carrying
the new fifth stub) regenerated from this same commit and applied first. The
rolled deployment's own containers read back as
`registry.example.com/mytube/mytube:5d27962` (interface) and
`nginx:1.27-alpine` (edge, unchanged), and the container's own start-up ran
the pending migration automatically. The schema revision read back afterward
is `0007` — the two migrations this phase queued (per-edition readiness and
the edition-scoped delete countdown) both applied. The edge container's error
directory lists all five stub clips, `preparing.mp4` among them.

**Read back once more, the converter's log now shows a clean poll on the
matching protocol**, with no disagreement line at all:

```
INFO:httpx:HTTP Request: POST https://admin.mytube.example.com/worker/claim "HTTP/1.1 204 No Content"
```

and the running container is confirmed on the new tag.

**After the migration's backfill**, an existing title's numbered address was
checked against its bare address to confirm the backfill actually reached
every pre-existing row. `horimiya-01`, an episode already in the catalog
before this phase, answers identically at its bare address and at every
numbered form of it:

| Address | Status | Redirects to |
|---|---|---|
| `/v/horimiya-01` | 302 | the same signed blob address |
| `/v/horimiya-01/01` | 302 | the same signed blob address |
| `/v/horimiya-01/1` | 302 | the same signed blob address |
| `/v/horimiya-01/001` | 302 | the same signed blob address |

All four forms resolve to the exact same object, confirming the migration's
backfill reached this title without anyone touching it by hand.

Both public hostnames answered after the roll: the operator's own interface
with a login redirect, and the delivery hostname with a real video response.

### Layout fix rolled during the acceptance walk

While walking the rendered surfaces after the rollout above, the owner found the
library page pinned to 960px on a 1920x1080 monitor, with each row's text column
crushed to 77px beside a 641px action cluster. The stylesheet fix (commit `ae76f19`:
the page spans the viewport, a row's text column grows and never shrinks below
320px, the action cluster wraps under it when the row is narrow, status chips keep
their own width) was verified on the live page with an injected style before it
shipped, then built as `registry.example.com/mytube/mytube:ae76f19` (digest
`sha256:dfd80162c1fc2462688c838892ae38df3040c1d5db3e882c277e8d36ac372c96`) and
rolled on 2026-09-09 as an interface-only change; the converter stayed on `5d27962`
because nothing in its code or in the shared media package changed between the two
commits. Read back after the roll: pod `mytube-5c46f75cb4-s47tf` 2/2, schema
revision `0007`, login page 200. Measured on the live library afterwards: the page
width equals the viewport at 1024, 1920 and 3440px; the text column of a row with
four actions measures 798, 1209 and 2729px respectively; at 1024px the action
cluster sits on a line of its own; no horizontal scroll at any width.

Refined the same day: the full-width page ran the row actions to the far edge of a
wide monitor, a walk away from the text they act on. Commit `0978aef` sizes the page
to its content instead: as wide as its widest line needs, never narrower than 960px
and never wider than the viewport, centered, and the sign-in card keeps an explicit
400px. Rolled as `registry.example.com/mytube/mytube:0978aef` (index digest
`sha256:c8e303a7b62d96bb12b5d8e38c50b0cd67d5c4749616f3a7cb1c124e68982824`), again
interface only with the converter still on `5d27962`; pod `mytube-68ccdd6f6c-lx7mt`
2/2, schema revision `0007`, login page 200. Measured on the live library afterwards,
in the Russian locale: the page is 1781px wide with 69px on each side at 1920px and
829px on each side at 3440px, and fills the viewport at 1024px; a row with four
actions keeps an 899px text column beside a 641px action cluster; no horizontal
scroll at any width.
