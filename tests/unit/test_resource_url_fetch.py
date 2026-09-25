"""Untrusted URL fetches fail closed before any document is accepted."""

import socket
import subprocess
from types import SimpleNamespace

import pytest

from oam.resources import url_fetch
from oam.resources.url_fetch import (
    ResourceFetchUnavailable,
    ResourceURLRejected,
    _resolve_public_address,
    _validate_content,
    _validate_url,
)


@pytest.mark.parametrize(
    "url",
    (
        "http://example.com/report.pdf",
        "file:///tmp/report.pdf",
        "https://127.0.0.1/report.pdf",
        "https://169.254.169.254/latest/meta-data",
        "https://user:password@example.com/report.pdf",
        "https://example.com/report.pdf#fragment",
    ),
)
def test_url_fetch_rejects_unsafe_url_shapes(url):
    with pytest.raises(ResourceURLRejected):
        _validate_url(url)


def test_url_fetch_rejects_private_dns_and_pins_a_public_address(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443)),
        ],
    )
    with pytest.raises(ResourceURLRejected, match="non-public"):
        _resolve_public_address("reports.example.test", 443)

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))
        ],
    )
    assert _resolve_public_address("reports.example.test", 443) == "93.184.215.14"


def test_url_fetch_rejects_landing_page_mismatch_and_oversize_content():
    with pytest.raises(ResourceURLRejected, match="media type"):
        _validate_content(b"<html>Expired URL</html>", "application/pdf", 100)
    with pytest.raises(ResourceURLRejected, match="byte limit"):
        _validate_content(b"%PDF-" + b"x" * 101, "application/pdf", 100)
    assert _validate_content(b"%PDF-1.4 synthetic", "application/pdf", 100) == (
        b"%PDF-1.4 synthetic"
    )


def test_https_fetch_uses_pinned_address_original_host_and_exact_entity(monkeypatch):
    seen = {}
    content = b"%PDF-1.4 synthetic"

    class Response:
        status = 200

        def getheader(self, name, default=None):
            return default

        def read1(self, length):
            nonlocal content
            chunk, content = content[:length], content[length:]
            return chunk

    class Connection:
        def __init__(self, host, address, port, timeout):
            seen["connection"] = (host, address, port, timeout)
            self.sock = SimpleNamespace(settimeout=lambda value: None)

        def request(self, method, target, headers):
            seen["request"] = (method, target, headers)

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(url_fetch, "_resolve_public_address", lambda host, port: "93.184.215.14")
    monkeypatch.setattr(url_fetch, "_PinnedHTTPSConnection", Connection)

    result = url_fetch._download(
        "https://reports.example.test/report.pdf?sig=synthetic", "application/pdf", 100, 5
    )

    assert result == b"%PDF-1.4 synthetic"
    assert seen["connection"] == ("reports.example.test", "93.184.215.14", 443, 5)
    assert seen["request"][0:2] == ("GET", "/report.pdf?sig=synthetic")
    assert seen["request"][2]["Accept-Encoding"] == "identity"
    assert "Authorization" not in seen["request"][2]


def test_https_fetch_rejects_short_http_200_body(monkeypatch):
    body = b"<html>incomplete"

    class Response:
        status = 200

        def getheader(self, name, default=None):
            return str(len(body) + 20) if name == "Content-Length" else default

        def read1(self, length):
            nonlocal body
            chunk, body = body[:length], body[length:]
            return chunk

    class Connection:
        sock = None

        def __init__(self, *args):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(url_fetch, "_resolve_public_address", lambda host, port: "93.184.215.14")
    monkeypatch.setattr(url_fetch, "_PinnedHTTPSConnection", Connection)

    with pytest.raises(ResourceFetchUnavailable, match="incomplete"):
        url_fetch._download("https://reports.example.test/report.html", "text/html", 100, 5)


@pytest.mark.parametrize(
    "status,encoding,error_type",
    (
        (302, "identity", ResourceURLRejected),
        (404, "identity", ResourceFetchUnavailable),
        (200, "gzip", ResourceURLRejected),
    ),
)
def test_https_fetch_rejects_redirect_or_compressed_response(
    monkeypatch, status, encoding, error_type
):
    class Connection:
        sock = None

        def __init__(self, *args):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return SimpleNamespace(
                status=status,
                getheader=lambda name, default=None: (
                    encoding if name == "Content-Encoding" else default
                ),
            )

        def close(self):
            pass

    monkeypatch.setattr(url_fetch, "_resolve_public_address", lambda host, port: "93.184.215.14")
    monkeypatch.setattr(url_fetch, "_PinnedHTTPSConnection", Connection)

    with pytest.raises(error_type):
        url_fetch._download("https://reports.example.test/report.pdf", "application/pdf", 100, 5)


def test_url_fetch_child_timeout_is_unavailable_without_network(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic child", 0.1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ResourceFetchUnavailable, match="timed out"):
        url_fetch.fetch_resource_url(
            "https://reports.example.test/report.pdf",
            "application/pdf",
            max_bytes=100,
            timeout_seconds=1,
        )
