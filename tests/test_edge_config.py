"""Tripwires over `edge/nginx.conf.template` -- a plain unit test module, no
fixtures.

`test_the_delivery_server_block_is_byte_identical_to_the_pinned_digest` and
`test_every_delivery_location_the_edge_proxies_is_exempt_from_the_hostname_check`
were the two red-on-purpose tests an earlier phase wrote against a template
and an `orchestrator.auth` module that did not exist yet; both have been
green for several phases now. The digest has since been re-pinned twice,
each time deliberately: once for the playlist phase's own `location /p/`
block and its `error_page 425` mapping, and once more in this phase for
the fifth `error_page 423` mapping, `EDITION_PREPARING_STATUS`'s own line
-- see `DELIVERY_DIGEST`'s own comment for what each re-pin was taken
after. Below both of the original tests is a third and fourth, added
across those same two phases: this project adopted a pinned digest
precisely because a comment asking a future reader to remember one line is
not enforcement, and relying on exactly that to keep an application status
constant and a template directive in agreement would be the same mistake
one file over -- so `test_the_not_ready_status_the_application_answers_is_mapped_to_a_clip`
ties `orchestrator.web.routes_playlists.PLAYLIST_NOT_READY_STATUS` to the
template's own `error_page` mapping, parsed out of the template rather than
restated as a literal, and this phase's own
`test_the_preparing_status_the_application_answers_is_mapped_to_a_clip`
does the identical thing for `orchestrator.resolve.EDITION_PREPARING_STATUS`.
"""

import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "edge" / "nginx.conf.template"

# The line the admin `server` block is appended directly after. Not one
# byte before this line may change -- the admin block is appended, not
# interleaved.
SENTINEL = "# ==== admin surface begins here ===="

# Taken from the template by computing sha256 over everything before
# SENTINEL (UTF-8 bytes) -- pasted here as a literal, never recomputed
# from the same file at test time, which would make this test always
# pass. Re-pinned twice, each time deliberately: once after the playlist
# phase's own edit to the delivery block (a fourth `location /p/` block
# mirroring `/v/`, and a fourth `error_page 425 ... playlist-not-ready.mp4`
# mapping), and once more in this phase for its own one-line addition, a
# fifth `error_page 423 ... preparing.mp4` mapping -- this phase's one and
# only digest re-pin, since the public prefix location needed no change
# and every numbered/edition address reaches the application through the
# existing `location /v/` block unmodified.
DELIVERY_DIGEST = "a0a963e6cecb55449caa12642387caeaca396cc03b1cb497c6bdde98cdd280a0"

# A prefix `location` block (not a regex `location ~ ...` block) whose body
# proxies to the `orchestrator` upstream. The object-storage location is a
# regex location and is deliberately excluded by this same shape -- it
# never reaches the application at all.
_LOCATION_BLOCK = re.compile(r"location\s+(/\S+)\s*\{")


def _delivery_location_prefixes(head_text: str) -> list[str]:
    """Every prefix `location` in `head_text` whose body proxies to the
    `orchestrator` upstream -- the addresses a foreign hostname arrives on
    legitimately, because the edge overwrites the forwarded host on every
    request it proxies to them.
    """
    prefixes = []
    for match in _LOCATION_BLOCK.finditer(head_text):
        start = match.end() - 1  # the opening brace itself
        depth = 0
        i = start
        while i < len(head_text):
            if head_text[i] == "{":
                depth += 1
            elif head_text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = head_text[start : i + 1]
        if "proxy_pass http://orchestrator" in body:
            prefixes.append(match.group(1))
    return prefixes


def test_the_delivery_server_block_is_byte_identical_to_the_pinned_digest():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    head, sep, _tail = text.partition(SENTINEL)

    assert sep, (
        f"the sentinel {SENTINEL!r} is missing from {TEMPLATE_PATH} -- the "
        "admin server block has not been appended after it yet, "
        "or somebody removed the marker this test is pinned to"
    )
    digest = hashlib.sha256(head.encode("utf-8")).hexdigest()
    assert digest == DELIVERY_DIGEST, (
        "the delivery server block (everything before the sentinel) changed "
        f"at least one byte -- computed {digest}, pinned {DELIVERY_DIGEST}"
    )


def test_the_edge_template_never_gains_a_subrequest_authorisation_directive():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "auth_request" not in text, (
        "auth_request appeared in the edge template -- the subrequest "
        "authorisation mechanism was deliberately not chosen for the "
        "delivery path; a login on this template's delivery "
        "server block breaks every viewer"
    )


def test_the_admin_block_does_not_proxy_the_health_endpoint():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    _head, _sep, tail = text.partition(SENTINEL)

    assert re.search(r"location\s*=\s*/healthz\s*\{[^}]*return\s+404", tail), (
        "the admin server block must refuse /healthz with a bare 404 -- an "
        "anonymous endpoint that runs a database query has no reason to be "
        "reachable from the public internet"
    )


def test_every_delivery_location_the_edge_proxies_is_exempt_from_the_hostname_check():
    from orchestrator.auth import HOST_EXEMPT_PREFIXES

    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    head, _sep, _tail = text.partition(SENTINEL)

    prefixes = _delivery_location_prefixes(head)

    # Pinned so a future change to the regex above cannot silently turn
    # this test into a no-op that finds nothing and asserts nothing. Three
    # since the playlist phase's own `location /p/` joined `/v/` and
    # `/refresh/` -- this is that deliberate update, not a future one.
    assert len(prefixes) == 3, (
        f"expected exactly three delivery locations proxying to orchestrator "
        f"in the template as it stands, found {prefixes!r} -- if this "
        "assertion is what changed, update the count deliberately rather "
        "than deleting it"
    )

    for prefix in prefixes:
        assert prefix in HOST_EXEMPT_PREFIXES, (
            f"the edge proxies {prefix!r} to the orchestrator, forwarding "
            "its own hostname on every request -- but orchestrator.auth."
            "HOST_EXEMPT_PREFIXES does not exempt it from the interface "
            "hostname check, so every viewer request through this address "
            "would be refused with a plain-text 400 it cannot render. Add "
            f"{prefix!r} to HOST_EXEMPT_PREFIXES in src/orchestrator/auth.py"
        )


def test_the_not_ready_status_the_application_answers_is_mapped_to_a_clip():
    """Changing `PLAYLIST_NOT_READY_STATUS` in the application without
    updating this template's own `error_page` mapping would leave a
    player receiving a status the delivery block does not map to any
    clip -- it renders as the black screen the fourth clip exists to
    abolish, with the whole suite otherwise green. Asserts the number is
    parsed out of the template, never restated as a literal here.
    """
    from orchestrator.web.routes_playlists import PLAYLIST_NOT_READY_STATUS

    text = TEMPLATE_PATH.read_text(encoding="utf-8")

    match = re.search(
        r"error_page\s+(\d+)\s+=200\s+/errors/playlist-not-ready\.mp4;", text
    )
    assert match, (
        "no `error_page <code> =200 /errors/playlist-not-ready.mp4;` "
        "directive found in the template"
    )
    assert int(match.group(1)) == PLAYLIST_NOT_READY_STATUS, (
        f"the template maps status {match.group(1)} to the not-ready clip, "
        f"but the application's PLAYLIST_NOT_READY_STATUS is "
        f"{PLAYLIST_NOT_READY_STATUS} -- a player receiving the real status "
        "would get no mapped clip at all and see a black screen"
    )


def test_the_preparing_status_the_application_answers_is_mapped_to_a_clip():
    """The identical tripwire, one status code lower in the same table:
    changing `EDITION_PREPARING_STATUS` without updating the template's own
    `error_page` mapping would leave a still-converting episode's address
    answering a status the delivery block does not map to any clip.
    """
    from orchestrator.resolve import EDITION_PREPARING_STATUS

    text = TEMPLATE_PATH.read_text(encoding="utf-8")

    match = re.search(r"error_page\s+(\d+)\s+=200\s+/errors/preparing\.mp4;", text)
    assert match, (
        "no `error_page <code> =200 /errors/preparing.mp4;` directive "
        "found in the template"
    )
    assert int(match.group(1)) == EDITION_PREPARING_STATUS, (
        f"the template maps status {match.group(1)} to the preparing clip, "
        f"but the application's EDITION_PREPARING_STATUS is "
        f"{EDITION_PREPARING_STATUS} -- a viewer of a still-converting "
        "episode would get no mapped clip at all and see a black screen"
    )
