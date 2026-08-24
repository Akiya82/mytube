"""Tests for orchestrator.signing — including the golden vector and the live
nginx cross-check for Pitfall C (Python/secure_link_md5 formula drift)."""
import subprocess
import textwrap
import time
from pathlib import Path

import httpx
import pytest

from orchestrator.signing import blob_path, sign_blob_url

GOLDEN_EDITION_ID = "11111111-1111-1111-1111-111111111111"
GOLDEN_EXPIRES = 1893456000
GOLDEN_SECRET = "golden-test-secret"
GOLDEN_SIG = "Y5XmPPrYbfymO6y5qiUcKw"


def test_blob_path():
    assert blob_path("abc-123") == "/blob/abc-123.mp4"


def test_sign_blob_url_returns_sig_and_expires(signing_secret):
    before = int(time.time())
    sig, expires = sign_blob_url("edition-1", signing_secret, ttl_seconds=43200)
    after = int(time.time())
    assert before + 43200 <= expires <= after + 43200
    assert isinstance(sig, str) and sig


def test_signature_changes_with_edition_id(monkeypatch):
    import orchestrator.signing as signing_mod

    monkeypatch.setattr(signing_mod.time, "time", lambda: 5000)
    sig_a, _ = sign_blob_url("edition-a", "same-secret", ttl_seconds=100)
    sig_b, _ = sign_blob_url("edition-b", "same-secret", ttl_seconds=100)
    assert sig_a != sig_b


def test_signature_changes_with_secret():
    sig_a, _ = sign_blob_url("edition-1", "secret-a", ttl_seconds=100)
    sig_b, _ = sign_blob_url("edition-1", "secret-b", ttl_seconds=100)
    assert sig_a != sig_b


def test_signature_changes_with_expires(monkeypatch):
    import orchestrator.signing as signing_mod

    times = iter([1000, 2000])
    monkeypatch.setattr(signing_mod.time, "time", lambda: next(times))
    sig_a, exp_a = sign_blob_url("edition-1", "same-secret", ttl_seconds=100)
    sig_b, exp_b = sign_blob_url("edition-1", "same-secret", ttl_seconds=100)
    assert exp_a != exp_b
    assert sig_a != sig_b


def test_golden_vector(monkeypatch):
    """Fails if the formula's field order is ever rearranged."""
    import orchestrator.signing as signing_mod

    monkeypatch.setattr(signing_mod.time, "time", lambda: GOLDEN_EXPIRES)
    sig, expires = sign_blob_url(GOLDEN_EDITION_ID, GOLDEN_SECRET, ttl_seconds=0)
    assert expires == GOLDEN_EXPIRES
    assert sig == GOLDEN_SIG


NGINX_TEST_CONF = textwrap.dedent(
    """\
    events {}
    http {
        server {
            listen 80;
            location ~ ^/blob/(?<edition_id>[0-9a-f-]+)\\.mp4$ {
                secure_link     $arg_sig,$arg_exp;
                # ${uri} (curly-braced) is required, not $uri: nginx's variable-name
                # parser greedily consumes identifier chars after $uri, so any secret
                # starting with alnum (e.g. hex from openssl rand -hex) makes nginx
                # read "$uri<secret-prefix>" as one unknown variable name and refuse
                # to start ("unknown \"uri...\" variable"). Found while writing this
                # test — the bounded form must also be used in nginx.conf.template.
                secure_link_md5 "$secure_link_expires${uri}%s";
                if ($secure_link = "") { return 403; }
                if ($secure_link = "0") { return 403; }
                return 200 "ok";
            }
        }
    }
    """
)


@pytest.mark.integration
def test_nginx_accepts_python_signature(tmp_path: Path, signing_secret: str):
    """Live nginx:1.27-alpine, secure_link_md5 with the same expression as
    src/edge/nginx.conf.template, accepts a URL signed by sign_blob_url()."""
    conf_path = tmp_path / "nginx.conf"
    conf_path.write_text(NGINX_TEST_CONF % signing_secret, encoding="utf-8")

    edition_id = "22222222-2222-2222-2222-222222222222"
    sig, expires = sign_blob_url(edition_id, signing_secret, ttl_seconds=3600)

    container_name = "mytube-test-signing-nginx"
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    run = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", container_name,
            "-p", "0:80",
            "-v", f"{conf_path}:/etc/nginx/nginx.conf:ro",
            "nginx:1.27-alpine",
        ],
        capture_output=True, text=True,
    )
    assert run.returncode == 0, f"docker run failed: {run.stderr}"

    try:
        port_out = subprocess.run(
            ["docker", "port", container_name, "80/tcp"],
            capture_output=True, text=True, check=True,
        )
        host_port = port_out.stdout.strip().split(":")[-1]
        base_url = f"http://127.0.0.1:{host_port}"

        # wait for nginx to be ready
        for _ in range(30):
            try:
                httpx.get(f"{base_url}/", timeout=1)
                break
            except httpx.TransportError:
                time.sleep(0.5)

        good_url = f"{base_url}/blob/{edition_id}.mp4?sig={sig}&exp={expires}"
        resp = httpx.get(good_url, timeout=5)
        assert resp.status_code == 200, f"valid signature rejected: {resp.status_code} {resp.text}"

        # Corrupt the FIRST character, not the last. The signature is 22
        # base64url characters encoding a 128-bit MD5 digest with no
        # padding: the last character carries only 2 significant bits (the
        # other 4 are unused), so swapping 'A' -> 'B' there can flip only
        # the unused bits and decode to the same 16 bytes. nginx correctly
        # answers 200 in that case, and this test used to fail on a quarter
        # of the signatures it tried. Any character except the last carries
        # the full 6 bits, so corrupting it is always a real corruption.
        tampered_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
        bad_url = f"{base_url}/blob/{edition_id}.mp4?sig={tampered_sig}&exp={expires}"
        resp_bad = httpx.get(bad_url, timeout=5)
        assert resp_bad.status_code == 403, f"tampered signature accepted: {resp_bad.status_code}"
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
