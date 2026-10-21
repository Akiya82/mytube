"""URL signing for the /blob/ delivery path.

This module's formula MUST mirror, character for character, the
`secure_link_md5` expression in edge/nginx.conf.template:

    secure_link_md5 "$secure_link_expires${uri}${MYTUBE_SECURE_LINK_SECRET}";

Order is `expires`, then `uri` (blob_path), then the secret, with no
separators between them. Any deviation gives an unconditional signature
mismatch with no diagnostic.

Note the curly-braced `${uri}` in the nginx expression above (not bare
`$uri`): nginx's variable-name parser greedily consumes identifier chars
following `$uri`, so a secret starting with an alnum character — which
every `openssl rand -hex` secret does — gets read as part of the variable
name (`$uritest...`) and nginx refuses to start ("unknown variable"). This
does not affect this module: the bounding only matters for nginx's own
string-interpolation syntax, not for this plain Python concatenation.

This codebase carries a bare identifier in one place (an edition id, the
shape every caller before playlists existed has ever passed) and an
identifier with an extension already on it in another (a playlist
artifact's stored object key, mirroring `editions.object_key`'s own
shape) — the delivery address pattern accepts exactly one shape at the
edge, `{identifier}.mp4`, and a mismatch produces a signature that
verifies against an address the edge refuses: a failure with no
diagnostic at all, the same failure class the paragraph above already
warns about. `blob_path` below is idempotent on the `.mp4` suffix rather
than trusting each caller to get this right — it appends the extension
only when the value does not already carry it, so a bare id and an
already-extensioned key both reach the edge as the exact same key the
object was stored under, never doubled and never missing.
"""

import base64
import hashlib
import time


def blob_path(object_key: str) -> str:
    """Return the exact `$uri` nginx sees for this object: /blob/{key}.mp4.

    `object_key` may be a bare identifier (an edition id) or a full
    storage key that already carries its extension (a playlist artifact's
    `object_key`) — `.mp4` is appended only when it is not already there,
    so this is safe to call with either shape without ever doubling or
    dropping the extension (see the module docstring).
    """
    if object_key.endswith(".mp4"):
        return f"/blob/{object_key}"
    return f"/blob/{object_key}.mp4"


def sign_object_key(object_key: str, secret: str, ttl_seconds: int) -> tuple[str, int]:
    """Sign a /blob/ URL for `object_key`, whichever shape `blob_path`
    accepts (bare id or already-extensioned key).

    Mirrors edge/nginx.conf.template's secure_link_md5 expression
    (see module docstring): expires + blob_path(object_key) + secret,
    concatenated with no separators, MD5-digested, then base64url-encoded
    with the padding `=` characters stripped.

    $remote_addr is deliberately NOT part of this formula: the audience
    (7-15 friends) watches from mixed networks including mobile, and tying
    the signature to a client IP would trigger the refresh loop mid
    film whenever the viewer's carrier rotates their address. The
    signature's protection comes from the 12h TTL and the secrecy of the
    URL itself; per-viewer revocation is not implemented.
    """
    expires = int(time.time()) + ttl_seconds
    raw = f"{expires}{blob_path(object_key)}{secret}".encode()
    digest = hashlib.md5(raw).digest()
    sig = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return sig, expires


def sign_blob_url(edition_id: str, secret: str, ttl_seconds: int) -> tuple[str, int]:
    """Sign a /blob/ URL for an edition. Kept as its own name and signature
    so every existing caller is untouched; delegates to
    `sign_object_key` — `blob_path`'s own idempotent `.mp4` handling means
    this produces byte-identical output to before for a bare edition id,
    and the correct single-extension output if it is ever handed a key
    that already carries one.
    """
    return sign_object_key(edition_id, secret, ttl_seconds)
