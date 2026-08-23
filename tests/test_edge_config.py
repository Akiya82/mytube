"""Tripwires over `edge/nginx.conf.template` -- a plain unit test module, no
fixtures. Two of these are red today on purpose, both for the same reason
this whole phase exists: a comment asking a future phase to remember one
line is not enforcement, a test that fails at the moment the edit happens
is.

- `test_the_delivery_server_block_is_byte_identical_to_the_pinned_digest`
  fails right now because `SENTINEL` is not in the template yet -- plan 03
  appends the admin `server` block after it. Once that block lands, this
  test either keeps passing (nothing before the sentinel moved) or fails
  loudly (something did), which is the whole point of pinning a digest
  taken from the file as it stood before any edit.
- `test_every_delivery_location_the_edge_proxies_is_exempt_from_the_hostname_check`
  fails right now because `orchestrator.auth` does not exist yet -- the
  same RED state as `tests/test_auth.py`.
"""
import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "edge" / "nginx.conf.template"

# The line plan 03 appends the admin `server` block directly after. Not one
# byte before this line may change -- the admin block is appended, not
# interleaved.
SENTINEL = "# ==== admin surface begins here ===="

# Taken from the template as it stood before any edit in this phase, by
# computing sha256 over its full current contents (UTF-8 bytes) -- pasted
# here as a literal, never recomputed from the same file at test time,
# which would make this test always pass.
DELIVERY_DIGEST = "b715ae0c141ab3f3f0ce20ab3986e401c06dee805ecf9c1f66a468911f0ceb96"

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
        "admin server block has not been appended after it yet (plan 03), "
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
        "delivery path (see D-73); a login on this template's delivery "
        "server block breaks every viewer"
    )


def test_the_admin_block_does_not_proxy_the_health_endpoint():
    """Red until plan 03 appends the admin server block after SENTINEL --
    `tail` is empty on the template as it stands today, so the assertion
    below fails honestly rather than being skipped past.
    """
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    _head, _sep, tail = text.partition(SENTINEL)

    assert re.search(r"location\s*=\s*/healthz\s*\{[^}]*return\s+404", tail), (
        "the admin server block must refuse /healthz with a bare 404 -- an "
        "anonymous endpoint that runs a database query has no reason to be "
        "reachable from the public internet"
    )


def test_every_delivery_location_the_edge_proxies_is_exempt_from_the_hostname_check():
    from orchestrator.auth import HOST_EXEMPT_PREFIXES  # RED until plan 04

    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    head, _sep, _tail = text.partition(SENTINEL)

    prefixes = _delivery_location_prefixes(head)

    # Pinned so a future change to the regex above cannot silently turn
    # this test into a no-op that finds nothing and asserts nothing.
    assert len(prefixes) == 2, (
        f"expected exactly two delivery locations proxying to orchestrator "
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
