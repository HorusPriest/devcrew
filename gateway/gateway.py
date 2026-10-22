#!/usr/bin/env python3
"""A local gateway that sends chosen Claude Code requests to an approved OpenCode model.

Usage:
  python gateway/gateway.py [--routes gateway/routes.json] [--log gateway/gateway.log]

It listens on 127.0.0.1 only. A request that carries none of the four route
headers (x-route-provider, x-gateway-token, x-run-id, x-data-class) goes to
the default provider unchanged, as if the gateway were not there. A request
that carries any of them is an OpenCode request: it goes to OpenCode only when
every check passes, and is refused with 403 otherwise, never sent anywhere
else. At each start the gateway prints a fresh random token once; it is never
stored or logged. See gateway/README.md.

The upstream endpoints are constants: no flag or config value names one.
Python standard library only.
"""
import argparse
import datetime
import hmac
import http.client
import http.server
import json
import math
import os
import re
import secrets
import sys
import threading
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_UPSTREAM = "https://api.anthropic.com"
OPENCODE_UPSTREAM = "https://opencode.ai/zen/go/v1/messages"
DEFAULT_PORT = 8787
ROUTE_HEADERS = ("x-route-provider", "x-gateway-token", "x-run-id", "x-data-class")
USER_AGENT = "devcrew-gateway/1"
ANTHROPIC_VERSION = "2023-06-01"
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
LENGTH_RE = re.compile(r"^[0-9]{1,12}\Z")
CHUNK_RE = re.compile(rb"^[0-9a-fA-F]{1,8}\Z")
KEY_RE = re.compile(r"^[\x21-\x7e]+\Z")
# one header line (RFC 9112 field-line): a token name, a colon, then only visible characters, spaces and
# tabs; no leading whitespace (a folded line), no CR, no control characters
FIELD_LINE_RE = re.compile(rb"^([!#$%&'*+.^_`|~0-9A-Za-z-]+):([\t \x21-\x7e\x80-\xff]*)\Z")
EVENT_TYPE_RE = re.compile(r"^[a-z][a-z_]{0,63}\Z")
MAX_BODY = 32 * 1024 * 1024
MAX_EVENT = 4 * 1024 * 1024
UPSTREAM_TIMEOUT = 600
CLIENT_TIMEOUT = 600
LOG_FIELD_MAX = 100
# hop-by-hop headers belong to one connection and are never relayed; Expect is answered here
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization",
              "te", "trailer", "transfer-encoding", "upgrade", "expect"}
WITHHELD_EVENT = (b'event: error\ndata: {"type":"error","error":{"type":"api_error",'
                  b'"message":"the gateway withheld the upstream response"}}\n\n')


class Refused(Exception):
    """A request the gateway will not send anywhere. The code is fixed text, never request text."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def no_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicated key")
        seen[key] = value
    return seen


def no_constants(name):
    raise ValueError("JSON constant %s is not allowed" % name)


def finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number out of range: %s" % text[:20])
    return value


def strict_json(data):
    """Parse JSON, refusing a duplicated key at any depth, NaN, Infinity or a number too large to hold
    (such as 1e400), and nesting too deep to parse."""
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicate_keys, parse_constant=no_constants,
                          parse_float=finite_float)
    except RecursionError:
        raise ValueError("JSON nested too deeply")


class Fields:
    """The request's header lines as the gateway itself read them off the wire, in order."""

    def __init__(self, pairs):
        self._pairs = pairs

    def get_all(self, name):
        name = name.lower()
        found = [value for key, value in self._pairs if key.lower() == name]
        return found or None

    def items(self):
        return list(self._pairs)


def parse_field_lines(lines):
    """Read raw header lines strictly. Returns Fields, or None if any line is not a plain header line.

    Routing never relies on the standard library's header parser: it stops at a malformed line and
    silently drops every header after it, and how it treats folded lines differs between versions.
    """
    if not lines or lines[-1] not in (b"\r\n", b"\n"):
        return None  # the block must end with an empty line
    pairs = []
    for line in lines[:-1]:
        if line.endswith(b"\r\n"):
            line = line[:-2]
        elif line.endswith(b"\n"):
            line = line[:-1]
        else:
            return None
        m = FIELD_LINE_RE.match(line)
        if not m:
            return None
        pairs.append((m.group(1).decode("ascii"), m.group(2).strip(b" \t").decode("latin-1")))
    return Fields(pairs)


class LineRecorder:
    """Wraps the request stream while the standard library reads the header block, keeping each raw line."""

    def __init__(self, stream):
        self._stream, self.lines = stream, []

    def readline(self, *args):
        line = self._stream.readline(*args)
        self.lines.append(line)
        return line

    def __getattr__(self, name):
        return getattr(self._stream, name)


def load_routes(path):
    """Read the routes file. A missing file means: no approved model and no data class."""
    if not os.path.exists(path):
        return {"loaded": False, "port": DEFAULT_PORT, "models": [], "classes": {}}
    with open(path, "rb") as f:
        data = strict_json(f.read())
    if not isinstance(data, dict):
        raise ValueError("the routes file must hold a JSON object")
    port = data.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be a whole number from 1 to 65535")
    models = ((data.get("providers") or {}).get("opencode") or {}).get("models") or []
    if not isinstance(models, list) or not all(isinstance(m, str) and m for m in models):
        raise ValueError("providers.opencode.models must be a list of model ids")
    classes = data.get("data_classes") or {}
    if not isinstance(classes, dict) or not all(isinstance(c, dict) for c in classes.values()):
        raise ValueError("data_classes must map each class name to an object")
    for name, cls in classes.items():
        approved = cls.get("approved_providers", [])
        if not isinstance(approved, list) or not all(isinstance(p, str) for p in approved):
            raise ValueError("data class %r: approved_providers must be a list of provider names" % name)
    return {"loaded": True, "port": port, "models": list(models), "classes": classes}


def class_allows_opencode(cls):
    """A class is sensitive unless marked exactly "sensitive": false; a sensitive class must approve opencode."""
    if cls.get("sensitive") is False:
        return True
    return "opencode" in cls.get("approved_providers", [])


def model_name_forbidden(model):
    folded = model.casefold()
    return "claude" in folded or "anthropic" in folded


def loopback_only(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "http" or u.hostname != "127.0.0.1" or not u.port:
        raise ValueError("test upstreams must be http://127.0.0.1:<port> addresses")
    return url


def open_upstream(url, method, target, headers, body):
    """Send one request upstream and return (connection, response). The caller closes the connection."""
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, u.port, timeout=UPSTREAM_TIMEOUT)
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=UPSTREAM_TIMEOUT)
    try:
        conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", u.netloc)
        for name, value in headers:
            conn.putheader(name, value)
        conn.endheaders(body if body else None)
        return conn, conn.getresponse()
    except BaseException:
        conn.close()
        raise


def read_limited(resp, limit):
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError("upstream body too large")
    if resp.length:
        raise ValueError("upstream body shorter than its Content-Length")  # read() returns a short body quietly
    return data


def parse_event(raw):
    """Return (kind, payload) for one server-sent event.

    kind is None for an event made only of comments. Anything that is not a JSON object whose "type"
    is a plain lower-case name, agreeing with the event name if one is given, counts as an error. So
    does a CR left after CRLF line endings were normalised: a lone CR also ends a line in this format.
    """
    if b"\r" in raw:
        return "error", None
    event, data_lines, fields = None, [], 0
    for line in raw.split(b"\n"):
        if not line or line.startswith(b":"):
            continue
        fields += 1
        name, _, value = line.partition(b":")
        if value.startswith(b" "):
            value = value[1:]
        if name == b"event":
            event = value.decode("utf-8", errors="replace").strip()
        elif name == b"data":
            data_lines.append(value)
    if not fields:
        return None, None
    try:
        payload = strict_json(b"\n".join(data_lines)) if data_lines else None
    except ValueError:
        payload = None
    ptype = payload.get("type") if isinstance(payload, dict) else None
    if not isinstance(ptype, str) or not EVENT_TYPE_RE.match(ptype):
        return "error", payload
    if ptype == "error" or (event is not None and event != ptype):
        return "error", payload
    return ptype, payload


class EventSplitter:
    """Cuts an upstream byte stream into raw events.

    CRLF becomes LF exactly once per byte: a CR at the end of one read waits for the next read, and
    bytes already normalised are never normalised again, so a CR CR LF split across reads keeps its
    lone CR (which parse_event then treats as an error) instead of collapsing into a line break.
    """

    def __init__(self):
        self.buf, self.held_cr, self.scanned = bytearray(), False, 0

    def feed(self, data):
        if self.held_cr:
            data = b"\r" + data
        self.held_cr = data.endswith(b"\r")
        if self.held_cr:
            data = data[:-1]
        self.buf += data.replace(b"\r\n", b"\n")
        events, start = [], 0
        end = self.buf.find(b"\n\n", max(self.scanned - 1, 0))  # only new bytes are searched
        while end != -1:
            events.append(bytes(self.buf[start:end]))
            start = end + 2
            end = self.buf.find(b"\n\n", start)
        del self.buf[:start]  # whatever follows the last complete event waits for more bytes
        self.scanned = len(self.buf)
        return events

    def pending(self):
        return len(self.buf)

    def leftover(self):
        """Bytes after the last complete event. A CR still held at the very end is only a line end."""
        return bytes(self.buf)


def serialize_event(kind, payload):
    """Rebuild a checked event from its type and data alone, so no other upstream bytes reach the client."""
    if kind == "ping":
        payload = {"type": "ping"}  # a keep-alive carries nothing, whatever the upstream put in it
    data = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return b"event: %s\ndata: %s\n\n" % (kind.encode("ascii"), data.encode("ascii"))


def cut(value):
    return value[:LOG_FIELD_MAX] if isinstance(value, str) else None


class Gateway:
    def __init__(self, routes_path, log_path, port=None, environ=None, _test_upstreams=None,
                 _client_timeout=CLIENT_TIMEOUT):
        self.routes_path = os.path.abspath(routes_path)
        self.routes = load_routes(self.routes_path)
        self.log_path = os.path.abspath(log_path)
        env = os.environ if environ is None else environ
        key = env.get("OPENCODE_API_KEY") or ""
        self._key = key if KEY_RE.match(key) else None
        self.token = secrets.token_hex(32)  # 32 random bytes, in memory only
        self.default_upstream = DEFAULT_UPSTREAM
        self.opencode_upstream = OPENCODE_UPSTREAM
        if _test_upstreams is not None:  # tests only: loopback stubs in place of the real endpoints
            self.default_upstream = loopback_only(_test_upstreams["default"])
            self.opencode_upstream = loopback_only(_test_upstreams["opencode"])
        self._log_lock = threading.Lock()
        handler = type("BoundHandler", (Handler,), {"gateway": self, "timeout": _client_timeout})
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.routes["port"] if port is None else port), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.allowed_hosts = {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port}
        self._thread = None

    @property
    def has_key(self):
        return self._key is not None

    def start(self, poll_interval=0.5):
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": poll_interval},
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def log(self, rec):
        line = {
            "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "route": rec["route"],
            "reason": rec["reason"],
            "model_requested": cut(rec["model_requested"]),
            "model_reported": cut(rec["model_reported"]),
            "status": rec["status"],
            "run_id": rec["run_id"],
        }
        text = json.dumps(line, ensure_ascii=True) + "\n"
        with self._log_lock:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(text)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = CLIENT_TIMEOUT
    gateway = None
    raw_header_lines = ()
    fields = None

    def parse_request(self):
        recorder, stream = LineRecorder(self.rfile), self.rfile
        self.rfile = recorder
        try:
            return super().parse_request()
        finally:
            self.rfile = stream
            self.raw_header_lines = recorder.lines

    def log_message(self, fmt, *args):
        pass  # the JSON log is the only record; the console never shows requests

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.handle_any  # every method gets the same checks; the routes decide
        raise AttributeError(name)

    def send_error(self, code, message=None, explain=None):
        """The standard library refuses malformed requests itself; log those too."""
        self.gateway.log({"route": "refused", "reason": "malformed_request", "model_requested": None,
                          "model_reported": None, "status": code, "run_id": None})
        super().send_error(code, message, explain)

    # ---- the request ----

    def handle_expect_100(self):
        # parse_request is still recording headers when the standard library calls this hook.
        self.raw_header_lines = self.rfile.lines
        try:
            self.check_request_headers()
        except Refused as e:
            self.close_connection = True
            self.gateway.log({"route": "refused", "reason": e.code, "model_requested": None,
                              "model_reported": None, "status": 403, "run_id": None})
            self.send_constant(403, "refused by the gateway: %s" % e.code, "permission_error")
            return False
        return super().handle_expect_100()

    def handle_any(self):
        self.close_connection = True  # one request per connection: an unread body is never parsed as a request
        rec = {"route": "refused", "reason": None, "model_requested": None, "model_reported": None,
               "status": None, "run_id": None}
        try:
            self.route(rec)
        except Refused as e:
            rec["route"], rec["reason"], rec["status"] = "refused", e.code, 403
            self.send_constant(403, "refused by the gateway: %s" % e.code, "permission_error")
        except (BrokenPipeError, ConnectionResetError):
            rec["reason"] = rec["reason"] or "client_gone"
        finally:
            self.gateway.log(rec)

    def check_request_headers(self):
        if self.request_version == "HTTP/0.9":
            raise Refused("http09")
        fields = parse_field_lines(self.raw_header_lines)
        if fields is None:
            raise Refused("header_syntax")
        self.fields = fields
        hosts = fields.get_all("Host") or []
        if len(hosts) != 1 or hosts[0] not in self.gateway.allowed_hosts:
            raise Refused("host")
        if fields.get_all("Origin") is not None:
            raise Refused("origin")
        if self.command == "OPTIONS":
            raise Refused("options")

    def route(self, rec):
        self.check_request_headers()
        fields = self.fields
        if not self.path.startswith("/"):
            raise Refused("path")
        if any(fields.get_all(name) is not None for name in ROUTE_HEADERS):
            return self.opencode_route(rec)
        if urllib.parse.urlsplit(self.path).path.startswith("/_gateway/"):
            return self.local_route(rec)
        return self.default_route(rec)

    def read_body(self):
        te = self.fields.get_all("Transfer-Encoding")
        cl = self.fields.get_all("Content-Length")
        if te is not None and cl is not None:
            raise Refused("body")
        if te is not None:
            if [t.strip().lower() for t in te] != ["chunked"]:
                raise Refused("body")
            return self.read_chunked()
        if cl is None:
            return b""
        if len(cl) != 1 or not LENGTH_RE.match(cl[0].strip()):
            raise Refused("body")
        n = int(cl[0].strip())
        if n > MAX_BODY:
            raise Refused("body_too_large")
        data = self.rfile.read(n)
        if len(data) != n:
            raise Refused("body")
        return data

    def read_chunked(self):
        parts, total = [], 0
        while True:
            line = self.rfile.readline(1024)
            digits = line.split(b";", 1)[0].strip()
            if not CHUNK_RE.match(digits):
                raise Refused("body")
            size = int(digits, 16)
            if size == 0:
                while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                    pass  # trailers are dropped
                return b"".join(parts)
            total += size
            if total > MAX_BODY:
                raise Refused("body_too_large")
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                raise Refused("body")
            parts.append(chunk)

    # ---- default route: unchanged ----

    def default_route(self, rec):
        rec["route"] = "default"
        chunked = self.fields.get_all("Transfer-Encoding") is not None
        body = self.read_body()
        headers = [(k, v) for k, v in self.fields.items() if k.lower() != "host" and k.lower() not in HOP_BY_HOP]
        if chunked:
            headers.append(("Content-Length", str(len(body))))
        try:
            conn, resp = open_upstream(self.gateway.default_upstream, self.command, self.path, headers, body)
        except (OSError, http.client.HTTPException, ValueError):
            rec["reason"], rec["status"] = "upstream_unreachable", 502
            return self.send_constant(502, "the gateway could not reach the upstream", "api_error")
        try:
            rec["status"] = resp.status
            self.relay(resp)
        finally:
            conn.close()

    def relay(self, resp):
        self.send_response_only(resp.status, resp.reason)
        for name, value in resp.getheaders():
            low = name.lower()
            if low in HOP_BY_HOP or low.startswith("access-control-") or low == "content-length":
                continue
            self.send_header(name, value)
        self.send_header("Connection", "close")
        bodiless = self.command == "HEAD" or resp.status in (204, 304) or resp.status < 200
        if bodiless:
            length = resp.getheader("Content-Length")
            if length is not None and self.command == "HEAD":
                self.send_header("Content-Length", length)
            self.end_headers()
            return
        if resp.length is not None and not resp.chunked:
            self.send_header("Content-Length", str(resp.length))
            self.end_headers()
            while True:
                data = resp.read1(65536)
                if not data:
                    break
                self.wfile.write(data)
                self.wfile.flush()
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            data = resp.read1(65536)
            if not data:
                break
            self.write_chunk(data)
        self.end_chunks()

    # ---- OpenCode route: every check, or refused ----

    def opencode_route(self, rec):
        rec["route"] = "opencode"
        h, gw = self.fields, self.gateway
        if (h.get_all("x-route-provider") or []) != ["opencode"]:
            raise Refused("provider")
        token = h.get_all("x-gateway-token") or []
        if len(token) != 1:
            raise Refused("token")
        if not hmac.compare_digest(token[0].encode("utf-8", errors="replace"), gw.token.encode("ascii")):
            raise Refused("token")
        run_id = h.get_all("x-run-id") or []
        if len(run_id) != 1 or not RUN_ID_RE.match(run_id[0]):
            raise Refused("run_id")
        rec["run_id"] = run_id[0]
        if self.command != "POST":
            raise Refused("method")
        if urllib.parse.urlsplit(self.path).path != "/v1/messages":
            raise Refused("path")
        data_class = h.get_all("x-data-class") or []
        if len(data_class) != 1 or data_class[0] not in gw.routes["classes"]:
            raise Refused("data_class")
        if not class_allows_opencode(gw.routes["classes"][data_class[0]]):
            raise Refused("data_class_not_approved")
        if not gw.has_key:
            raise Refused("no_key")
        body = self.read_body()
        try:
            payload = strict_json(body)
        except ValueError:
            raise Refused("body")
        if not isinstance(payload, dict):
            raise Refused("body")
        model = payload.get("model")
        rec["model_requested"] = model if isinstance(model, str) else None
        if not isinstance(model, str) or model not in gw.routes["models"]:
            raise Refused("model_not_approved")
        if model_name_forbidden(model):
            raise Refused("model_name")
        self.forward_opencode(rec, body, model)

    def forward_opencode(self, rec, body, model):
        key = self.gateway._key
        headers = [
            ("Authorization", "Bearer " + key),
            ("x-api-key", key),
            ("User-Agent", USER_AGENT),
            ("content-type", "application/json"),
            ("anthropic-version", ANTHROPIC_VERSION),
            ("Content-Length", str(len(body))),
        ]
        target = urllib.parse.urlsplit(self.gateway.opencode_upstream).path
        try:
            conn, resp = open_upstream(self.gateway.opencode_upstream, "POST", target, headers, body)
        except (OSError, http.client.HTTPException, ValueError):
            rec["reason"], rec["status"] = "upstream_unreachable", 502
            return self.send_constant(502, "the gateway could not reach the upstream", "api_error")
        try:
            if not 200 <= resp.status < 300:
                rec["reason"], rec["status"] = "upstream_error", 502
                return self.send_constant(502, "the upstream refused the request", "api_error")
            if (resp.getheader("Content-Type") or "").lower().startswith("text/event-stream"):
                return self.relay_stream(rec, resp, model)
            try:
                data = read_limited(resp, MAX_BODY)
                answer = strict_json(data)
            except (ValueError, OSError, http.client.HTTPException):
                rec["reason"], rec["status"] = "upstream_unreadable", 502
                return self.send_constant(502, "the gateway withheld the upstream response", "api_error")
            reported = answer.get("model") if isinstance(answer, dict) else None
            rec["model_reported"] = reported if isinstance(reported, str) else None
            if reported != model or answer.get("type") != "message":
                rec["reason"], rec["status"] = "model_mismatch", 502
                return self.send_constant(502, "the gateway withheld the upstream response", "api_error")
            rec["status"] = 200
            self.send_bytes(200, "application/json", data)
        finally:
            conn.close()

    def relay_stream(self, rec, resp, model):
        rec["status"] = 200
        self.send_response_only(200, "OK")
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        started, stopped, splitter = False, False, EventSplitter()
        while True:
            try:
                data = resp.read1(65536)
            except (OSError, http.client.HTTPException):
                return self.withhold(rec, "upstream_broken")  # a client that went away is not caught here
            if not data:
                break
            for raw in splitter.feed(data):
                kind, payload = parse_event(raw)
                if not started:
                    if kind in (None, "ping"):
                        continue  # nothing is relayed before the model is checked
                    if kind != "message_start":
                        return self.withhold(rec, "stream_no_start")
                    message = payload.get("message") if isinstance(payload, dict) else None
                    reported = message.get("model") if isinstance(message, dict) else None
                    rec["model_reported"] = reported if isinstance(reported, str) else None
                    if reported != model:
                        return self.withhold(rec, "model_mismatch")
                    started = True
                elif stopped:
                    if kind is None:
                        continue  # comments are never relayed
                    if kind != "ping":
                        return self.withhold(rec, "event_after_stop")  # the message is complete; only pings may follow
                elif kind == "error":
                    return self.withhold(rec, "upstream_error_event")
                elif kind == "message_start":
                    return self.withhold(rec, "model_mismatch")  # one message per stream; a second start is a switch
                elif kind is None:
                    continue  # comments are never relayed: they could carry upstream text
                self.write_chunk(serialize_event(kind, payload))
                if kind == "message_stop":
                    stopped = True
            if splitter.pending() > MAX_EVENT:
                return self.withhold(rec, "event_too_large")
        if not started:
            return self.withhold(rec, "stream_no_start")
        if splitter.leftover().strip(b"\r\n"):
            return self.withhold(rec, "stream_truncated")  # an unfinished event is never dropped silently
        if not stopped:
            return self.withhold(rec, "stream_truncated")  # a stream cut off between events never ends as a success
        self.end_chunks()

    def withhold(self, rec, code):
        rec["reason"] = code
        self.write_chunk(WITHHELD_EVENT)
        self.end_chunks()

    # ---- local answers ----

    def local_route(self, rec):
        rec["route"] = "local"
        if self.command != "GET" or urllib.parse.urlsplit(self.path).path != "/_gateway/status":
            rec["status"] = 404
            return self.send_constant(404, "no such gateway page", "not_found_error")
        gw = self.gateway
        status = {
            "opencode_key_present": gw.has_key,
            "approved_models": gw.routes["models"],
            "routes_file": gw.routes_path,
            "routes_loaded": gw.routes["loaded"],
            "port": gw.port,
        }
        rec["status"] = 200
        self.send_bytes(200, "application/json", (json.dumps(status, indent=2) + "\n").encode("utf-8"))

    # ---- writing ----

    def send_bytes(self, code, ctype, data):
        self.send_response_only(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_constant(self, code, message, kind):
        body = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode("utf-8")
        self.send_bytes(code, "application/json", body)

    def write_chunk(self, data):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def end_chunks(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local gateway: chosen Claude Code requests to an approved OpenCode model.")
    ap.add_argument("--routes", default=os.path.join(HERE, "routes.json"), help="routes file (default gateway/routes.json)")
    ap.add_argument("--log", default=os.path.join(HERE, "gateway.log"), help="log file (default gateway/gateway.log)")
    a = ap.parse_args(argv)
    try:
        gw = Gateway(a.routes, a.log)
    except (OSError, ValueError) as e:
        print("ERROR: %s" % e)
        return 2
    print("gateway listening on http://127.0.0.1:%d" % gw.port)
    print("routes file: %s (%s)" % (gw.routes_path, "loaded" if gw.routes["loaded"] else "not found: OpenCode requests will be refused"))
    print("OPENCODE_API_KEY: %s" % ("present" if gw.has_key else "missing: OpenCode requests will be refused"))
    print("log file: %s" % gw.log_path)
    print("gateway token for this start (shown once, never stored): %s" % gw.token)
    sys.stdout.flush()
    try:
        gw.server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gw.server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
