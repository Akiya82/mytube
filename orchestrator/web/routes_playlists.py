"""The playlist assembly screen and the public playlist address (D-101..
D-104, D-107, D-108).

Two different surfaces share this module for the same reason
`routes_titles.py` shares the Library screen and the add flow: they are
about the same catalog rows. `GET /playlists/new` and `POST /playlists`
sit behind the operator session, exactly like `/library/*`. `GET
/p/{slug}` is the one public address a video player reaches, unauthenticated
and unsignable from outside -- it joins `/v/` and `/refresh/` in
`orchestrator.auth.HOST_EXEMPT_PREFIXES`.

Every blocking call here goes through the acquire-with-timeout shape
`orchestrator.resolve` already uses, for the same reason that module's own
docstring gives: the event loop is shared with every other request this
process answers, including `GET /v/{slug}`.

The public address deliberately has no tombstone answer. A playlist slug
is a plain unique column with no history table behind it (D-103): renaming
a playlist breaks its old address on purpose, so there is nothing to
tombstone and an address that stops working stops working outright --
`GET /p/{slug}` never answers 410, unlike `/v/{slug}`.
"""
import asyncio
import logging
import uuid

import asyncpg
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from orchestrator import auth
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media import playlist_build
from orchestrator.media.playlist_queue import PlaylistBuildJob
from orchestrator.resolve import external_base_url
from orchestrator.signing import blob_path, sign_object_key
from orchestrator.slugs import SlugError, normalize_slug, pick_unique_slug
from orchestrator.web.templates import render

logger = logging.getLogger(__name__)
router = APIRouter()

# In VRChat every HTTP status a video player receives renders as the same
# black screen, so the only way a viewer can tell "the operator has not
# finished this playlist yet" from "the server is broken" is for the edge
# to answer each with a different stub clip -- and that mapping keys off
# the status code (D-104). The four codes the delivery block already maps
# are 404, 410, 403 and the 5xx group; this one is picked from what is
# free. 425 ("Too Early") is not otherwise meaningful to this
# application -- it exists here purely as a distinct signal edge/nginx.conf.
# template's error_page directive maps to the fourth clip.
PLAYLIST_NOT_READY_STATUS = 425


async def _acquire_and(pool, work):
    """Same shape as `orchestrator.resolve`'s own helper -- kept local
    rather than imported, since it is a private, three-line helper and
    this module's own acquire/timeout pair is bounded by the same shared
    constant (`auth.ACQUIRE_TIMEOUT_SECONDS`) the video address uses.
    """
    async with pool.acquire() as conn:
        return await work(conn)


def _signed_blob_url(request: Request, object_key: str) -> str:
    """The one place this module turns a stored key into a delivery
    address, whether that address ends up in a redirect or inside a
    generated document.

    `object_key` is handed straight through to `sign_object_key`/
    `blob_path` with nothing appended -- it already carries its extension,
    mirroring `editions.object_key`'s own stored shape (see
    `orchestrator.signing`'s module docstring for what appending a second
    one would do to the address). There is no second signing formula here
    and there must never be: the expression has to mirror the edge's own
    character for character, and a mismatch is a signature failure with no
    diagnostic at all.
    """
    settings = get_settings()
    sig, exp = sign_object_key(
        object_key, settings.secure_link_secret, settings.sign_ttl_seconds
    )
    return (
        f"{external_base_url(request)}{blob_path(object_key)}"
        f"?exp={exp}&sig={sig}"
    )


def _sign_redirect(request: Request, object_key: str) -> RedirectResponse:
    """The signed `/blob/` redirect for a playlist assembled as one file."""
    return RedirectResponse(_signed_blob_url(request, object_key), status_code=302)


async def _enqueue_build(request: Request, playlist_id) -> None:
    """Hand the assembly off to the background worker and come straight
    back, the way the add route returns before its own work happens.

    Save is the one trigger. Adding, removing or moving a member does not
    rebuild: a burst of arrow clicks would otherwise start a burst of
    multi-gigabyte builds, and the screen already carries a save button
    and a note about when a change reaches a viewer.

    `getattr` rather than a bare attribute because the queue is created in
    the application lifespan, and the unit tests for these screens drive
    the app without one -- a save in that setting has nothing to enqueue
    onto and must not raise on the way to its redirect.
    """
    queue = getattr(request.app.state, "playlist_queue", None)
    if queue is None:
        logger.warning(
            "no build worker on this application: playlist %s was saved but "
            "nothing will assemble it",
            playlist_id,
        )
        return
    await queue.enqueue(PlaylistBuildJob(playlist_id=playlist_id))


@router.get("/playlists/new")
async def new_playlist_form_route(request: Request):
    return render(
        request,
        "playlist_form.html",
        editing=False,
        name_value="",
        slug_value="",
        slug_plan=None,
    )


@router.post("/playlists")
async def create_playlist_route(
    request: Request, name: str = Form(...), slug: str = Form("")
):
    """Derive the address through `orchestrator.slugs.normalize_slug` and
    `pick_unique_slug` against `taken_playlist_slugs` -- never `plan_slug`
    and never `taken_slugs`, the video-address machinery: `/p/` and `/v/`
    are separate namespaces under separate path prefixes, and playlists
    keep no tombstone to check (D-103). A collision is a plain collision
    and gets the auto-suffix, visible and editable, with no confirmation
    step -- there is no history here to ask about reusing.
    """
    clean_name = name.strip()
    try:
        candidate = normalize_slug(slug) if slug.strip() else normalize_slug(clean_name)
    except SlugError:
        return render(
            request,
            "playlist_form.html",
            status_code=400,
            editing=False,
            name_value=clean_name,
            slug_value=slug,
            slug_plan=None,
            slug_error=True,
        )

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        taken = await queries.taken_playlist_slugs(conn)
        chosen, _similar = pick_unique_slug(candidate, taken)
        await queries.create_playlist(conn, clean_name, chosen)

    return RedirectResponse("/library", status_code=302)


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


@router.get("/playlists/{playlist_id}/edit")
async def edit_playlist_form_route(playlist_id: str, request: Request):
    """The assembly screen's edit entry point (03-UI-SPEC.md Screen 5) --
    name/address fields pre-filled, the timing note, the ordered member
    list and the picker. A playlist that does not exist, or one whose
    status is not `active`, answers 404 -- `.get("status", "active")`
    rather than a bare index so a hand-built test double that omits the
    field (this screen's own frozen behaviour spec does, throughout) is
    treated as active rather than raising.
    """
    parsed = _parse_uuid(playlist_id)
    if parsed is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        playlist = await queries.playlist_detail(conn, parsed)
        if playlist is None or playlist.get("status", "active") != "active":
            return Response(status_code=404)
        members = await queries.playlist_members(conn, parsed)
        editions = await queries.active_editions_for_picker(conn)

    return render(
        request,
        "playlist_form.html",
        editing=True,
        playlist=playlist,
        name_value=playlist["name"],
        slug_value=playlist["slug"],
        slug_plan=None,
        members=members,
        editions=editions,
    )


@router.post("/playlists/{playlist_id}")
async def update_playlist_route(
    playlist_id: str, request: Request, name: str = Form(...), slug: str = Form("")
):
    """Save a changed name and/or address. The slug goes through the same
    `normalize_slug`/`pick_unique_slug` path `create_playlist_route` uses,
    with this playlist's own current slug excluded from the taken set --
    saving without changing the address must not collide with itself.
    """
    parsed = _parse_uuid(playlist_id)
    if parsed is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        playlist = await queries.playlist_detail(conn, parsed)
        if playlist is None or playlist.get("status", "active") != "active":
            return Response(status_code=404)

        clean_name = name.strip()
        try:
            candidate = normalize_slug(slug) if slug.strip() else normalize_slug(clean_name)
        except SlugError:
            members = await queries.playlist_members(conn, parsed)
            editions = await queries.active_editions_for_picker(conn)
            return render(
                request,
                "playlist_form.html",
                status_code=400,
                editing=True,
                playlist=playlist,
                name_value=clean_name,
                slug_value=slug,
                slug_plan=None,
                slug_error=True,
                members=members,
                editions=editions,
            )

        taken = await queries.taken_playlist_slugs(conn)
        taken.discard(playlist["slug"])
        chosen, _similar = pick_unique_slug(candidate, taken)
        await queries.update_playlist(conn, parsed, clean_name, chosen)

    await _enqueue_build(request, parsed)
    return RedirectResponse(f"/playlists/{playlist_id}/edit", status_code=302)


@router.get("/playlists/{playlist_id}/items")
async def playlist_items_fragment_route(playlist_id: str, request: Request):
    """The member section alone, no document or navigation element around
    it -- the drop handler's own read-only refetch address, and generally
    the standalone half of the "one file, two ways in" shape
    `partials/library_row.html` already established for the 4-second poll.
    Read-only, session-gated like every route in this module, and it
    changes nothing.

    Deliberately does not query the picker's editions: a drop only ever
    needs the order section re-rendered, never the picker, so this route
    never touches `active_editions_for_picker` at all -- the partial
    tolerates the omission with a plain empty default (see its own
    docstring).
    """
    parsed = _parse_uuid(playlist_id)
    if parsed is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        playlist = await queries.playlist_detail(conn, parsed)
        if playlist is None or playlist.get("status", "active") != "active":
            return Response(status_code=404)
        members = await queries.playlist_members(conn, parsed)

    return render(request, "partials/playlist_members.html", playlist=playlist, members=members)


@router.post("/playlists/{playlist_id}/items")
async def add_playlist_item_route(
    playlist_id: str, request: Request, edition_id: str = Form(...)
):
    """Append `edition_id` to the member list and redirect back to the
    assembly screen -- never a fragment body (see the module-level note
    below `move_playlist_item_route`, which every mutating route in this
    module follows for the same reason). Repeats are allowed (D-101): no
    lookup here refuses an edition already present.

    Both identifiers arrive from the browser and both are foreign keys, so
    a well-formed one naming a playlist or an edition that does not exist
    reaches the statement and comes back as a foreign key violation. That
    is a bad request, not a broken server, so it is answered as 404 here
    rather than escaping as a 500. Caught rather than pre-checked because
    the check and the insert would race anyway, and the constraint is the
    only thing that actually decides.
    """
    parsed_playlist = _parse_uuid(playlist_id)
    parsed_edition = _parse_uuid(edition_id)
    if parsed_playlist is None or parsed_edition is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            await queries.add_playlist_item(conn, parsed_playlist, parsed_edition)
        except asyncpg.exceptions.ForeignKeyViolationError:
            return Response(status_code=404)

    return RedirectResponse(f"/playlists/{playlist_id}/edit", status_code=302)


@router.post("/playlists/{playlist_id}/items/{item_id}/remove")
async def remove_playlist_item_route(playlist_id: str, item_id: str, request: Request):
    """Remove one member and redirect back to the assembly screen.
    `queries.remove_playlist_item` scopes its DELETE to this playlist id
    as well as the item id (T-03-18) and raises `LookupError` when the
    item belongs to a different playlist -- answered here as 404, not a
    redirect that silently changed nothing.
    """
    parsed_playlist = _parse_uuid(playlist_id)
    parsed_item = _parse_uuid(item_id)
    if parsed_playlist is None or parsed_item is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            await queries.remove_playlist_item(conn, parsed_playlist, parsed_item)
        except LookupError:
            return Response(status_code=404)

    return RedirectResponse(f"/playlists/{playlist_id}/edit", status_code=302)


@router.post("/playlists/{playlist_id}/items/{item_id}/move")
async def move_playlist_item_route(
    playlist_id: str, item_id: str, request: Request, direction: str = Form(...)
):
    """One arrow click, one small POST, one redirect back to the assembly
    screen -- the whole no-mouse baseline (D-107). This is a plain form
    post: `static/app.js` intercepts a submit only to run a confirmation
    dialog and to disable a one-shot button, never to cancel one and swap
    markup in place, and there is no such machinery in this project to
    reach for. A route that answered a fragment body here would navigate
    the browser to that fragment on a script-free click -- an unstyled
    list with no stylesheet and no navigation -- and reloading it would
    re-post the move. Every mutating route in this module redirects for
    exactly this reason; `GET /playlists/{playlist_id}/items` above is the
    separate, safe-to-refetch address the drag handler reads from instead.

    A `direction` that is neither "up" nor "down" is ignored outright,
    before `queries.move_playlist_item` gets a chance to also treat it as
    a no-op (T-03-19) -- belt and suspenders, cheap at this size.
    """
    parsed_playlist = _parse_uuid(playlist_id)
    parsed_item = _parse_uuid(item_id)
    if parsed_playlist is None or parsed_item is None:
        return Response(status_code=404)

    if direction in ("up", "down"):
        pool = request.app.state.db_pool
        async with pool.acquire() as conn:
            await queries.move_playlist_item(conn, parsed_playlist, parsed_item, direction)

    return RedirectResponse(f"/playlists/{playlist_id}/edit", status_code=302)


async def _read_for_delivery(conn, slug: str):
    """Everything the public address needs, on one connection: the
    playlist row, and -- only when a generated document is what it will
    answer with -- the artifacts that document lists.

    The second read is what the validation checks its keys against, so
    both come from the same place and neither is a second fan-out on the
    shared event loop. It is skipped entirely for a row that will answer
    404, not-ready, or a plain redirect, which is every row that has no
    document to render.
    """
    row = await queries.resolve_playlist_slug(conn, slug)
    if (
        row is None
        or row["status"] != "active"
        or row["built_at"] is None
        or row["mode"] != "hls"
    ):
        return row, None
    return row, await queries.playlist_artifact_sections(conn, row["id"])


def _manifest_response(request: Request, row, artifacts) -> Response:
    """Generate this playlist's document, per request, and refuse to send
    it if it does not validate.

    Generated rather than stored, because a stored one carries signatures
    that age out partway through an evening and there is no refresh path
    for a document the way there is for a blob address. It buys nothing
    for a session already playing -- the player reads the list once, at
    open, and never again, which was measured rather than assumed -- but
    it is what keeps the address working for the next person to open it.

    **Nothing that fails validation is ever sent, and there is no
    fall-back-and-send-it-anyway branch.** A malformed document does not
    fail for one viewer; it hangs every player in the instance. The
    not-ready status goes out instead, which the edge already answers with
    a clip a person can actually read, and the specific failure goes to
    the log with enough detail to fix it.

    Marked unstorable because its contents are only valid for as long as
    the signatures inside it.
    """
    sections = [
        playlist_build.Section(
            title_name=artifact["title_name"] or "",
            segments=(
                playlist_build.Segment(
                    object_key=artifact["object_key"],
                    duration_seconds=artifact["duration_seconds"] or 0.0,
                    url=_signed_blob_url(request, artifact["object_key"]),
                ),
            ),
        )
        for artifact in (artifacts or [])
    ]
    document = playlist_build.render_manifest(sections)
    known = {artifact["object_key"] for artifact in (artifacts or [])}

    verdict = playlist_build.validate_manifest(sections, known, document=document)
    if not verdict.ok:
        logger.error(
            "refusing to serve the playlist at /p/%s: %s %s",
            row["slug"],
            verdict.reason_key,
            verdict.reason_params,
        )
        return Response(status_code=PLAYLIST_NOT_READY_STATUS)

    return Response(
        content=document,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/p/{slug}")
async def resolve_playlist_route(slug: str, request: Request):
    pool = request.app.state.db_pool
    try:
        row, artifacts = await asyncio.wait_for(
            _acquire_and(pool, lambda conn: _read_for_delivery(conn, slug)),
            timeout=auth.ACQUIRE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return Response(status_code=503)

    # Answer 404 for ANY status other than active, not only for a deleted
    # one -- stated as the condition rather than as a list of statuses, so
    # a status added later is refused by default instead of falling
    # through to the built branch below. A playlist inside its cancellable
    # delete grace window is not active; its address must stop resolving
    # the moment the operator presses delete, and there is nothing to
    # tombstone -- from outside, a deletion that can still be cancelled
    # and one that is finished look the same, which is what a namespace
    # with no history means (D-103). Deliberately NOT the branch that
    # answers 410 for a non-active status the way `/v/{slug}` does:
    # playlists keep no address history, so there is no tombstone to
    # answer for.
    if row is None or row["status"] != "active":
        return Response(status_code=404)

    if row["built_at"] is None:
        return Response(status_code=PLAYLIST_NOT_READY_STATUS)

    if row["mode"] == "concat":
        return _sign_redirect(request, row["built_object_key"])

    return _manifest_response(request, row, artifacts)
