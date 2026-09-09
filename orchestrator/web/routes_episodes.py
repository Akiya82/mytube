"""Per-episode actions and per-episode/edition check pages (plan 10,
D-158, D-164).

A twelve-episode season is twelve things that can go wrong independently
-- until this module the only controls that exist are cancel/retry/
dismiss/delete for a whole title, which is the wrong granularity the
moment a title holds more than one episode. Every route below is a scoped
copy of the identical title-level route in `routes_titles.py`, keeping
the exact protection the original has (the same countdown, the same
confirmation, the same objects-before-rows ordering) rather than opening
a second deletion mechanism: `mark_episode_for_delete`/
`cancel_delete_episode`/`finish_delete_episode` are the episode-scoped
twins plan 02 already wrote, and `reaper.reap_once` (extended by this
plan) is the one sweep that collects both a title's own countdown and
every episode's.

The two check routes are the other half: an episode or an edition's own
verification page, reached one path segment deeper than the public
address that plays it, and read through the exact same address grammar
(`orchestrator.slugs.parse_public_address`) so a check address and a
playing address can never disagree about what a segment means. Both stay
behind the session gate -- the check page has never been public.
"""
import asyncio
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.reaper import reap_once
from orchestrator.resolve import _readiness_status_code
from orchestrator.slugs import AddressRefused, parse_public_address
from orchestrator.storage import delete_object
from orchestrator.web.templates import render

router = APIRouter()


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


# --- Task 1: an episode can be cancelled, retried and deleted on its own --


@router.post("/library/episode/{episode_id}/delete")
async def delete_episode_route(episode_id: str, request: Request):
    """Start the grace window on one episode alone (D-13, D-164) -- the
    episode-scoped twin of routes_titles.delete_route. Only an 'active'
    episode may be marked for deletion, the same T-02-10 guard the title-
    level route already applies one level up: an episode already counting
    down, already failed-and-dismissable, or already gone has no live
    address to protect a mouse slip against.
    """
    parsed = _parse_uuid(episode_id)
    if parsed is None:
        return Response(status_code=404)

    settings = get_settings()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.episode_for_action(conn, parsed)
        if row is None or row["episode_status"] != "active":
            return Response(status_code=404)
        await queries.mark_episode_for_delete(conn, parsed, settings.delete_grace_seconds)

    return RedirectResponse("/library", status_code=302)


@router.post("/library/episode/{episode_id}/undo")
async def undo_episode_route(episode_id: str, request: Request):
    """D-62's cancel, scoped to one episode -- only an episode currently
    pending deletion may be undone (T-02-10).
    """
    parsed = _parse_uuid(episode_id)
    if parsed is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.episode_for_action(conn, parsed)
        if row is None:
            return Response(status_code=404)
        if row["episode_status"] != "pending_delete":
            return Response(status_code=400)
        await queries.cancel_delete_episode(conn, parsed)

    return RedirectResponse("/library", status_code=302)


@router.post("/library/episode/{episode_id}/purge")
async def purge_episode_route(episode_id: str, request: Request):
    """D-62's skip-the-wait, scoped to one episode: reuses
    `mark_episode_for_delete` with a zero-second window rather than a
    second deletion path, then awaits one `reap_once` cycle inline -- the
    same sweep the title-level purge already awaits, extended by this
    plan to also collect episodes -- so the object is actually gone before
    this response returns. Only an episode already pending deletion may be
    purged (T-02-10): purge is reached from the countdown UI, which only
    ever shows for an episode already in that state.
    """
    parsed = _parse_uuid(episode_id)
    if parsed is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.episode_for_action(conn, parsed)
        if row is None:
            return Response(status_code=404)
        if row["episode_status"] != "pending_delete":
            return Response(status_code=400)
        await queries.mark_episode_for_delete(conn, parsed, 0)

    await reap_once(pool)

    return RedirectResponse("/library", status_code=302)


async def _cancel_never_active_episode(conn, episode_id) -> None:
    """The immediate half of an episode cancellation's hard delete
    (T-04-32, scoped one level down from
    routes_titles._cancel_never_active_title): objects before rows, the
    same ordering used there for the same reason (T-02-08).
    `finish_delete_episode` itself decides tombstone-or-drop from whether
    any of this episode's editions was ever active -- a job that was still
    queued had not yet made this episode's own edition active, so this
    call reaches the drop branch in practice, but the decision is made in
    the one place that rule already lives rather than duplicated here.
    """
    keys = await queries.episode_object_keys(conn, episode_id)
    for key in keys:
        await asyncio.to_thread(delete_object, key)
    await queries.finish_delete_episode(conn, episode_id)


@router.post("/library/episode/{episode_id}/cancel")
async def cancel_episode_route(episode_id: str, request: Request):
    """Stop an in-flight job for one episode's own default edition, and
    touch no sibling (D-129, D-164).

    Scoped to the default edition deliberately: the only case an episode
    row's own stage chip and Cancel control render for is that edition's
    own job (`episodes_for_library`'s live-job lookup and
    `_group_episodes_by_title`'s `job_stage`, both keyed off the episode a
    row belongs to) -- a second, non-default edition still preparing
    behind an already-ready episode is the known, documented gap plan 09's
    own summary already carries forward, unaffected by this plan.

    A job still queued has no worker to tell at all, so
    `queries.request_job_cancel` finishes it `cancelled` on the spot --
    this route notices that and hard-deletes the episode immediately, with
    no tombstone if it was never active (T-04-32): its address never went
    live. A running job only has its cancel flag set here; the row keeps
    polling until the consumer or the reaper's stale sweep finishes it.
    """
    parsed = _parse_uuid(episode_id)
    if parsed is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.episode_for_action(conn, parsed)
        if row is None or row["default_edition_id"] is None:
            return Response(status_code=404)

        job = await queries.job_for_edition(conn, row["default_edition_id"])
        if job is None:
            return Response(status_code=404)

        result = await queries.request_job_cancel(conn, job["id"])
        if result is not None and result["status"] == "cancelled":
            await _cancel_never_active_episode(conn, parsed)
            await queries.sync_title_status(conn, row["title_id"])

    return RedirectResponse("/library", status_code=302)


@router.post("/library/episode/{episode_id}/retry")
async def retry_episode_route(episode_id: str, request: Request):
    """Requeue one episode's own failed edition with the same choices and
    the same address (D-130, D-164) -- reads the failed default edition's
    own last job and enqueues a fresh one carrying the identical payload,
    then puts that edition back to 'preparing' via `queries.requeue_edition`
    (which also lifts the *title* out of 'failed' when every episode had
    failed -- see that function's own docstring). Only an episode whose
    own default edition is 'failed' has a Retry control to reach this from
    (T-02-10, one level down).
    """
    parsed = _parse_uuid(episode_id)
    if parsed is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.episode_for_action(conn, parsed)
        if row is None or row["edition_status"] != "failed":
            return Response(status_code=404)

        edition_id = row["default_edition_id"]
        last_job = await queries.last_job_for_edition(conn, edition_id)
        if last_job is None:
            return Response(status_code=404)

        async with conn.transaction():
            await queries.enqueue_job(
                conn,
                kind=last_job["kind"],
                title_id=row["title_id"],
                edition_id=edition_id,
                source_path=last_job["source_path"],
                audio_stream_index=last_job["audio_stream_index"],
                subtitle_kind=last_job["subtitle_kind"],
                subtitle_stream_index=last_job["subtitle_stream_index"],
                subtitle_path=last_job["subtitle_path"],
                stereo_layout=last_job["stereo_layout"],
            )
            await queries.requeue_edition(conn, edition_id)

    return RedirectResponse("/library", status_code=302)


@router.post("/library/episode/{episode_id}/dismiss")
async def dismiss_episode_route(episode_id: str, request: Request):
    """Hard-delete a failed episode and free every address it held
    immediately (D-61, D-164): nothing was ever published for it, so
    there is no old link to protect and no confirmation to ask for.

    **Objects before rows**, the same ordering
    routes_titles.dismiss_route already establishes for the same reason
    (T-02-08): `queries.episode_object_keys` only reads, the objects it
    names are deleted here first, and only then does
    `queries.finish_delete_episode` remove or tombstone the rows that were
    the only record of those keys. Only an episode whose own default
    edition is 'failed' may be dismissed (T-02-10, one level down).
    `sync_title_status` re-derives the title's own aggregate afterwards,
    the single place that rule lives (D-157).
    """
    parsed = _parse_uuid(episode_id)
    if parsed is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.episode_for_action(conn, parsed)
        if row is None or row["edition_status"] != "failed":
            return Response(status_code=404)

        keys = await queries.episode_object_keys(conn, parsed)
        for key in keys:
            await asyncio.to_thread(delete_object, key)

        await queries.finish_delete_episode(conn, parsed)
        await queries.sync_title_status(conn, row["title_id"])

    return RedirectResponse("/library", status_code=302)


# --- Plan 11: an edition can be made default, cancelled, and deleted on its
# --- own, one level down from the episode routes above ---------------------


@router.post("/library/edition/{edition_id}/make-default")
async def make_default_edition_route(edition_id: str, request: Request):
    """Move an episode's default-edition pointer to `edition_id` (D-178) --
    the operator's own explicit "Make default" click, the only other
    caller of `queries.set_default_edition` besides a job's own terminal
    write with `make_default_when_ready` set. Refused for anything but a
    ready (`active`), non-default edition: the pointer must never move to
    an edition still converting or one that has already failed, and a
    click on the edition that is already default is meaningless.
    """
    parsed = _parse_uuid(edition_id)
    if parsed is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.edition_for_action(conn, parsed)
        if row is None or row["edition_status"] != "active" or row["is_default"]:
            return Response(status_code=404)
        await queries.set_default_edition(conn, parsed)

    return RedirectResponse("/library", status_code=302)


@router.post("/library/edition/{edition_id}/delete")
async def delete_edition_route(edition_id: str, request: Request):
    """Start the grace window on one non-default edition alone (D-178) --
    the edition-scoped twin of `delete_episode_route`, one level down.
    Refused for the default edition (the control is disabled with its
    reason in the template, but the server refuses a crafted POST too,
    T-05-43) and for anything not currently `active`: a still-converting
    or already-failed non-default edition has no live address this
    countdown protects.
    """
    parsed = _parse_uuid(edition_id)
    if parsed is None:
        return Response(status_code=404)

    settings = get_settings()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.edition_for_action(conn, parsed)
        if row is None or row["edition_status"] != "active" or row["is_default"]:
            return Response(status_code=404)
        await queries.mark_edition_for_delete(conn, parsed, settings.delete_grace_seconds)

    return RedirectResponse("/library", status_code=302)


@router.post("/library/edition/{edition_id}/undo")
async def undo_edition_route(edition_id: str, request: Request):
    """D-62's cancel, scoped to one edition -- only an edition currently
    pending deletion may be undone.
    """
    parsed = _parse_uuid(edition_id)
    if parsed is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.edition_for_action(conn, parsed)
        if row is None:
            return Response(status_code=404)
        if row["edition_status"] != "pending_delete":
            return Response(status_code=400)
        await queries.cancel_delete_edition(conn, parsed)

    return RedirectResponse("/library", status_code=302)


@router.post("/library/edition/{edition_id}/purge")
async def purge_edition_route(edition_id: str, request: Request):
    """D-62's skip-the-wait, scoped to one edition: reuses
    `mark_edition_for_delete` with a zero-second window rather than a
    second deletion path, then awaits one `reap_once` cycle inline -- the
    same sweep the title- and episode-level purges already await, extended
    by this plan to also collect editions -- so the object is actually
    gone before this response returns. Only an edition already pending
    deletion may be purged.
    """
    parsed = _parse_uuid(edition_id)
    if parsed is None:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.edition_for_action(conn, parsed)
        if row is None:
            return Response(status_code=404)
        if row["edition_status"] != "pending_delete":
            return Response(status_code=400)
        await queries.mark_edition_for_delete(conn, parsed, 0)

    await reap_once(pool)

    return RedirectResponse("/library", status_code=302)


# --- Task 2: a check page for every episode and every edition --------------


def _layout_hint_key(stereo_layout: str | None) -> str | None:
    """The stereo-mode sentence's own catalog key (04-UI-SPEC.md Screen 4
    addendum), read straight off the resolved edition rather than
    re-derived from the file -- the identical rule
    routes_titles.check_page_route already applies to a title's default
    edition, applied here to whichever edition this exact address names.
    """
    if stereo_layout and stereo_layout.startswith("sbs"):
        return "check.layout_hint.sbs"
    if stereo_layout and stereo_layout.startswith("ou"):
        return "check.layout_hint.ou"
    return None


def _episode_check_href(slug: str, number: int, edition_slug: str | None) -> str:
    if edition_slug is not None:
        return f"/check/{slug}/{number}/{edition_slug}"
    return f"/check/{slug}/{number}"


async def _render_episode_check(
    request: Request, conn, *, slug: str, number: int, edition_slug: str | None, resolved
) -> Response:
    """Shared body of both routes below -- an episode's own check page, or
    an edition's, reached one path segment apart. Mirrors
    routes_titles.check_page_route's own shape (D-65: the video points at
    the public address, never a direct storage URL) one level down, and
    reuses that exact template rather than a second one.

    The navigation walks every episode of the title in number order,
    ready or not (D-158): no ready range is computed anywhere, so the
    links can never skip an episode the operator wanted to look at. It
    renders only when the title has more than one episode -- a film's
    check page (bare, or its own edition address) never gets it.
    """
    settings = get_settings()
    title_row = await queries.episode_with_title(conn, resolved["episode_id"])
    title_name = title_row["title_name"] if title_row is not None else slug
    library = await queries.library_row(conn, resolved["title_id"])
    episode_count = library["episode_count"] if library is not None else 1

    address = f"{settings.public_base_url}/v/{slug}/{number}"
    if edition_slug is not None:
        address += f"/{edition_slug}"

    show_episode_nav = bool(episode_count and episode_count > 1)
    previous_href = next_href = None
    previous_reason_key = next_reason_key = None
    if show_episode_nav:
        neighbours = await queries.episode_neighbours(
            conn, resolved["title_id"], number, edition_slug
        )
        if neighbours["previous_number"] is not None:
            keep_edition = edition_slug if neighbours["previous_has_edition_slug"] else None
            previous_href = _episode_check_href(slug, neighbours["previous_number"], keep_edition)
        else:
            previous_reason_key = "check.nav.first"
        if neighbours["next_number"] is not None:
            keep_edition = edition_slug if neighbours["next_has_edition_slug"] else None
            next_href = _episode_check_href(slug, neighbours["next_number"], keep_edition)
        else:
            next_reason_key = "check.nav.last"

    return render(
        request,
        "check.html",
        title_name=title_name,
        heading_kind="edition" if edition_slug is not None else "episode",
        episode_number=number,
        edition_slug=edition_slug,
        address=address,
        is_active=_readiness_status_code(resolved) is None,
        layout_hint_key=_layout_hint_key(resolved.get("stereo_layout")),
        show_episode_nav=show_episode_nav,
        previous_href=previous_href,
        next_href=next_href,
        previous_reason_key=previous_reason_key,
        next_reason_key=next_reason_key,
    )


@router.get("/check/{slug}/{segment}")
async def check_episode_route(slug: str, segment: str, request: Request):
    """A numbered episode's check page (.../check/{slug}/{number}), or a
    film's own alternative edition's (.../check/{slug}/{edition_slug}) --
    decided by `parse_public_address` from `segment`'s own shape, the
    identical grammar `orchestrator.resolve`'s public route reads (D-156,
    D-158), so a check address and a playing address can never disagree
    about what a segment means. Stays behind the session gate: the check
    page is the operator's own verification step and has never been
    public.
    """
    try:
        address = parse_public_address(f"{slug}/{segment}")
    except AddressRefused:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if address.number is not None:
            resolved = await queries.resolve_episode_slug(conn, address.slug, address.number)
            number = address.number
            edition_slug = None
        else:
            resolved = await queries.resolve_title_edition_slug(
                conn, address.slug, address.edition_slug
            )
            edition_slug = address.edition_slug
            number = resolved["number"] if resolved is not None else None

        if resolved is None:
            return Response(status_code=404)

        return await _render_episode_check(
            request,
            conn,
            slug=address.slug,
            number=number,
            edition_slug=edition_slug,
            resolved=resolved,
        )


@router.get("/check/{slug}/{number}/{edition_slug}")
async def check_episode_edition_route(
    slug: str, number: str, edition_slug: str, request: Request
):
    """An episode's own alternative edition's check page (D-158) -- the
    three-segment address a series episode gets once it, too, carries more
    than one edition. Read through `parse_public_address` exactly as the
    two-segment route above is.
    """
    try:
        address = parse_public_address(f"{slug}/{number}/{edition_slug}")
    except AddressRefused:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        resolved = await queries.resolve_episode_slug(
            conn, address.slug, address.number, edition_slug=address.edition_slug
        )
        if resolved is None:
            return Response(status_code=404)

        return await _render_episode_check(
            request,
            conn,
            slug=address.slug,
            number=address.number,
            edition_slug=address.edition_slug,
            resolved=resolved,
        )
