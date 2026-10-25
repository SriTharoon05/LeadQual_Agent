"""Fetch bounded public website text without following URLs into private networks."""
import gzip
import http.client
import io
import ipaddress
import socket
import ssl
import threading
from time import monotonic
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import dns.resolver


class _SocketDeadline:
    """Interrupt slow-dripped headers/body, including detached response sockets."""
    def __init__(self, timeout):
        self.deadline = monotonic() + timeout
        self.lock = threading.Lock()
        self.socket = None
        self.expired = threading.Event()
        self.timer = threading.Timer(timeout, self._expire)
        self.timer.daemon = True

    @staticmethod
    def _shutdown(sock):
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _expire(self):
        self.expired.set()
        with self.lock:
            self._shutdown(self.socket)

    def register(self, sock):
        with self.lock:
            self.socket = sock
            if self.expired.is_set() or monotonic() >= self.deadline:
                self._shutdown(sock)
                raise TimeoutError("Website fetch timed out")

    def __enter__(self):
        self.timer.start()
        return self

    def __exit__(self, *_args):
        self.timer.cancel()
        with self.lock:
            self.socket = None


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, hostname, port, address, timeout, secure, deadline):
        super().__init__(hostname, port, timeout=timeout)
        self.address = address
        self.secure = secure
        self.default_port = 443 if secure else 80
        self.deadline = deadline

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        self.deadline.register(self.sock)
        if self.secure:
            context = ssl.create_default_context()
            context.set_alpn_protocols(["http/1.1"])
            try:
                self.sock = context.wrap_socket(self.sock, server_hostname=self.host, do_handshake_on_connect=False)
                self.deadline.register(self.sock)
                self.sock.do_handshake()
            except Exception:
                self.sock.close()
                raise


def _public_address(hostname, deadline):
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        resolver = dns.resolver.Resolver()
        addresses = []
        for record_type in ("A", "AAAA"):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Website lookup timed out")
            try:
                records = resolver.resolve(hostname, record_type, lifetime=min(2.0, remaining))
                addresses = [ipaddress.ip_address(str(record)) for record in records]
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                continue
            if addresses:
                break
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Only public company websites can be scraped")
    # The TCP connection uses this checked address. Do not resolve the hostname
    # a second time in an HTTP library (a DNS-rebinding/redirect bypass).
    return str(addresses[0])


def fetch_public_html(url, user_agent, max_bytes, timeout=8):
    with _SocketDeadline(timeout) as guard:
        return _fetch_public_html(url, user_agent, max_bytes, guard)


def _fetch_public_html(url, user_agent, max_bytes, guard):
    deadline = guard.deadline
    current = url
    for _ in range(5):
        parts = urlsplit(current)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username or parts.password):
            raise ValueError("Unsupported website URL")
        host = parts.hostname.encode("idna").decode("ascii")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in {80, 443}:
            raise ValueError("Only standard website ports are supported")
        address = _public_address(host, deadline)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Website fetch timed out")
        connection = _PinnedConnection(host, port, address, remaining, parts.scheme == "https", guard)
        response = None
        try:
            path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
            query = quote(parts.query, safe="%:@!$&'()*+,;=/?-._~")
            target = path + ("?" + query if query else "")
            connection.request("GET", target, headers={"User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain", "Accept-Encoding": "identity"})
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Website fetch timed out")
            if connection.sock:
                connection.sock.settimeout(remaining)
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Website redirect has no destination")
                current = urljoin(current, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise ValueError("Website returned an unsuccessful response")
            content_type = response.getheader("Content-Type", "text/html").lower()
            if not any(value in content_type for value in ("text/html", "application/xhtml+xml", "text/plain")):
                raise ValueError("Website response is not text or HTML")
            length = response.getheader("Content-Length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise ValueError("Website response exceeds the size limit")
            chunks, size = [], 0
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("Website read timed out")
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65_536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Website response exceeds the size limit")
            if guard.expired.is_set() or monotonic() >= deadline:
                raise TimeoutError("Website fetch timed out")
            if length.isdigit() and size != int(length):
                raise ValueError("Website response was incomplete")
            body = b"".join(chunks)
            encoding = response.getheader("Content-Encoding", "identity").lower()
            if encoding == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                    body = compressed.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ValueError("Decoded website response exceeds the size limit")
            elif encoding not in {"identity", ""}:
                raise ValueError("Unsupported website content encoding")
            return body, urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
        finally:
            if response is not None:
                response.close()
            connection.close()
    raise ValueError("Too many website redirects")
