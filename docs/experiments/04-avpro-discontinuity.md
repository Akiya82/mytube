# A boundary between clips with different stream parameters, in a VRChat video player

**English abstract:** Tested whether a video player inside VRChat carries a
boundary between two clips whose stream parameters differ — one difference at a
time: resolution, frame rate, audio channel layout. Three pairs, each joined into
a single HLS manifest with a discontinuity marker at the boundary, each watched on
PC by the owner. Verdict: **PASS on all three pairs**, on all three checks — the
second clip starts, no black frame was seen, and the audio never walked away from
the picture. Two side questions were answered at the same time: the player does
**not** re-read the manifest during a session, and the world's own player **does**
offer a loop control.

**Run date:** 2026-08-28
**Rig:** `src/tools/experiment_discontinuity.sh`
**Observation:** by the project owner, on PC, inside a VRChat world's video player

## The question

The playlist mechanism had two candidates and the choice was deliberately left to
a measurement. One candidate joins the members physically into a single file,
which requires every member to share stream parameters and refuses the list
otherwise. The other generates a manifest that lists the members' segments, which
tolerates members that differ — if, and only if, the player actually carries the
boundary.

Nothing in the documentation answers this. The player is closed-source, and its
manifest code path is not the same code as its progressive-file path, which this
project has already had to measure separately once before. So it was measured.

Two further questions rode along, because two promised behaviours rest on them:
whether the player re-reads the manifest during one continuous session, and
whether looping at the end of a list is something the world's player already does.

## Setup

Six clips, eight seconds each, built as three pairs, one stream parameter
differing per pair and everything else held constant:

| Pair | A side | B side | Held constant |
|---|---|---|---|
| resolution | 1920x1080 | 1280x720 | 24 fps, stereo |
| frame rate | 24 fps | 30 fps | 1920x1080, stereo |
| audio layout | 2 channels | 6 channels | 1920x1080, 24 fps |

Every clip is a solid colour — blue on the A side, red on the B side — with
burned-in text naming which half of which pair it is (`RES A`, `RES B` and so on),
and a distinct sine tone: 440 Hz on the A side, 880 Hz on the B side. The labels
and the tones exist so that a wrong pairing at the boundary is visible and audible
the instant it happens rather than something the watcher has to squint for.

Every clip is encoded H.264 High 4.1 in `yuv420p` with `+faststart`, and every one
is machine-checked with `ffprobe` and this repository's own faststart check before
it is used — a fixture that quietly came out ten-bit would have made the whole
session measure the wrong thing.

Every clip is segmented with `-c copy` on a forced two-second keyframe interval,
and the two halves of each pair are then joined by hand into one manifest — the A
side's segment lines, an unconditional `#EXT-X-DISCONTINUITY`, the B side's
segment lines, the end tag. The target duration is taken from the longer of the
two halves rather than assumed: a target duration lower than a real segment is a
malformed manifest, and a malformed manifest hangs the world for everyone in the
instance, which would have measured a different failure entirely.

## Deviation from the protocol: the serving arrangement

The script serves the fixtures from a throwaway `nginx:alpine` container bound to
a local port, and prints the addresses for a reverse proxy to point at. The owner
instead ran the fixtures from a throwaway stand inside the cluster, reached over
the delivery hostname under a path prefix. This is the same shape — an unsigned,
uncached static file server, sharing nothing with this stack's own storage or
database — reached over a hostname the world already loads video from. The
project's edge delivery configuration was not touched.

## Protocol as executed

For each of the three manifest addresses in turn, inside a world with a video
player, on PC:

1. Paste the address and let it play from the start.
2. Watch whether the second clip starts at all — the burned-in label changes and
   the background colour changes.
3. Watch the moment of the change for a black frame.
4. Listen for at least five seconds past the boundary for the audio drifting away
   from the picture.

Then, from the serving stand's access log, whether a second request for the same
manifest appears during one continuous session. And once, whether the world's own
player controls offer a loop setting.

## Observations

| Pair | Differing parameter | Second clip starts | Black frame at the boundary | Audio drift |
|---|---|---|---|---|
| resolution | 1920x1080 vs 1280x720 | yes | none seen | none — picture and sound switched together |
| frame rate | 24 vs 30 | yes | none seen | none |
| audio layout | stereo vs 5.1 | yes | none seen | none |

All three pairs passed all three checks. No pair distinguished itself, so there is
no single parameter difference to forbid: the result does not name one, because
none of the three broke anything.

Evidence boundary: a person watched this. There is no programmatic recording of
playback inside VRChat and no way to obtain one. The distinct labels and the two
distinct tones are what make the observation more than an impression — a wrong
pairing would have been immediately visible and audible, not a subtle judgement
call.

## Does the player re-read the manifest during a session? No.

This was measured from the serving stand's access log rather than asked. The
player identified itself as `NSPlayer/12.00.26100.9168 WMFSDK/12.00.26100.9168` —
the Windows Media Foundation backend. For each of the three manifests, the log
shows exactly one manifest request, at open, and none afterwards.

The same log answered a question nobody had posed: **the player downloads the
whole list at open.** For each pair, all eight segments were fetched within the
same second as the manifest, and every response was a range request that returned
the file in full — the served byte counts match the on-disk sizes exactly (for
example `seg_res_a_000.ts`, 63544 bytes requested and 63544 served). Across the
three pairs: 27 requests, 1.50 MB, all inside about one second per pair. That is
eager whole-list retrieval, not progressive streaming.

## Does the world's player offer a loop control? Yes.

The owner confirmed that the world's own video player exposes a loop setting in
its own controls, and deliberately did not use it during this test.

## What was not measured

Recorded plainly rather than left as a gap a later reader would read as a pass:

- **Whether eager whole-list retrieval still holds at a realistic size.** The test
  content was sixteen seconds per pair. An evening's list of ten clips is roughly
  forty minutes, and what the player does with a list that large is unknown. It
  may still fetch everything at open, it may not; nothing here says which.
- **Any player other than the Windows Media Foundation backend.** One backend
  answered, in one world, on PC. Nothing here generalises to another.

## Verdict: PASS — build the generated manifest

The player carries a boundary between clips whose resolution, frame rate or audio
channel layout differ, with no black frame and no audio drift. The generated
manifest is therefore the mechanism this service builds.

What this licenses: assembling a list out of clips exactly as they are, without
requiring the members to share stream parameters, and without re-encoding
anything. A library of as-is rips is heterogeneous by nature, and physical joining
would have refused precisely the lists a playlist is most wanted for.

What it does not license:

- **It says nothing about a list of realistic length.** See what was not measured.
- **It says nothing about any other player**, in a browser or elsewhere.
- **It does not relax the rule that nothing is served unvalidated.** A malformed
  manifest hangs the world for everyone in the instance, which is the worst blast
  radius in this project. Every segment must exist and every duration must agree
  before a manifest is served; on any doubt, the stub clip goes out instead.

## Decisions taken on this evidence

**1. The mechanism is the generated manifest.** It plays heterogeneous clips,
which is measured above. Physical joining would have refused exactly the material
a playlist is most wanted for.

**2. "Changes apply from the next clip" is not deliverable, and the wording
changes.** The player does not re-read the manifest, so nothing can change inside
a session under either mechanism — this is now measured rather than suspected.
Generating the manifest fresh on every request, with current signatures, remains
correct: it keeps a signature from ageing out for someone opening the link anew.
But it does nothing for a session already playing.

The interface must therefore say what is true. The current wording is:

> Changes apply from the next clip, not this instant, for anyone already watching.

It is replaced, verbatim, by:

> Changes apply the next time someone opens this link. Anyone already watching
> keeps playing the list as it was when they started.

Both language catalogues carry this text, and both are updated together.

**3. Looping is the world player's setting, not a feature of this service.** The
world's player has its own loop control. This becomes a line in the setup
documentation telling the operator where to find it, and this service builds
nothing for it. Neither mechanism could have delivered it anyway: a joined file
has a fixed duration, and a terminated manifest carries no loop directive.

**4. Playlists in a browser are deferred.** A manifest full of segment addresses
plays natively in one browser only, and the alternative is this project's first
frontend dependency. Playlists are a VRChat capability for now. Single videos in a
browser are unaffected and keep working. No frontend dependency is added, and the
decision to add one is postponed rather than taken.

**5. A member deleted from the library drops out on its own, and the rest keep
playing.** Because the manifest is generated on every request, a deleted member is
simply absent from the next one. Anyone already playing keeps the copy they
downloaded at open and plays it to the end. This is exactly the promised
behaviour, and on this mechanism it costs nothing to implement — no rebuild is
triggered, nothing has to be marked for the operator to fix, and nothing has to be
excused in the interface. The three fallbacks that were on the table — rebuilding
every affected list on delete, marking them for the operator to rebuild, or
accepting the stale playback and saying so — were all shaped by an artifact built
once and left sitting there, and none of them applies here. Under physical joining
the same promise would have meant rebuilding a multi-gigabyte object per affected
list, and the deleted video's bytes would have survived inside it regardless.
