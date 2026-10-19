# Experiment 3: extensionless + Content-Type (reference)

**English abstract:** Checked whether an extensionless address serving
`Content-Type: video/mp4` plays directly in AVPro, as a reference point
only -- the actual implementation always redirects through a signed
`/blob/{edition_id}.mp4` address regardless of this result, since a direct
extensionless address has nowhere to carry the signature without polluting
the short link. Informational; does not gate anything.

**This experiment's result is informational and does not affect the
implementation.** Even if an extensionless file with `Content-Type:
video/mp4` plays directly in AVPro, direct serving will not be chosen:
signatures have nowhere to go without polluting the short
link. This delivery implementation always returns `302` from `/v/{slug}` to
`/blob/{edition_id}.mp4`, regardless of this experiment's outcome.

## Rig

The same rig as experiment 1 (see `01-avpro-direct-mp4.md`, the "Deviation
from the protocol: the rig" section) — the owner's MinIO bucket behind a
caching reverse proxy (`nginx/1.27.5`), not a bare file on the domain.

## Protocol

| Field | Value |
|---|---|
| Date | 2026-08-20 (curl check, 21:3x UTC) |
| VRChat build | not applicable to this step — the experiment never reached the player (see "Result") |

The address of the same control clip was requested, but **without an
extension** — the object's key in the bucket is `movies/control.mp4`, and
the path requested was `movies/control` (without `.mp4`).

```
$ curl -I https://s3.example.com/movies/control
HTTP/1.1 404 Not Found
Content-Length: 333
Content-Type: application/xml
Server: nginx/1.27.5
X-Cache: MISS
```

## Result: 404 (never reached an AVPro check)

Observation: the server returned `404 Not Found` with an XML body
(`Content-Type: application/xml`) — this is S3's `NoSuchKey` response, not a
video-server response. The cause is purely technical, not behavioural: the
object in the bucket sits under the key `movies/control.mp4`, and at the
path `movies/control` (without `.mp4`) **there is no object at all**. S3 is
correctly saying "no such key."

This means the experiment **did not check** whether AVPro behaves
identically with extensionless addresses — the request never reached the
video data, so there was nothing further to check. The result is not a
failure of this experiment, it demonstrates that "extensionless" on a bare
S3 bucket is not equivalent to "strip the extension and serve the same
object" — that needs a routing layer (redirect/rewrite), which the bare-S3
rig does not have.

## Conclusion

The result is informational and **does not apply to this delivery
implementation**: in the project's architecture, the
extensionless address `/v/{slug}` is served not by S3 directly but by the
**orchestrator**, which resolves the slug and returns `302` to
`/blob/{edition_id}.mp4` — meaning the path "a raw extensionless request
straight to storage" is not on the production delivery path at all. A bare
S3 bucket's 404 on a missing key says nothing for or against the redirect
architecture — it only confirms that S3 has no extension-autocompletion
magic, which was already assumed.

This experiment is marked **informational / not applicable to the
implementation**, not failed.
