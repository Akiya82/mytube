"""URL signing for the /blob/ delivery path.

This module's formula MUST mirror, character for character, the
`secure_link_md5` expression in src/edge/nginx.conf.template:

    secure_link_md5 "$secure_link_expires${uri}${MYTUBE_SECURE_LINK_SECRET}";

Order is `expires`, then `uri` (blob_path), then the secret, with no
separators between them. Any deviation gives an unconditional signature
mismatch with no diagnostic (Pitfall C, 01-RESEARCH.md).

Note the curly-braced `${uri}` in the nginx expression above (not bare
`$uri`): nginx's variable-name parser greedily consumes identifier chars
following `$uri`, so a secret starting with an alnum character — which
every `openssl rand -hex` secret does — gets read as part of the variable
name (`$uritest...`) and nginx refuses to start ("unknown variable"). This
does not affect this module: the bounding only matters for nginx's own
string-interpolation syntax, not for this plain Python concatenation.
"""
import base64
import hashlib
import time


def blob_path(edition_id: str) -> str:
    """Return the exact `$uri` nginx sees for this edition: /blob/{id}.mp4."""
    return f"/blob/{edition_id}.mp4"


def sign_blob_url(edition_id: str, secret: str, ttl_seconds: int) -> tuple[str, int]:
    """Sign a /blob/ URL the way nginx's secure_link_md5 expects.

    Mirrors src/edge/nginx.conf.template's secure_link_md5 expression
    (see module docstring): expires + blob_path(edition_id) + secret,
    concatenated with no separators, MD5-digested, then base64url-encoded
    with the padding `=` characters stripped.

    $remote_addr is deliberately NOT part of this formula: the audience
    (7-15 friends) watches from mixed networks including mobile, and tying
    the signature to a client IP would trigger the D-04 refresh loop mid
    film whenever the viewer's carrier rotates their address (Pitfall B,
    01-RESEARCH.md). The signature's protection comes from the 12h TTL
    (D-03) and the secrecy of the URL itself; per-viewer revocation is not
    implemented (D-05).
    """
    expires = int(time.time()) + ttl_seconds
    raw = f"{expires}{blob_path(edition_id)}{secret}".encode()
    digest = hashlib.md5(raw).digest()
    sig = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return sig, expires
