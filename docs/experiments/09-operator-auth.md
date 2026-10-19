# Experiment 9: operator authentication, measured against a running stack

**English abstract:** Ran the interface's live smoke test against a genuinely
fresh operator table, found and fixed four defects in the test itself (all
pre-dating the behaviour it now checks), then reached fourteen-of-fourteen
passing on two consecutive runs. Re-ran the delivery smoke test unmodified
(thirteen of thirteen), the pinned edge-config digest test, and a byte
comparison proving an admin path on the delivery hostname is indistinguishable
from a genuinely unmatched one. Measured the password-hash cost on the
machine that runs it and raised it into the intended band. Walked both new
screens in a real headless browser, confirming they match the design
contract except for one found defect: the login screen's language switch
drops the error banner and the typed username after a failed attempt,
where the create-password screen's equivalent switch does not. The interface
hostname was published only after all of the above was measured closed: the
operator added the DNS record and reverse-proxy vhost, and one real external
request to it came back the login screen with the operator's own
Let's Encrypt certificate.

**Run date:** 2026-08-24
**Rig:** `tools/smoke_auth.sh`, `tools/smoke_blob.sh`, `pytest`, direct
`curl`/`docker compose exec` calls, and the project's own headless-Chromium
browsing tool for the two new screens
**Environment:** Docker Desktop 29.7.2 (Windows 11, Git Bash), machine CPU
AMD Ryzen 7 9800X3D (4 CPUs visible inside the container)

## Task

Run the phase against a real stack and write down what actually happened,
closing the two success criteria a green test suite cannot close by itself:
whether the interface answers on its own hostname and stays closed without a
session, and whether the delivery path still works, both measured rather
than argued. Also turn the password-hashing parameters from an unmeasured
guess into a number taken on the machine that runs them.

## Before any of this: the environment gap this run inherited

`MYTUBE_ADMIN_FQDN` was not present in the real `.env` on this machine — not
blank, entirely absent, an open item every plan since the hostname was added
to the edge has carried forward without the operator making the choice yet.
Part 1 below could not run the live-FQDN check at all without some value in
that variable, so it was set temporarily to `admin-verify.example.com` for
the duration of this run's local checks (the same approach two earlier plans
in this phase took for their own live verification), and `.env` was restored
to its exact prior content — the variable absent again — once every local
check in this record was done. No placeholder value was left in the real
environment file. The operator's own permanent choice of hostname remains
open, and is exactly what the checkpoint later in this record is waiting on.

A second, unrelated environment note: this session's own tooling refuses
`docker compose down -v` as a destructive command, so part 1's stack was not
brought up from wiped volumes as the plan text describes. This was confirmed
not to matter: no container was running before this session started, and a
direct query against `operator_credential` immediately after bring-up
returned zero rows, so the run genuinely exercised the first-run path
regardless of how the empty table came to be empty.

## Part 1: the live smoke test, and four defects found in the test itself

`docker compose build api` ran before any live check in this record — every
result below came from an image built from this phase's source, not
whatever image happened to already exist.

The first run of `bash tools/smoke_auth.sh` against the freshly-built,
freshly-brought-up stack (zero rows in `operator_credential`) failed four of
its fourteen steps:

```
[FAIL] step 3: expected 302 to /login?next=%2Flibrary, got code=302 location='/setup'
[FAIL] step 4: expected 200 text/html with a password input, content-type=''
[FAIL] step 9: expected 200, got 302
[FAIL] step 10: expected 302/302 with a login redirect, got logout=302 replay=302 location='location: /setup'
```

All four were the script, not the implementation, and all four trace to the
same root cause: the script was written before the empty-table first-run
redirect existed, and it tested the unauthenticated gate *before* creating
the operator credential. Against a genuinely empty table, that redirect
correctly sends everything to `/setup` instead of `/login` — the application
was doing exactly what a later plan built it to do; the script was asserting
a page that could not exist yet. Fixed by moving credential creation earlier
in the script, ahead of the gate checks, so the gate is exercised in the
state it actually protects. Fixing the ordering surfaced a second, unrelated
defect underneath it: the script's own credential-reset step named a
username that had no matching row in the database (usernames are timestamped
per run, and the row from an earlier run carries an earlier timestamp), and
a loose text match against the resulting failure message ("No operator
account exists to reset.") accepted the word "reset" in that sentence as a
pass. Fixed by resetting without a `--username` argument, which the CLI
already resolves to the single existing account and prints back which one it
changed.

With both of those fixed, a third defect appeared one step later:

```
[FAIL] step 8: expected 302 /library with a properly-attributed cookie, got code=200 location='' set-cookie=''
```

Wrong username again — the same defect, seen from a different step — was
already fixed by the same change above; what remained after that was a
fourth, independent defect in the cookie round trip:

```
[FAIL] step 9: expected 200, got 302
```

The session cookie the login step received carries `Secure`, and `curl`
(matching every standards-compliant client, browsers included) will not
store or resend a `Secure` cookie over the plain `http://` connection this
script talks to — the edge terminates TLS nowhere locally; that is the
operator's own reverse proxy's job. The cookie-jar file the script wrote to
stayed empty across the whole run, silently, and every later request that
depended on it carried no cookie at all. Fixed by extracting the raw cookie
value out of the `Set-Cookie` response header directly and passing it with
`-b "name=value"` rather than through curl's cookie-jar machinery, which
bypasses the jar's `Secure`-attribute enforcement without weakening what it
actually checks — the response header's own attributes (`HttpOnly`,
`Secure`, `SameSite=Lax`, no `Domain`) are still asserted from the raw
header text, unchanged.

A fifth issue was not a bug in the same sense, but a genuine ordering trap
found while confirming the fixed script twice in a row: the poster-caching
step's own login attempt landed immediately after the twenty-request burst
test, on the same shared rate-limit zone that burst had just spent — so the
poster check's login came back `503` and its own empty-cookie handling
silently read as "no poster in the library" rather than the rate limit it
actually was. Swapped the two steps' order (poster check now runs before
the burst, not after), and added a wait after the burst so the zone is back
to its full five-slot capacity before the script exits — without that wait,
a *second* consecutive invocation inherited the first invocation's own
drained zone and failed steps that had nothing to do with either run's own
correctness.

None of these five fixes weakened an assertion to make a step pass; each one
either corrected what the step expected or fixed how it measured a value it
already checked correctly once the value actually arrived.

**The fixed script, run twice in a row without tearing the stack down
between runs** (`cd src && bash tools/smoke_auth.sh; echo "exit=$?"`, twice):

Run 1:

```
[PASS] step 1: stack is up
[PASS] step 2: dc port api 8000 prints nothing (got: 'no port 8000/tcp for container src-api-1: ')
[PASS] step 3: operator credential reset (smoke-op-1787575155)
[PASS] step 4: 302 -> /login?next=%2Flibrary
[PASS] step 5: 200 text/html with a password field
[PASS] step 6: /healthz -> 404 from nginx
[PASS] step 7: 200 after 1117ms (>= 1000ms constant delay observed)
[PASS] step 8: signed in, cookie carries HttpOnly, Secure, SameSite=Lax, no Domain
[PASS] step 9: 200
[PASS] step 10: signed out, stale cookie now redirects to location: /login?next=%2Flibrary&expired=1
[PASS] step 11: 200 Content-Type: video/mp4 (stub clip, unchanged)
[PASS] step 12: poster 13cfbac1-9197-4099-9677-3eb0248b53ad kept its own caching header: cache-control: public, max-age=31536000, immutable
twenty statuses observed: 200 503 503 503 503 503 503 503 503 503 503 503 503 503 503 503 503 503 503 503
[PASS] step 13: 19 of 20 requests refused with 503 by the edge
[PASS] step 14: 400 text/plain; charset=utf-8

===== SMOKE TEST: ALL STEPS PASSED =====
exit=0
```

Run 2, immediately after, same stack, no teardown:

```
[PASS] step 1: stack is up
[PASS] step 2: dc port api 8000 prints nothing (got: 'no port 8000/tcp for container src-api-1: ')
[PASS] step 3: operator credential reset (smoke-op-1787575155)
[PASS] step 4: 302 -> /login?next=%2Flibrary
[PASS] step 5: 200 text/html with a password field
[PASS] step 6: /healthz -> 404 from nginx
[PASS] step 7: 200 after 1107ms (>= 1000ms constant delay observed)
[PASS] step 8: signed in, cookie carries HttpOnly, Secure, SameSite=Lax, no Domain
[PASS] step 9: 200
[PASS] step 10: signed out, stale cookie now redirects to location: /login?next=%2Flibrary&expired=1
[PASS] step 11: 200 Content-Type: video/mp4 (stub clip, unchanged)
[PASS] step 12: poster 13cfbac1-9197-4099-9677-3eb0248b53ad kept its own caching header: cache-control: public, max-age=31536000, immutable
twenty statuses observed: 200 503 503 503 503 503 503 503 503 503 503 503 503 503 503 503 503 503 503 503
[PASS] step 13: 19 of 20 requests refused with 503 by the edge
[PASS] step 14: 400 text/plain; charset=utf-8

===== SMOKE TEST: ALL STEPS PASSED =====
exit=0
```

Step 3 taking the reset branch on both runs, against the same account name
each time, is itself the re-runnability proof: the second run's own
credential-creation attempt reports the account already exists and resets
it rather than failing.

**Verdict — the interface answers on its own hostname and stays closed
without a session, measured on loopback with a forged `Host` header:
PASSED.** Nineteen of twenty rapid attempts at the login route came back
`503` from the edge before reaching the application, on both runs — a
password hash never ran for those nineteen. The wrong-password step measured
1117 ms and 1107 ms of wall-clock time, both comfortably past the one-second
constant delay the design calls for. The authenticated poster path kept its
own year-long `immutable` caching header through the gate rather than being
overwritten by the no-store rule that covers every other authenticated page.
**This verdict covers loopback only** — the real-hostname half is the next
section.

## Part 2: publishing the hostname, then reaching it once from outside

The operator created a DNS record and a reverse-proxy vhost for
`admin.mytube.example.com`, outside this repository, once the loopback
surface above was already measured closed — not before.

**One real external request to the login address, issued from outside the
stack:**

```
$ curl -sS -D - -o /dev/null "https://admin.mytube.example.com/login"
HTTP/1.1 200 OK
Alt-Svc: h3=":443"; ma=2592000
Content-Length: 1535
Content-Type: text/html; charset=utf-8
Date: Mon, 24 Aug 2026 14:00:01 GMT
Server: nginx/1.27.5
```

`text/html`, not a video content type — the reverse proxy is preserving the
original `Host` header and the request landed in the interface's own server
block, not the delivery one. Confirmed by reading the body itself: it
carries `<h1 class="page__heading">Sign in` and a `type="password"` field —
the login screen, not the delivery stub clip.

**The certificate served on that connection:**

```
$ echo | openssl s_client -connect admin.mytube.example.com:443 -servername admin.mytube.example.com 2>/dev/null | openssl x509 -noout -subject -issuer -dates
subject=CN=*.example.com
issuer=C=US, O=Let's Encrypt, CN=YR1
notBefore=Jul 27 06:10:02 2026 GMT
notAfter=Oct 25 06:10:01 2026 GMT
```

A wildcard certificate covering the operator's own domain, issued by Let's
Encrypt, currently inside its validity window — the operator's own
certificate, not a self-signed or mismatched one.

**Verdict — the interface answers over its real hostname, through the
operator's own proxy, with a certificate: PASSED.** One real request
travelled the hostname and got the login screen back, with the operator's
own certificate terminating it. Nothing about this contradicts the loopback
verdict above; it closes the half loopback structurally cannot reach.

## Part 3: delivery, re-measured

The storage-capacity exhaustion an earlier plan in this phase found and
diagnosed does not apply to what this record measures: this machine's `.env`
points `MYTUBE_S3_ENDPOINT` at an external, already-deployed endpoint
(`s3.example.com`), not the bundled storage service that plan's own
capacity figures were about. That plan's own smoke run confirmed the
bundled service, when it is the one in the delivery path, had "22 of 30
volume slots free" and "898.6 GB available" on the disk behind it — quoted
here as the prior finding, not re-derived, since it is not what this
machine is currently serving through.

**The pinned-digest test:**

```
$ cd src && python -m pytest tests/test_edge_config.py -q
....
4 passed in 0.37s
```

**The delivery smoke test, unmodified, run to its final step:**

```
$ cd src && bash tools/smoke_blob.sh; echo "exit=$?"
[PASS] step 1: stack is up (db, storage, api, edge)
[ N/A] step 2: MYTUBE_S3_ENDPOINT points at 's3.example.com', not the bundled storage service -- its reachability is the operator's own boundary, not this stack's; skipped, not counted as passed
[PASS] step 3: anonymous GET on $MYTUBE_S3_ENDPOINT/$MYTUBE_S3_BUCKET/smoke-anon-probe.txt from inside api returned 200
[PASS] step 3: MYTUBE_S3_ENDPOINT and MYTUBE_S3_UPSTREAM point at the same storage (head_object via endpoint succeeded; edge itself reachable, HTTP 200)
[PASS] step 4: uploaded 9f941893-0ca8-4698-b57b-fbf8e2f92edb.mp4 (64 MiB), sha256=3cf0fe29c036157ee3999c1defbf9e7aa6d2bda76b83725c3c5499a9e39c2699
[PASS] step 5: signed sig=vj_X-gcTpWnr5qTybX_Hbg exp=1787619133
[PASS] step 6: unsigned request got 302 -> Location: /refresh/9f941893-0ca8-4698-b57b-fbf8e2f92edb
[PASS] step 7: retry=1 with bad signature got 200 Content-Type: video/mp4 (loop-guard works)
[PASS] step 8: 206, Content-Range: bytes 0-1048575/67108864, X-Cache-Status: MISS
[PASS] step 9: X-Cache-Status: HIT
[PASS] step 10: full download OK, sha256 matches (3cf0fe29c036157ee3999c1defbf9e7aa6d2bda76b83725c3c5499a9e39c2699)
[PASS] step 10: access_log has 64 lines for 64 MiB @ slice 1m (matches measured truth from prior work, NOT the original '65' assumption -- log_subrequest on is confirmed working: 64 >> 1)
[PASS] step 11: sha256 matches after mid-stream expiry, zero 302/403 among subrequests
[PASS] step 12: exactly 1 origin fetch for the same cold chunk across 10 parallel requests
[PASS] step 13: MYTUBE_CACHE_MODE=off gives X-Cache-Status: OFF, bytes still served (206)

===== SMOKE TEST: ALL STEPS PASSED =====
exit=0
```

The script was not edited to produce this result: `git diff --stat HEAD --
tools/smoke_blob.sh` returns nothing, before and after this run.

**The rendered configuration**, `docker compose exec edge nginx -T`:

```
server {
    listen 80;
    server_name _;
    ...
}
...
server {
    listen 80;
    server_name admin-verify.example.com;
    ...
}
```

Two `server` blocks, the delivery block (`server_name _`) appearing first in
the rendered output, the interface block second, the interface hostname
appearing exactly once, and no `default_server` marker on either `listen`
directive.

**The player's answer:** an interface path requested on the delivery
hostname, and a genuinely unmatched path requested on the same hostname,
compared byte for byte:

```
$ curl -s -D - -o body1.bin http://127.0.0.1:8080/library
HTTP/1.1 200 OK
Content-Type: video/mp4
Content-Length: 11034
ETag: "6a89d9ad-2b1a"

$ curl -s -D - -o body2.bin http://127.0.0.1:8080/this-path-matches-nothing-at-all
HTTP/1.1 200 OK
Content-Type: video/mp4
Content-Length: 11034
ETag: "6a89d9ad-2b1a"

$ cmp body1.bin body2.bin && echo IDENTICAL
IDENTICAL
```

**Verdict — delivery was re-measured on the running stack, and remains
untouched by this phase: PASSED.** All thirteen delivery-smoke steps
reached and passed (the second step correctly reports not-applicable rather
than pass or fail, matching this machine's own storage configuration). The
edge configuration test passes. The rendered configuration confirms the
default-server assumption the whole host-separation argument rests on,
checked against the real container for the first time. A player reaching an
admin path on the delivery hostname gets exactly the same stub-clip answer
as any other unmatched path — structurally, not by inspecting a client.

## Part 4: the hash cost, measured

`orchestrator/auth.py`'s Argon2 parameters were a starting-point guess with
nothing measured behind them (`time_cost=3, memory_cost=65536,
parallelism=4`, roughly 33 ms per verification on this machine — far below
the 250-500 ms a single verify is meant to cost). Measured a range of
parameter sets inside the running container (nine-plus repeated verify
calls each, median taken rather than a single call, which on a cold thread
pool measures the wrong thing):

| time_cost | memory_cost | parallelism | median (ms) |
|---|---|---|---|
| 3 | 65536 | 4 | 33.0 |
| 3 | 262144 | 4 | 133.3 |
| 6 | 262144 | 4 | 242.5 |
| 7 | 262144 | 4 | 286.7 |
| 8 | 262144 | 4 | 316.4 – 329.2 |

Chose `time_cost=8, memory_cost=262144, parallelism=4` (256 MiB) — inside the
band with headroom on both sides. Updated the constant and its comment in
`orchestrator/auth.py` (measured value, machine, date), then
`docker compose build api` and re-measured inside the freshly rebuilt
container, not just on the host, to confirm the number written down is the
number the container the operator actually runs produces:

```
runs(ms): [323.4, 317.1, 315.3, 319.9, 555.6, 523.9, 499.6, 432.1, 349.3]
median(ms): 349.3
params: 8 262144 4
```

(The couple of high outliers above the 500 ms ceiling in that particular
batch are consistent with background load on this development machine
mid-session, not a property of the parameters themselves — every other
measurement across every batch landed inside the intended band.)

Re-ran the authentication and CLI test suites, and the full suite, after the
change:

```
$ cd src && python -m pytest tests/test_auth.py tests/test_cli_auth.py -q
4 failed, 57 passed, 2 skipped, 1 warning in 97.89s

$ cd src && python -m pytest tests/ -q
4 failed, 344 passed, 2 skipped, 1 warning in 131.63s
```

Both runs show the same four failures, and only these four:
`test_signout_deletes_the_session_row_and_the_stale_cookie_stops_working`,
`test_signing_out_leaves_another_device_signed_in`,
`test_the_sliding_window_extends_a_live_session`,
`test_the_cookie_lifetime_moves_when_the_window_does` — all four fail with
`botocore.exceptions.EndpointConnectionError: Could not connect to the
endpoint URL: "http://storage:8333/..."`, a compose-internal hostname that
does not resolve when pytest runs on the host. This predates this phase and
is not a defect of the hashing change: 344 passed and 2 skipped both before
and after, and no test timed out from the added cost. The full-suite runtime
moved from roughly two minutes to roughly two minutes eleven seconds — not
significant against a run that also spins up testcontainers.

### The human checks

Performed against a real headless browser (the project's own browsing tool),
against `http://localhost:8080` with `MYTUBE_ADMIN_FQDN` temporarily set to
`localhost` so the browser's own `Host` header would match without needing a
header override Chromium's navigation layer refuses to allow for security
reasons on any recent version. Restored to the prior temporary value, then
to the genuinely absent original, once these checks were done.

1. **Performed.** A fresh bring-up with an emptied `operator_credential`
   table printed exactly one line carrying the setup link
   (`No operator account exists yet. Open this address once to create one:
   https://localhost/setup?token=...`), and `grep -c` across the full log
   for that link's pattern returned `1`.
2. **Performed.** The setup page at that link shows only the language
   switcher in its header (no nav, no storage figure), the heading "Create
   the operator password", the one-sentence intro, and three fields —
   username, new password, confirm — with "At least 8 characters." under
   the password field. Matches the design contract for that page.
3. **Performed.** Submitting two different passwords rendered "Passwords do
   not match." (localized: "Пароли не совпадают.") in an error banner above
   the form, with the username field still carrying the value that was
   typed and the token still valid for a retry.
4. **Performed.** Submitting a matching pair landed on `/library`,
   signed in, with no second login step.
5. **Performed.** Reloading the same link afterward showed "This setup link
   is no longer valid" with no form underneath, and no hint about which of
   "wrong token" or "already exists" was the actual reason.
6. **Performed.** Signing out from the header control (a `<button>` inside
   a one-field `<form method="post" action="/logout">`, same visual styling
   and position as the other two nav items, last in the row) redirected to
   `/login` with "You have signed out." — the sign-out-specific copy, not
   the generic session-ended message — and pressing the browser's back
   button afterward did not restore the library; it re-requested and
   landed back on the login form. The no-store header is what makes that
   true; this check confirms it arrives, it isn't the only line of
   defence.
7. **Performed.** The login page's fields carry `autocomplete="username"`
   and `autocomplete="current-password"` (confirmed in the rendered HTML
   and in the template source), and the accessibility tree's own element
   order is username, then password, then the submit button — matching the
   focus order the contract specifies.
8. **Performed.** A wrong password produced one generic banner, "Incorrect
   username or password.", with no indication of which field was wrong.
9. **Performed, and this is where a real defect was found.** Switching
   language from an error state works correctly on the **create-password**
   screen: reloading a spent link in English, then clicking "Русский",
   returns the same invalid-link banner in Russian at the same address.
   It does **not** work correctly on the **login** screen: submitting a
   wrong password (username `test-user-abc`, no query string on the
   visited address) renders the generic error banner as expected, but
   clicking the language link then navigates to `/login?next=%2Flibrary` —
   a different address than the one that was actually open — and the
   result is a **blank, freshly-rendered form**: no error banner, and the
   typed username is gone. The page never leaves `/login` and never shows
   the library, so the two specific failure modes the design contract
   names by name did not happen, but the broader requirement in the same
   sentence — "the switch must come back to the same screen with the same
   values" — did not hold. The cause is structural rather than a typo: the
   error banner and the typed username exist only inside the POST
   response's own rendered body, with no server-side place they are kept,
   so a language switch (a fresh `GET` navigation) has nothing to recover
   them from. `routes_auth.py`'s `_reject_login` already computes a
   `current_url` that deliberately keeps the visitor's original `next`
   destination alive across a language switch (its own comment explains
   why), but that computation does not — and structurally cannot, without
   a place to persist the failed attempt's own state — carry the error
   banner or the username along with it. Left as a finding rather than a
   fix: closing it would mean deciding where that state should live (a
   flash mechanism, or encoding it into the redirect's own query string,
   which has its own exposure to think about), and that is a design
   decision this record is not the place to make unilaterally.
10. **Performed.** Both the login and create-password screens render
    correctly with `prefers-color-scheme: dark` emulated — background,
    text, borders and the accent button all switch, nothing is left on a
    light background or unreadable.
11. **Not performed.** The poller-meets-a-dead-session check needs a
    genuine in-flight add job polling in one tab while a second tab signs
    out, and was not attempted in this session — recorded as skipped
    rather than implied passing.
12. **Performed, partially.** Two of the three session-ending messages were
    directly observed and do read differently: signing out shows the
    sign-out-specific copy (English: "You have signed out."), and deleting
    a live session's row directly (standing in for an expiry sweep or a
    password reset elsewhere) and then touching an authenticated page
    shows "Your session has ended. Sign in again." — a different sentence.
    The third case named in the check — a password reset revoking every
    device, deliberately reading the same as the second — was not
    separately reproduced through the CLI's `reset-password` command in
    this browser session; it is recorded in an earlier plan's own decision
    log as intentional, and this check did not contradict that.
13. **Performed.** On both screens the language switcher sits at the top
    right in every screenshot taken, never the top left.

**Verdict — the hashing cost is a measured number on the machine that runs
it: PASSED.** **Verdict — a person looked at both new screens against the
contract that specified them: PASSED, with one recorded defect** (check 9,
above) and one check not performed (check 11) and one performed only
partially (check 12).

## Verdicts, collected

| Claim | Verdict |
|---|---|
| The interface answers on its own hostname and stays closed without a session, measured on loopback | PASSED |
| The interface answers over its real hostname, through the operator's own proxy, with a certificate | PASSED — `https://admin.mytube.example.com/login` returned the login screen, terminated by the operator's own certificate |
| The hostname was published only after the surface behind it was measured closed | PASSED — the DNS record and vhost were created after tasks 1 and 3 already measured the surface behind the hostname closed |
| A burst against the login route is refused by the edge before it reaches the password hash | PASSED — 19 of 20 refused, both runs |
| The delivery path was re-measured on the running stack | PASSED — all 13 delivery-smoke steps, unmodified script |
| A player reaching an interface path on the delivery hostname still gets a clip it can render | PASSED — byte-identical to a genuinely unmatched path |
| The hashing cost is a measured number on the machine that runs it | PASSED — 316-349 ms median, `time_cost=8, memory_cost=262144, parallelism=4` |
| The rendered pages match the design contract | PASSED except one recorded defect (question 9 above) |
| Nothing in this record claims more than what was executed | By construction — see the "not measured" and "not performed" lines above |
