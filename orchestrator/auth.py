"""Everything the interface needs to know who is asking: the password
hash, the session token, the public path list and the gate.

The gate and the public path constants land in a later plan; this module
carries only the cryptographic primitives and the session query surface's
shared constants. The hashing calls are deliberately expensive -- that is
the whole point of a password hash -- and therefore never run on the
event loop this process shares with every viewer's `/v/{slug}` request.
"""
import asyncio
import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor

import argon2
import argon2.exceptions

# The sliding window (D-81): one constant both the cookie's Max-Age and
# the database's expires_at are built from, so the two can never disagree.
SESSION_LIFETIME_SECONDS = 30 * 24 * 3600

# The window is not rewritten until its remaining life has slipped inside
# this much of the full thirty days -- one day of slack. Writing on every
# request would make the sliding window the busiest writer in the system
# for a session this long-lived; see validate_and_renew_session in
# orchestrator/db/queries.py for the throttled query this constant feeds.
SESSION_RENEW_AFTER_SECONDS = 24 * 3600

SESSION_COOKIE_NAME = "mytube_session"

# D-87's constant delay on every failed login attempt. No lockout and no
# attempt table: a lockout would let any passer-by lock the operator out
# of their own interface by hammering the form with their username.
FAILED_ATTEMPT_DELAY_SECONDS = 1.0

# Explicit cost parameters rather than the library defaults, which are
# platform-dependent. A starting point measured against nothing yet -- a
# single verify should land near 250-500 ms on the real deploy machine;
# a later plan benchmarks these on the deploy host and adjusts. Note that
# `max_workers` below (the hashing executor) and `memory_cost` here
# multiply: raising one after that measurement means looking at the other.
_hasher = argon2.PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

# A dedicated executor for the two hashing calls, not `asyncio.to_thread`.
# `to_thread` uses the event loop's single default executor, and in this
# process that executor is shared by every probe, every transcode call,
# every S3 read, the poster reader and the startup sweep. The login route
# is public, unauthenticated by definition, and each verification holds
# its worker for the better part of a second and its configured memory for
# the whole of it. A dozen concurrent attempts on the default executor
# would occupy every worker and hold gigabytes transiently -- in the same
# process that answers /v/{slug} for viewers. Two workers bounds both
# numbers: at most twice `memory_cost` is resident for hashing at any
# moment, and a burst queues on this executor instead of on the one
# delivery shares. The edge's rate limit is the other half of that answer
# and lives in the edge configuration; this half is what survives a
# misconfigured edge.
_HASH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="argon2")


async def hash_password(password: str) -> str:
    """Hash `password` with argon2id, off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_HASH_EXECUTOR, _hasher.hash, password)


async def verify_password(stored_hash: str, password: str) -> bool:
    """Verify `password` against `stored_hash`, off the event loop.

    Returns False for a mismatch or a malformed stored hash; never raises
    on either. Any other error (e.g. an out-of-memory hashing failure)
    propagates.
    """
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_HASH_EXECUTOR, _hasher.verify, stored_hash, password)
        return True
    except (argon2.exceptions.VerifyMismatchError, argon2.exceptions.InvalidHashError):
        return False


# A throwaway hash computed once at import, for verify_absent_credential
# below -- never a real operator's hash.
DUMMY_HASH = _hasher.hash("mytube-dummy-hash-for-timing-only")


async def verify_absent_credential() -> None:
    """Run a dummy verify and discard the result.

    When the username is not found there is nothing to verify against,
    and returning immediately would make "no such user" measurably faster
    than "wrong password". Not required by any locked decision; it costs
    four lines and closes a timing distinction that would otherwise tell
    a stranger which half of the credential they guessed right.
    """
    await verify_password(DUMMY_HASH, "irrelevant")


def new_session_token() -> tuple[str, str]:
    """Mint a fresh session token.

    The raw value goes into the cookie and is never stored; the digest
    goes into the row and is never enough to sign in with.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_session_token(raw)


def hash_session_token(raw: str) -> str:
    """The same digest new_session_token uses, so the gate can hash an
    incoming cookie without re-deriving the recipe.
    """
    return hashlib.sha256(raw.encode()).hexdigest()
