# Experiment 11: conversion on the GPU host — measured acceptance

**Correction applied after deployment (2026-09-05):** the report protocol widened
and both halves were rolled to the same commit — see "The gap, closed" under
Claim 4 and the corrected recovery-time figure in the same section before
reading the verdict table below.

**Run date:** 2026-09-04 (all times UTC)
**Service:** k3s namespace `mytube`, api image `registry.example.com/mytube/mytube:930634d`
**Converter host:** `ml-server`, Ubuntu 26.04, AMD FX-8320E (no AVX2), NVIDIA RTX 3050
8 GiB shared with three other GPU services (ollama, whisper, reranker), driver 595.84
**Converter:** container `mytube-worker` from `registry.example.com/mytube/mytube-worker:930634d`,
ffmpeg `n8.1.2` (BtbN LGPL, tag `autobuild-2026-08-31-13-27`, sha256 `7d6d93e9…31bbee`)
**Sources:** `10.0.120.19:/mnt/HDD/k3s/qbittorrent/completed`, mounted read-only at
`/mnt/mytube-share` on the host and `/share` inside both programs
**Test suite at the time of writing:** 641 passed, 115 deselected (`pytest -m "not integration"`)

## What this record is for

The conversion pipeline makes five claims, and each one is an acceptance criterion for
the work that built it. Four can be argued from code and tests; none can be *settled*
that way, because the central one is a duration on hardware the test suite has never
seen, and the one next to it is a statement about a second machine no test can observe.
This document records what was measured, how, and what the measurement did not reach.
Where a claim was not measured it says so instead of reasoning its way to a pass — the
same discipline experiment 2/5 used for its own unverified half, and experiment 10 used
when the evidence changed the answer.

### The five claims

| # | Claim |
|---|-------|
| 1 | The analyzer parses `ffprobe` JSON, produces a plan (remux or convert) and explains why that path was chosen; when no suitable audio track exists the operator picks a subtitle track to burn in, with its language and title shown |
| 2 | A single-frame preview with the subtitles burned in renders in seconds, before the full conversion starts |
| 3 | The conversion is queued in Postgres (`SELECT … FOR UPDATE SKIP LOCKED`, short claim transaction) and runs on a separate machine with NVENC — a new video is ready in under five minutes and the workstation stays free the whole time |
| 4 | The operator sees the job's status, and the reason when it failed; a job stuck running after the converter dies returns to the queue by itself on the heartbeat |
| 5 | The 3D layout (side-by-side, over-under, flat) is detected, stored on the edition and not broken by scaling (SAR/DAR preserved); the operator is told which stereo mode to switch the screen to |

### How the runs were driven, and what that costs the record

Every conversion below was enqueued from inside the api container, through the same code
the add form's convert branch calls: `probe` → `analyze` → `plan_slug` →
`create_title_with_edition` → `enqueue_job` with `subtitle_kind="embedded"`. The form
itself needs a browser session, and this run had no browser. Everything that only exists
as rendered HTML — the plan sentence as the form words it, the subtitle list, the stereo
selector's guess, the time estimate as displayed, the live row transitions, the stop and
retry buttons — is therefore **not verified here** and is listed as such at the end. What
was measured is the pipeline underneath, end to end, on the real card, plus the delivered
file itself.

---

## Claim 3, the central one: the five-minute promise

### The reference source (the harder of the two common cases)

`/share/tv/Horimiya [BDRip 1080p]/Horimiya - 01 [BDRip 1080p HEVC 10bits FLAC].mkv`
— 23.75 min (1425.13 s), HEVC Main 10 `yuv420p10le` 1920×1080 at 4.98 Mbit/s, audio
track 1 AAC stereo rus "Wakanim" (what the analyzer defaulted to), ASS track 6
"Полные (YakuSub Studio)" burned in, 21 attached fonts collected from the container.

The analyzer's verdict on it: plan `transcode`, reason `reason.codec` with
`{'codec': 'hevc'}`, stereo layout `2d` from no source signal, not over the bitrate
ceiling. That is the correct call — a ten-bit HEVC stream is exactly what the target
player cannot open.

| Run | Job | Queue wait | Start → finish | Realtime factor | Result |
|---|---|---|---|---|---|
| 1st, before the region fix | `04aea39b` | 5.6 s | 203.6 s to `uploading`, then `failed.reason.upload` | 7.09 | encode fine, upload refused with `SignatureDoesNotMatch` |
| 2nd, through the retry path | `136b622a` | 1.0 s | **249.2 s** (encode ≈210 s, upload ≈40 s of 1.07 GB) | 7.06 | `done`, title `active` |

The first run is kept here on purpose: it is the run that found the region defect, and it
also shows the encode itself was never the problem. The measured number for the reference
source is **249.2 s — 4 min 9 s, inside the promise**.

Three more reference-shaped episodes ran during the break-it scenarios and are reported
here as extra data points, not as separate claims: Horimiya 03 at 275.3 s (factor 6.96),
Horimiya 04 at 275.4 s (7.00), Horimiya 05 at 246.1 s (7.11) on its second attempt after
the kill. **Every run finished under 300 s.**

### The second source, a different shape

`/share/tv/Grand_Blue_Dreaming_II/Grand_Blue_Dreaming_II_-_01.mkv` — 24.0 min
(1440.03 s), H.264 High 8-bit 1920×1080 at 38.0 Mbit/s, audio track 1 AC3 stereo rus
(re-encoded to AAC), ASS track 7 "Полные" burned in. The analyzer planned `passthrough`
with an over-the-ceiling warning; convert was chosen anyway, because the point of the run
was the burn-in. The estimate the service produced from history was 204 s, from a median
realtime factor of 7.06.

| Job | Queue wait | Start → finish | Realtime factor | Result |
|---|---|---|---|---|
| `5f742278` | 3.0 s | **275.9 s** | 7.24 | `done`, delivered h264 High 4.1 `yuv420p`, AAC, 1 108 548 981 bytes, 6.16 Mbit/s, faststart holds |

Two sources, two shapes, factors 7.06 and 7.24. The duration does scale with length at a
roughly constant factor, which is what makes the estimate from history usable — 204 s
predicted against 275.9 s measured, low by a quarter because the estimate is built from
the reference source's factor and this source carried a heavier decode.

### What the promise actually rests on

At a factor of about 7, the five-minute ceiling holds for anything up to roughly 35
minutes of source. A feature-length film is **not** covered by these measurements and
would land near 15 minutes at this factor on this card. That is not a failure of the
claim as written — the claim is about an episode — but nobody should read this record as
saying any source converts in five minutes.

### The delivered file

`/v/horimiya-01` → `302` → `/blob/15195f4f-….mp4?exp=…&sig=…`; the edge answers `206
Partial Content` to a one-byte range with `X-Cache-Status: MISS` on first touch. The
object itself: h264 High level 4.1 `yuv420p` 1920×1080, AAC LC 48 kHz stereo, duration
1425.13 s (identical to the source), 1 066 411 876 bytes, 5.99 Mbit/s, `ftyp` at byte 4
and `moov` at byte 36 — faststart holds, which is the one property that decides whether
the player shows a frame before downloading a gigabyte.

One surprise in the output: a third stream, index 2, `bin_data` with codec tag `text` and
handler `SubtitleHandler`. It is the source's own chapter list (three chapters) written
out by the MP4 muxer as a chapter text track. The as-is clips never carried one because
they had no chapters. Whether the target player tolerates it is unknown and is listed as
an open item; `-map_chapters -1` is the switch if it does not.

### The card, and the workstation

`nvidia-smi dmon -s u -d 5` on the converter host across the runs, 77 five-second
samples: peak sm 68 %, encoder 100 %, decoder 70 %; mean while encoding sm 49 %, encoder
80 %, decoder 31 %. Free VRAM reported by the converter at claim time: 5225 MiB of 8192,
the other three services holding roughly 2.6 GiB.

The workstation during a run: its own card (RTX 5070 Ti) at encoder 0 %, GPU 3 %. Nothing
of the workstation participates in a conversion. The service pod only renders the preview
and serves the queue; the encode is entirely on the other machine, and the operator's
machine can be switched off mid-conversion without touching it.

**Verdict on claim 3: PASSED.** 249.2 s on the named reference source, 275.9 s on a
source of a different shape, five runs all under 300 s, and the workstation measurably
idle throughout.

---

## Claim 2, and claim 1's honest half: the preview and the plan

The preview frame was rendered on the service pod (CPU, the static ffmpeg the api image
carries, release line 9.0) through `build_preview_command`, at the first subtitle cue
after 600 s — 602.28 s. A second was rendered for the Grand Blue source at 400 s.

**The preview's own render time was not timed on this run.** The claim says "in seconds";
the frames were produced without a perceptible wait, but "without a perceptible wait" is
not a measurement and this record will not write it down as one. What was measured is
what the preview is *for*, below.

### Frame parity: does the preview tell the truth?

This is the check nothing automated in this project can do, and the reason the preview
feature is worth anything at all. The preview renders on the service, on a CPU, with one
ffmpeg build and one font set. The conversion renders on the other machine, on the card,
with a different ffmpeg build and a different font set. Both commands come out of the
same builder — and the tests compare the *commands*. Only a picture-to-picture comparison
compares the results.

Preview frame at 602.28 s against the delivered file seeked to the same timestamp:

- **Text:** identical — "от тех яиц!" in both. No mojibake in either, so the subtitle
  file's character encoding survived both paths intact.
- **Font:** identical italic face, drawn from the fonts attached to the source container
  in both cases, not a substitute.
- **Position and size:** identical.
- **Legibility:** legible in both.

Grand Blue at 400 s: a two-line cue, burned and legible.

Frames kept as `preview-602.jpg`, `output-602.jpg`, `grandblue-400.jpg` outside this
repository.

**Verdict on claim 2: PASSED on the property that matters, NOT MEASURED on its stated
duration.** The preview is honest — same text, same font, same position, same encoding
across two ffmpeg builds on two machines. Its render time was not put on a clock, and no
number is invented here. The on-demand preview *as driven from the form* was not
exercised; the frame came from the same builder, called directly.

---

## Claim 1: the plan and the subtitle choice

Measured through the pipeline, not through the form. The analyzer read both sources'
`ffprobe` output, produced `transcode` for the ten-bit HEVC episode with the reason
naming the codec, and `passthrough` with an over-the-ceiling warning for the 38 Mbit/s
H.264 one. The subtitle track was enumerated from the container, chosen by kind, resolved
against a fresh probe on the converter, and burned. Both sources carried an ASS track
with a language and a title, and both burned the intended one.

**Verdict on claim 1: PASSED for the analysis and the burn-in; NOT VERIFIED HERE for its
presentation.** The plan sentence as the form words it, the explanation shown to the
operator, the subtitle list as rendered and the stereo selector's guess were never seen
in a browser during this run.

---

## Claim 5: stereo layout

Both sources are flat. The analyzer detected `2d` from no source signal on each, stored
it on the edition (`stereo_layout 2d` reads back on the library row), and the delivered
file kept 1920×1080 with the source's own aspect — scaling did not touch the geometry.
The hint the operator is supposed to see was not looked at in a browser.

**Verdict on claim 5: PASSED for detection, storage and geometry on flat sources; NOT
EXERCISED for a genuine side-by-side or over-under source, and NOT VERIFIED HERE for the
badge and the hint as rendered.** Detection on real 3D material is covered by unit tests
against captured fixtures; this run added no live evidence for it, because neither source
was 3D.

---

## Claim 4: status, failure reasons and recovery

Recovery and cancellation were exercised deliberately on live hardware. Four scenarios.

### A. Cancel mid-encode

Horimiya 02, job `fc423703`, cancelled at 11.3 % (16:53:57.3). The converter confirmed,
the job reached `cancelled` **4.6 s** later, and the row, title and edition were gone
**5.6 s** after the request. On the host afterwards: no ffmpeg process, no scratch
directory, the bucket unchanged at 6 objects, and no incomplete multipart uploads. The
address is free again because the title row itself is gone.

The converter's log carried `could not abort the dangling upload` even though no upload
had started — the abort is attempted unconditionally and its listing call was refused,
because the converter's storage account lacked `s3:ListBucketMultipartUploads`. That
permission was added on the account during the run (`list_objects` and `delete_object`
stay denied, verified from inside the container). With the cleanup fix in place the
message is a log line and nothing more.

Earlier the same day, **before** that fix, the same abort error took the whole converter
down; Docker restarted it, and the cancelled job sat `running` with its cancel flag set
until the stale sweep finished it. The row, title, object and scratch directory were all
gone afterwards, so nothing leaked — but a cancel could kill the converter, which is what
commit `d7c9122` closes.

### B. Kill the converter mid-encode

Horimiya 05, job `9246ef52`. `sudo kill -9` against the container's PID 1 from the host
at 17:06:49, with the encode at 15.2 %. (Killing from inside is impossible: the slim
image has no `kill` binary, and PID 1 ignores SIGKILL sent from within its own namespace.
Worth knowing before anyone tries to reproduce this.)

Docker restarted the container within 1 s (`RestartCount` 1). The restarted converter
polled an empty queue, because the job was still `running` behind a heartbeat that had
stopped. The stale sweep, at its 180 s threshold, returned the job to the queue between
17:10:14 and 17:10:24 — **about 3 min 30 s after the kill**. Attempt 2 claimed it at
17:10:21 and finished at 17:14:27 in 246.1 s (factor 7.11). End to end, from enqueue to
`done`: 17:05:59 → 17:14:27, **8 min 28 s**, of which about 3.5 min was the stale wait.
The title ended `active` with exactly one object.

**Recovery-time figure corrected, 2026-09-05:** the stale threshold measured above
(180 s) was raised to 360 s once every consumer of the job queue started
heartbeating through its own upload as well as its encode — a converter still
retrying the final report of an already-finished job needs more room than 180 s
left, now that the report itself can be retried for up to 300 s. The recovery from
a killed converter this record measured is therefore **superseded**: at the new
360 s threshold the same failure recovers in roughly six and a half minutes (360 s
plus one reaper poll cycle) rather than the three and a half minutes measured
above, with the second attempt's own encode time unaffected. This was not
re-measured live — repeating a kill against real hardware to confirm arithmetic
already implied by the threshold value would cost a GPU-hours for a fact the code
already guarantees, not a new one — so this figure is arithmetic against the new
constant, not a fresh measurement, and is written that way on purpose.

Nothing was lost, the attempt count behaved, and no orphaned conversion process was left
holding the card.

### C. Converter absent

`docker stop mytube-worker` at 17:15:11, last check-in 17:15:04. Horimiya 06 was enqueued
at 17:16:20 with the check-in 82 s old. The row's derived stage came back
`waiting_for_converter`, naming the converter it last saw, with "1 minute" as how long
ago — the 60 s offline threshold doing exactly what it is for. The row never claims the
work is progressing when nothing is there to progress it.

### D. Card full

The real converter was stopped and a second instance started against the same card with
its VRAM floor raised above what was free (`9000 MiB` demanded, 5225 MiB free). It logged
`waiting_for_gpu: 5225 MiB free, floor is 9000 MiB` on every poll, its check-in row read
`waiting_for_gpu`, and the queued job's stage became `waiting_for_gpu` — a distinct state
from waiting for a converter, which matters, because the two need different responses from
the operator. It waited rather than claiming work it could not run.

The probe was stopped and the real converter started at 17:16:55; it claimed the job
within 15 s. The job was then cancelled while running for hygiene — row and title gone
2.2 s later, host clean.

### The gap, closed: the row now says what it converted

**Closed 2026-09-05**, after the report protocol widened and both halves were rolled to
the same commit. A fresh conversion (`Horimiya Piece 01`, a second ten-bit HEVC episode,
not one of the titles the original run left behind) was run end to end against the
deployed pair and its edition row read back directly:

`source_codec hevc`, `source_profile Main 10`, `subtitle_burned_language rus`,
`subtitle_burned_title Надписи (Crunchyroll)`, `size_bytes 1060989475`,
`duration_seconds 1421.014`, `width 1920`, `height 1080`, `fps 23.976`,
`video_profile High`, `video_level 41`, `pix_fmt yuv420p`, `audio_codec aac`,
`audio_sample_rate 44100`, `audio_channel_layout stereo`, `audio_language rus`,
`stereo_layout 2d`. `audio_title` read back `NULL` — legitimately, since the source
track carried no title tag, not a field the report failed to fill. `poster_key` read
back `65165c71-7bd0-4c74-96ec-4a49984ead27.jpg`, and the object exists in storage
(head request: 33,337 bytes, `image/jpeg`), next to the video object at the size the
row itself records (1,060,989,475 bytes, matching byte for byte).

The job's own average speed (not the tail progress reading) reported **7.58** for this
run, against a wall time of 279.8 s start-to-finish (an encode phase of roughly 187 s
implied by the source's 1421.0 s duration at that factor, plus the upload of the
finished object) — consistent with the reference source's own factor of ~7 measured in
Claim 3, and clearly distinct from the whole-run factor (1421.0 / 279.8 ≈ 5.08), which
is what the tail-reading bug this closure fixed would have reported instead.

The consequence: the check page's technical detail line and the library row's poster
now have real data to render, for any conversion run against this deployed pair from
this point on. The five editions this phase's own runs converted **before** this
closure (`horimiya-01`, `horimiya-03`, `horimiya-04`, `horimiya-05`,
`grand-blue-dreaming-ii-01`) keep their technical columns `NULL` — nothing backfills
them retroactively, since the report that would have filled them was never sent. An
operator who wants the detail line on one of those older titles has Retry, which
re-queues the same source under the new report and fills the row the same way this
measurement did; no migration is warranted when the existing retry path already
produces the same result.

**Verdict on claim 4: PASS.** Recovery, cancellation, the absent converter and the full
card are all measured and all behave (recovery time corrected above for the new
threshold). The failure reason is carried and rendered server-side. The row now says
what it was made from and what was burned into it, measured against the deployed pair
rather than assumed from the protocol change alone.

### The remaining time, now on the row

**Closed 2026-09-08.** The status sentence's own remaining-time slot was permanently
empty until this round: the converter now computes it from the source duration, the
output timestamp and the current speed, and posts it with every heartbeat. Measured
against the deployed pair on a fresh conversion (`Horimiya Piece 02`, a third ten-bit
HEVC episode, never converted before): the job row's own estimate column read `171`
seconds at `8.5%` and `149` seconds at `20.3%` twenty-three seconds later — a positive
number that fell as the encode proceeded, not a static placeholder. The conversion
went on to reach `done`, with the edition row reading back `source_codec hevc`,
`source_profile Main 10`, `subtitle_burned_language rus`, `subtitle_burned_title
Надписи (Crunchyroll)`, `size_bytes 881216842`, `duration_seconds 1421.06`, closing the
one component of this claim the previous round had to write down as failed.

### The start-up refusal, on the real host

**Closed 2026-09-08.** The converter now refuses to start against a scratch mount it
cannot write to, instead of claiming a job and failing partway through with a message
that blames the source file. Reproduced on the real GPU host: a throwaway container of
this round's converter image, given a root-owned directory bind-mounted at its own
scratch path, no card and only fake credentials, exited immediately with one line,
recorded verbatim —

```
ERROR:__main__:scratch directory /work is not writable by this container's user -- see the setup guide for the ownership it needs
```

— before contacting the service, the storage or the card. The temporary directory and
the throwaway container were both removed afterwards. The real converter, checked in
the same session, never logged that line and kept claiming and finishing work normally
throughout.

### The outside bring-up path, run for real

**Closed 2026-09-08.** The self-hoster's own documented bring-up command
(`docker compose --env-file .env.worker -f docker-compose.worker.yml up -d --build`)
was exercised by the container tool itself, not merely read, on the commit this round
ships: with a normal `PATH` the interpolation test rendered a temporary `.env.worker`
against the compose file and confirmed the fake values it held came out the other
side (7 tests passed, none skipped); with the container tool's own directory stripped
from `PATH` for one invocation the same test skipped cleanly instead of failing (the
recorded pair from the prior round: 7 passed with the tool present, 6 passed plus 1
skipped without it). Both outcomes hold again on this round's final commit.

---

## Defects this run found, and their fixes

Every one of these was found by running the thing, not by reading it. All are in this
repository, all carry tests, and the suite is green at 641.

| Commit | Defect | Fix |
|---|---|---|
| `edf8072` | `si=` is the *subtitle-relative* ordinal, not the absolute stream index, and `[ ] , ;` in a filename need filtergraph-level escaping. Both pinned ffmpeg builds refuse the old form — the first live run silently produced a file with no subtitles at all | Correct ordinal, correct escaping, both pinned by tests |
| `ab71cc9` | An embedded subtitle choice fell through to "no subtitles" on the converter, because the kind was never resolved against the fresh probe | Resolve the choice against the probe; the filter builder now refuses an unknown kind instead of quietly building a subtitle-free chain |
| `10bc3b7` | The storage region must match on service and converter, or the upload is refused with `SignatureDoesNotMatch` — the first reference run died here after a clean 203 s encode | Region pinned on both sides, documented, covered |
| `d7c9122` | A storage error in any cleanup path took the converter down instead of being logged | Logged and swallowed; the failure or cancellation report still goes out |
| `930634d` | The region requirement was not in the setup document | Written down where somebody standing the service up will meet it |

Two more fixes landed outside this repository, in the infrastructure configuration: the
region passed through to the converter's compose template, and the image pins. Two
operational faults were also cleared: the storage secret had been stored with a trailing
carriage return (a Windows `openssl` writing CRLF) and was rotated to a clean value, and
the converter's storage policy gained the multipart-listing permission its own abort path
needs.

The first of these five is the one worth remembering. A silently subtitle-free output
file is the worst possible failure mode for this feature: it succeeds, it plays, and it
is wrong. No test caught it, because every test compared the command the builder produced
against the command the builder was expected to produce — and both were wrong in the same
way. Only running it on real ffmpeg found it.

---

## Verdict table

Each row states the claim, the verdict, and the measurement the verdict rests on. A
criterion nobody measured is written as not measured; a criterion measured only in part
says which part.

| # | Claim (acceptance criterion) | Verdict | Measurement |
|---|-------|---------|-------------|
| 1 | Plan produced and explained; subtitle track chosen for burn-in | **PASS** on the pipeline; **NOT VERIFIED HERE** on its presentation | `transcode` on ten-bit HEVC with the codec named; `passthrough` plus over-ceiling warning on 38 Mbit/s H.264; ASS track enumerated, resolved and burned on both sources. The form's own wording, subtitle list and stereo selector were never opened in a browser |
| 2 | Preview frame with burned subtitles renders in seconds, before conversion | **PASS** on honesty; **NOT MEASURED** on duration | Preview at 602.28 s against the delivered file at the same timestamp: same text, same italic face from the attached fonts, same position, same size, legible in both, no encoding damage across two ffmpeg builds on two machines. The render was not put on a clock |
| 3 | Queued in Postgres, run on a separate NVENC machine, ready in under five minutes, workstation free | **PASS** | 249.2 s on the 23.75-min reference (factor 7.06), 275.9 s on a 24.0-min source of a different shape (7.24), three further runs at 246–275 s; queue waits 1.0–5.6 s; card at encoder 80 % mean / 100 % peak; workstation's own card at encoder 0 %, GPU 3 % throughout |
| 4 | Status and failure reason visible; a stranded job returns to the queue by itself | **PASS** (corrected 2026-09-05, remaining time closed 2026-09-08) | Cancel: job `cancelled` in 4.6 s, row gone in 5.6 s, no process, no object, no dangling multipart. Kill: measured ≈3 min 30 s after the kill at the then-180 s threshold, second attempt finished in 246.1 s, 8 min 28 s end to end — **superseded**: the threshold is now 360 s, so the same recovery is ≈6.5 min by arithmetic against the new constant, not re-measured. Absent converter: `waiting_for_converter`, named, "1 minute". Full card: `waiting_for_gpu` at 5225 MiB against a 9000 MiB floor, claimed within 15 s once free. The converted edition's technical detail, source codec/profile and burned-subtitle fields were all NULL when this record was first written; a fresh conversion against the rolled pair on 2026-09-05 read back every field non-null (or a legitimately null `audio_title`) and the poster object confirmed in storage — see "The gap, closed" above. The status sentence's remaining time was empty until this round: a fresh conversion against the redeployed pair on 2026-09-08 read the job row's own estimate at 171 s (8.5%) and 149 s (20.3%) twenty-three seconds later — see "The remaining time, now on the row" above |
| 5 | 3D layout detected, stored, unbroken by scaling; stereo hint shown | **PASS** on flat sources; **NOT EXERCISED** on 3D material; **NOT VERIFIED HERE** on the hint | `2d` detected and stored on both editions; delivered file 1920×1080 with the source's own aspect. Neither source was side-by-side or over-under; the badge and the check-page hint were not seen in a browser |

---

## Still open after this run

1. ~~The success report does not carry what the row needs.~~ **Closed 2026-09-05** — the
   report protocol widened, both halves rolled together, and a fresh conversion against
   the deployed pair read back a fully populated edition row with a real poster. See "The
   gap, closed" under Claim 4.
2. **The chapter text track.** The MP4 muxer writes the source's chapters as a `text`
   stream in the output. Whether the target player tolerates it is unknown — and an as-is
   remux of a chaptered container would carry the same track, so this is not specific to
   conversion. `-map_chapters -1` is the switch if it turns out to matter.
3. **The converter attempts a multipart abort even when no upload started.** Harmless log
   noise since the cleanup fix; still noise.
4. **The preview route extracts the embedded track to a file but still burns from the
   source by ordinal.** It works; it is a simplification worth removing when that code is
   next touched.

## Not verified here — needs a browser, and the world's own player

Everything below is real work this run could not do, listed so nobody mistakes its
absence for a pass:

- Opening a source in the add form: the plan sentence and its explanation as worded, the
  subtitle list, the on-demand preview triggered from the form, the stereo selector's
  guess, the estimate as displayed.
- Playback of `/v/horimiya-01` and `/v/grand-blue-dreaming-ii-01` in a browser and in the
  world's own player; subtitle legibility at the size they actually render; whether the
  chapter text track bothers the player.
- The library rows changing live — queued, working with a percentage, uploading — the
  stop and retry buttons, the converted-edition detail line (populated as of 2026-09-05,
  per the gap closed above, but never seen rendered in a browser) and the stereo badge.

## Library state left behind

`horimiya-01`, `grand-blue-dreaming-ii-01`, `horimiya-03`, `horimiya-04` and
`horimiya-05` are active converted titles from the original run; their technical
columns stay `NULL` as noted above. `horimiya-piece-01`, converted 2026-09-05 to close
the gap, carries the full detail line and a poster. `horimiya-piece-02`, converted
2026-09-08 to measure the remaining-time estimate against the redeployed pair, carries
the same. All seven can be kept or deleted.
