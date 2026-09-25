"""Bounded HTTPS retrieval of caller-supplied Evidence originals."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit, urlunsplit


class ResourceURLRejected(ValueError):
    """The URL or returned entity cannot be accepted as Evidence."""


class ResourceFetchUnavailable(RuntimeError):
    """The remote original could not be fetched within the configured deadline."""


def _validate_url(raw_url: str):
    if (
        not isinstance(raw_url, str)
        or len(raw_url) > 2048
        or any(ord(character) < 32 or ord(character) == 127 for character in raw_url)
    ):
        raise ResourceURLRejected("Evidence URL is invalid")
    try:
        parsed = urlsplit(raw_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ResourceURLRejected("Evidence URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port == 0
    ):
        raise ResourceURLRejected(
            "Evidence URL requires public HTTPS without credentials or fragment"
        )
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if isinstance(literal, ipaddress.IPv6Address) and literal.ipv4_mapped:
            literal = literal.ipv4_mapped
        if not literal.is_global:
            raise ResourceURLRejected("Evidence URL has a non-public destination")
    return parsed


def safe_source_url(raw_url: str) -> str:
    parsed = _validate_url(raw_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _resolve_public_address(host: str, port: int) -> str:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise ResourceFetchUnavailable("Evidence URL host could not be resolved") from exc
    public = []
    for family, _, _, _, address in addresses:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        parsed = ipaddress.ip_address(address[0].split("%", 1)[0])
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            parsed = parsed.ipv4_mapped
        if not parsed.is_global:
            raise ResourceURLRejected("Evidence URL resolves to a non-public address")
        public.append(str(parsed))
    if not public:
        raise ResourceFetchUnavailable("Evidence URL has no reachable public address")
    return public[0]


def _validate_content(content: bytes, expected_media_type: str, max_bytes: int) -> bytes:
    if not content or len(content) > max_bytes:
        raise ResourceURLRejected("Evidence URL content is empty or exceeds the byte limit")
    if content.startswith(b"%PDF-"):
        actual = "application/pdf"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        actual = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        actual = "image/jpeg"
    elif b"<html" in content[:1024].lower() or content[:1024].lstrip().lower().startswith(
        b"<!doctype html"
    ):
        actual = "text/html"
    else:
        actual = None
    if actual != expected_media_type:
        raise ResourceURLRejected("Evidence URL content does not match the declared media type")
    return content


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _download(
    raw_url: str, expected_media_type: str, max_bytes: int, timeout_seconds: float
) -> bytes:
    parsed = _validate_url(raw_url)
    port = parsed.port or 443
    host = parsed.hostname.encode("idna").decode("ascii")
    address = _resolve_public_address(host, port)
    deadline = time.monotonic() + timeout_seconds
    connection = _PinnedHTTPSConnection(host, address, port, timeout_seconds)
    target = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        target += "?" + quote(parsed.query, safe="=&%/:?+,$@!;*'()-._~")
    try:
        connection.request(
            "GET",
            target,
            headers={"Accept-Encoding": "identity", "Connection": "close"},
        )
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ResourceURLRejected("Evidence URL redirected instead of returning an original")
        if response.status != 200:
            raise ResourceFetchUnavailable(
                "Evidence URL did not return a complete HTTP 200 response"
            )
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise ResourceURLRejected("Evidence URL returned compressed content")
        declared_length = response.getheader("Content-Length")
        length_from_header = None
        if declared_length is not None:
            try:
                length_from_header = int(declared_length)
                if length_from_header < 0 or length_from_header > max_bytes:
                    raise ResourceURLRejected("Evidence URL content exceeds the byte limit")
            except ValueError as exc:
                raise ResourceURLRejected("Evidence URL has invalid content length") from exc
        chunks = []
        length = 0
        while True:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise ResourceFetchUnavailable("Evidence URL fetch timed out")
            if connection.sock is not None:
                connection.sock.settimeout(remaining_time)
            chunk = response.read1(min(4096, max_bytes + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > max_bytes:
                raise ResourceURLRejected("Evidence URL content exceeds the byte limit")
        if length_from_header is not None and length != length_from_header:
            raise ResourceFetchUnavailable("Evidence URL returned an incomplete response")
        return _validate_content(b"".join(chunks), expected_media_type, max_bytes)
    except (ResourceURLRejected, ResourceFetchUnavailable):
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
        raise ResourceFetchUnavailable("Evidence URL fetch failed") from exc
    finally:
        connection.close()


def fetch_resource_url(
    raw_url: str, expected_media_type: str, *, max_bytes: int, timeout_seconds: float
) -> bytes:
    """Run the network fetch in a killable child so DNS and body time share one deadline."""
    _validate_url(raw_url)
    request = json.dumps(
        {
            "url": raw_url,
            "media_type": expected_media_type,
            "max_bytes": max_bytes,
            "timeout_seconds": timeout_seconds,
        },
        ensure_ascii=False,
    ).encode()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "oam.resources.url_fetch"],
            input=request,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ResourceFetchUnavailable("Evidence URL fetch timed out") from exc
    if result.returncode == 2:
        raise ResourceURLRejected("Evidence URL or returned content was rejected")
    if result.returncode != 0:
        raise ResourceFetchUnavailable("Evidence URL fetch failed")
    return _validate_content(result.stdout, expected_media_type, max_bytes)


def _main() -> int:
    request = json.loads(sys.stdin.buffer.read(16384))
    try:
        content = _download(
            request["url"],
            request["media_type"],
            request["max_bytes"],
            request["timeout_seconds"],
        )
    except ResourceURLRejected:
        return 2
    except ResourceFetchUnavailable:
        return 3
    sys.stdout.buffer.write(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
