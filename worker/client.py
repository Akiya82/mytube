"""The converter's only way to reach the queue -- one class wrapping the
five conversations `orchestrator/web/routes_worker.py` answers.

Every call carries the credential in the authorization header, the
protocol version and the converter's own name (D-111, D-117). The claim
returns nothing on an empty-queue answer rather than raising. Every call
retries a genuine network failure and a server error (5xx) with a
backoff that grows and is capped, giving up at a bounded window and
raising then (D-116) -- a server error joined the retryable set in this
plan, since a transient database failure at the moment of reporting is
exactly the condition this retry exists for, and it must not cost a
finished GPU encode. A refusal of the credential (401), a conflict
answer (409), a not-found answer (404) and a protocol mismatch (400) are
each raised immediately and never retried: a wrong credential is not a
transient condition, a conflict means the job has already moved on so
the correct response is cleanup, a not-found on a heartbeat means this
attempt no longer owns the job at all, and a protocol mismatch will not
resolve on the next poll either -- only redeploying one half or the
other changes the answer.

Nothing here ever logs the credential or the authorization header.
"""
import asyncio

import httpx

WORKER_PROTOCOL_VERSION = 2

# D-116: "retries the heartbeat and the final report for a bounded time".
# No existing precedent in this codebase names a duration -- this is this
# module's own reasonable default (04-RESEARCH.md's own Assumptions Log
# flags exactly this as a planner decision, not a verified requirement):
# long enough that a brief network blip or an orchestrator pod restart
# does not throw away a finished GPU encode, short enough that it does
# not delay the reaper's own stale-job recovery beyond JOB_STALE_SECONDS.
REPORT_RETRY_WINDOW_SECONDS = 300.0
RETRY_BACKOFF_CAP_SECONDS = 30.0
_INITIAL_BACKOFF_SECONDS = 1.0


class ReportRejected(Exception):
    """A 409: the job moved on to a different attempt -- the reaper
    already requeued it, or another worker's report was accepted first.
    Never retried; this is the signal to clean up (abort any dangling
    multipart upload of its own) rather than to persist.
    """


class AuthenticationRejected(Exception):
    """A 401: the presented credential was refused. Never retried -- a
    wrong credential is not a transient condition, and retrying it only
    delays the operator learning about it.
    """


class JobLost(Exception):
    """A 404: the service says this attempt no longer owns the job it
    thinks it is still working -- the reaper requeued it, or another
    attempt already claimed and finished it. Raised only by the heartbeat
    call in practice, since that is the one conversation the service can
    answer this way mid-run; the caller's response is the same as an
    operator's own cancel: stop the encode, and report nothing further for
    this attempt.
    """


class ProtocolMismatch(Exception):
    """A 400 whose body names a protocol mismatch -- this converter and
    the service disagree on the wire shape they speak. Carries both
    numbers so the caller can say, in one line, which is which; never
    retried, since the numbers do not change on the next poll -- only
    redeploying the two together does.
    """

    def __init__(self, *, client_protocol: int, service_protocol: int) -> None:
        self.client_protocol = client_protocol
        self.service_protocol = service_protocol
        super().__init__(
            f"this converter speaks protocol {client_protocol}, "
            f"the service speaks protocol {service_protocol} -- "
            "deploy the converter and the service together"
        )


class WorkerClient:
    """One HTTP client for the whole converter, over the service's
    worker-facing prefix. `transport` is accepted so tests can substitute
    `httpx.MockTransport` with no network at all.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        worker_name: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._worker_name = worker_name
        # Every worker-facing route on the service lives under one prefix;
        # callers hand over the service's own address and never spell it.
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/worker",
            headers={"authorization": f"Bearer {token}"},
            transport=transport,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _body(self, **fields) -> dict:
        body = {"protocol": WORKER_PROTOCOL_VERSION, "worker": self._worker_name}
        body.update(fields)
        return body

    async def _post(self, path: str, json_body: dict) -> httpx.Response:
        """POST with the bounded, growing-backoff retry every call shares.

        Retried: a genuine transport failure (the network refusing the
        attempt, a name lookup failure, a timeout) and a server error
        (5xx) -- the second joined this plan, since a transient database
        failure at the moment of reporting is exactly the condition this
        retry exists for. Not retried: a definite 4xx answer (401, 404,
        409, 400) is handled by the caller immediately, once, without
        going through this retry loop again -- none of those resolve
        themselves on a second attempt.

        The window is tracked against the sum of backoff durations
        actually slept, not wall-clock time -- a caller substituting a
        no-op `asyncio.sleep` (as the test suite does) still exercises
        the real give-up boundary instead of looping for real minutes.
        """
        elapsed = 0.0
        backoff = _INITIAL_BACKOFF_SECONDS
        while True:
            try:
                response = await self._client.post(path, json=json_body)
            except httpx.TransportError:
                if elapsed >= REPORT_RETRY_WINDOW_SECONDS:
                    raise
            else:
                if response.status_code < 500 or elapsed >= REPORT_RETRY_WINDOW_SECONDS:
                    return response
            sleep_for = min(backoff, RETRY_BACKOFF_CAP_SECONDS)
            await asyncio.sleep(sleep_for)
            elapsed += sleep_for
            backoff = min(backoff * 2, RETRY_BACKOFF_CAP_SECONDS)

    async def _post_and_decide(self, path: str, json_body: dict) -> httpx.Response:
        response = await self._post(path, json_body)
        if response.status_code == 401:
            raise AuthenticationRejected("the presented worker credential was refused")
        if response.status_code == 409:
            raise ReportRejected(f"{path}: the job has moved on to a different attempt")
        if response.status_code == 404:
            raise JobLost(f"{path}: this attempt no longer owns the job")
        if response.status_code == 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            if body.get("error") == "protocol_mismatch":
                raise ProtocolMismatch(
                    client_protocol=WORKER_PROTOCOL_VERSION,
                    service_protocol=body.get("expected_protocol"),
                )
        response.raise_for_status()
        return response

    async def claim(self, *, status: str = "idle") -> dict | None:
        """One claim, at most one job. `status` is `"idle"` (ready) or
        `"waiting_for_gpu"` (D-113) -- either way the check-in is
        recorded; an empty queue answers no-content, returned here as
        `None` rather than raised.
        """
        response = await self._post_and_decide("/claim", self._body(status=status))
        if response.status_code == 204:
            return None
        return response.json()

    async def heartbeat(
        self,
        job_id,
        *,
        attempt: int,
        stage: str,
        progress_pct: float | None = None,
        eta_seconds: int | None = None,
        realtime_factor: float | None = None,
    ) -> bool:
        """Reports progress, answers with the cancellation flag."""
        response = await self._post_and_decide(
            f"/jobs/{job_id}/heartbeat",
            self._body(
                attempt=attempt,
                stage=stage,
                progress_pct=progress_pct,
                eta_seconds=eta_seconds,
                realtime_factor=realtime_factor,
            ),
        )
        return bool(response.json().get("cancel_requested", False))

    async def done(
        self,
        job_id,
        *,
        attempt: int,
        object_key: str,
        source_codec: str,
        source_profile: str | None,
        subtitle_burned_language: str | None,
        subtitle_burned_title: str | None,
        size_bytes: int,
        duration_seconds: float,
        width: int,
        height: int,
        fps: float,
        video_profile: str,
        video_level: int | None,
        pix_fmt: str,
        audio_codec: str,
        audio_sample_rate: int,
        audio_channel_layout: str,
        audio_language: str | None,
        audio_title: str | None,
        audio_reencoded: bool,
        poster_key: str | None,
        realtime_factor: float,
    ) -> None:
        """A finished transcode. The body widened past `attempt`/
        `object_key` in this plan: the route's own `DoneBody` needs what
        the conversion measured to fill the library row's detail line and
        its poster, and every field name here matches the route's own,
        so nothing between the wire and the write has to translate. See
        `orchestrator/web/routes_worker.py`'s `DoneBody` for which of
        these may still be sent as an explicit `None`.
        """
        await self._post_and_decide(
            f"/jobs/{job_id}/done",
            self._body(
                attempt=attempt,
                object_key=object_key,
                source_codec=source_codec,
                source_profile=source_profile,
                subtitle_burned_language=subtitle_burned_language,
                subtitle_burned_title=subtitle_burned_title,
                size_bytes=size_bytes,
                duration_seconds=duration_seconds,
                width=width,
                height=height,
                fps=fps,
                video_profile=video_profile,
                video_level=video_level,
                pix_fmt=pix_fmt,
                audio_codec=audio_codec,
                audio_sample_rate=audio_sample_rate,
                audio_channel_layout=audio_channel_layout,
                audio_language=audio_language,
                audio_title=audio_title,
                audio_reencoded=audio_reencoded,
                poster_key=poster_key,
                realtime_factor=realtime_factor,
            ),
        )

    async def failed(self, job_id, *, attempt: int, reason_key: str) -> None:
        """A conversion the converter gave up on. `reason_key` must be
        one of the service's own allowed catalog keys
        (`ALLOWED_FAILURE_REASONS`) -- an unknown key is refused by the
        service (400), never sent as a raw ffmpeg message.
        """
        await self._post_and_decide(
            f"/jobs/{job_id}/failed", self._body(attempt=attempt, reason_key=reason_key)
        )

    async def cancelled(self, job_id, *, attempt: int) -> None:
        """Confirms a cancellation the converter learned of from an
        earlier heartbeat's own answer.
        """
        await self._post_and_decide(
            f"/jobs/{job_id}/cancelled", self._body(attempt=attempt)
        )
