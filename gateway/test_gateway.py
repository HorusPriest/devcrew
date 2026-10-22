#!/usr/bin/env python3
"""Tests for gateway.py against two loopback stub upstreams. No request leaves 127.0.0.1.

Usage:
  python gateway/test_gateway.py [-v] [TestGateway.test_name ...]

Python standard library only.
"""
import http.client
import io
import http.server
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gateway  # noqa: E402

KEY = "canary-canary-canary-canary"
PROMPT = "PROMPT-CANARY-8e2b do not log me"
RUN_ID = "0123456789abcdef0123456789abcdef"
MODEL = "fictional-coder-1"
ROUTES = {
    "port": 8787,
    "providers": {"opencode": {"models": [MODEL, "fictional-CLAUDE-mirror", "fictional-AnThRoPiC-relay"]}},
    "data_classes": {
        "general": {"sensitive": False},
        "internal": {"sensitive": True, "approved_providers": ["opencode"]},
        "client-data": {"sensitive": True, "approved_providers": []},
        "unset-null": {"sensitive": None},
        "unset-missing": {},
        "string-false": {"sensitive": "false"},
    },
}


class Stub:
    """A loopback upstream that records what it receives and answers as the test says."""

    def __init__(self):
        self.received = []
        self.answer = self.json_answer(MODEL)
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def handle_any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                stub.received.append({"method": self.command, "path": self.path,
                                      "headers": list(self.headers.items()), "body": body})
                stub.answer(self)
                self.close_connection = True

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def json_answer(model, status=200, extra=()):
        def answer(h):
            body = json.dumps({"id": "msg_stub", "type": "message", "model": model, "role": "assistant",
                               "content": [{"type": "text", "text": "STUB-REPLY"}]}).encode()
            h.send_response_only(status)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.send_header("Access-Control-Allow-Origin", "*")
            h.send_header("Access-Control-Expose-Headers", "x-stub")
            h.send_header("X-Stub", "yes")
            for k, v in extra:
                h.send_header(k, v)
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(body)
        return answer

    @staticmethod
    def raw_answer(status, ctype, body):
        def answer(h):
            h.send_response_only(status)
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(len(body)))
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(body)
        return answer

    @staticmethod
    def wire_answer(head, parts):
        """Write the status line and headers as given, then each part as is: for broken framing."""
        def answer(h):
            try:
                h.wfile.write(head)
                for part in parts:
                    h.wfile.write(part)
                    h.wfile.flush()
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                pass
        return answer

    @staticmethod
    def reset_after(head, parts):
        """Write the head and parts, then reset the connection (RST) instead of closing it."""
        def answer(h):
            h.wfile.write(head)
            for part in parts:
                h.wfile.write(part)
                h.wfile.flush()
                time.sleep(0.01)
            h.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            h.connection.close()
            h.wfile, h.rfile = io.BytesIO(), io.BytesIO()  # nothing more is written or read
        return answer

    @staticmethod
    def sse_answer(events, cors=True):
        def answer(h):
            h.send_response_only(200)
            h.send_header("Content-Type", "text/event-stream")
            if cors:
                h.send_header("Access-Control-Allow-Origin", "*")
            h.send_header("Transfer-Encoding", "chunked")
            h.send_header("Connection", "close")
            h.end_headers()
            try:
                for ev in events:
                    h.wfile.write(b"%x\r\n%s\r\n" % (len(ev), ev))
                    h.wfile.flush()
                    time.sleep(0.01)
                h.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # the gateway stopped reading, as it should when it withholds a stream
        return answer


def sse(event, data):
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode()


def start_event(model):
    return sse("message_start", {"type": "message_start", "message": {"id": "msg_stub", "model": model}})


DELTA = sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                    "delta": {"type": "text_delta", "text": "STREAM-TEXT"}})
STOP = sse("message_stop", {"type": "message_stop"})
PING = sse("ping", {"type": "ping"})
BLOCK_STOP = sse("content_block_stop", {"type": "content_block_stop", "index": 0})
MESSAGE_DELTA = sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
DELTA_DATA = DELTA.split(b"\ndata: ", 1)[1].split(b"\n", 1)[0]


def rebuilt(event_bytes):
    """What the gateway relays for a checked event: its type and its data as compact JSON, nothing else."""
    data = json.loads(event_bytes.split(b"\ndata: ", 1)[1].split(b"\n", 1)[0])
    return ("event: %s\ndata: %s\n\n" % (data["type"], json.dumps(data, separators=(",", ":")))).encode()


class GatewayCase(unittest.TestCase):
    environ = {"OPENCODE_API_KEY": KEY}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.routes = os.path.join(self.tmp, "routes.json")
        with open(self.routes, "w", encoding="utf-8") as f:
            json.dump(ROUTES, f)
        self.logfile = os.path.join(self.tmp, "gateway.log")
        self.default = Stub()
        self.opencode = Stub()
        self.gw = gateway.Gateway(self.routes, self.logfile, port=0, environ=dict(self.environ),
                                  _test_upstreams={"default": self.default.url,
                                                   "opencode": self.opencode.url + "/zen/go/v1/messages"})
        self.gw.start(poll_interval=0.05)
        self.host = "127.0.0.1:%d" % self.gw.port

    def tearDown(self):
        self.gw.stop()
        self.default.stop()
        self.opencode.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def send(self, method, path, headers=(), body=None, host=None, content_length=None):
        before = len(self.log_lines())
        result = self._send(method, path, headers, body, host, content_length)
        self.wait_log(before + 1)
        return result

    def _send(self, method, path, headers, body, host, content_length):
        conn = http.client.HTTPConnection("127.0.0.1", self.gw.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not False:
            conn.putheader("Host", host or self.host)
        for k, v in headers:
            conn.putheader(k, v)
        if content_length is not None:
            conn.putheader("Content-Length", str(content_length))
        elif body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, resp.getheaders(), data

    def raw(self, request, half_close=False):
        """Send raw bytes, for requests http.client would not build. Returns (status, whole response).

        The status is None when the answer has no status line: before Python 3.13 the standard
        library answers a request line it cannot read with the error page alone, in HTTP/0.9 style.
        """
        before = len(self.log_lines())
        s = socket.create_connection(("127.0.0.1", self.gw.port), timeout=10)
        s.sendall(request)
        if half_close:
            s.shutdown(socket.SHUT_WR)  # end of input, without the empty line that ends the headers
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
        s.close()
        self.wait_log(before + 1)
        resp = b"".join(chunks)
        status = int(resp.split(b" ", 2)[1]) if resp.startswith(b"HTTP/") else None
        return status, resp

    def raw_oc_request(self, header_lines, body=None, extra_first=()):
        body = self.body() if body is None else body
        lines = [b"POST /v1/messages HTTP/1.1", b"Host: " + self.host.encode(),
                 b"Content-Length: %d" % len(body)] + list(extra_first) + list(header_lines)
        return b"\r\n".join(lines) + b"\r\n\r\n" + body

    def oc_lines(self):
        return [b"x-route-provider: opencode", b"x-gateway-token: " + self.gw.token.encode(),
                b"x-run-id: " + RUN_ID.encode(), b"x-data-class: general"]

    def assert_raw_refused(self, request, reason):
        status, resp = self.raw(request)
        self.assertEqual(status, 403, resp[:300])
        self.assertEqual((self.default.received, self.opencode.received), ([], []))
        self.assertNotIn(b"access-control-", resp.lower())
        rec = self.last_log()
        self.assertEqual((rec.get("route"), rec.get("reason")), ("refused", reason))

    def body(self, model=MODEL, **extra):
        d = {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": PROMPT}]}
        d.update(extra)
        return json.dumps(d).encode()

    def oc_headers(self, **override):
        h = {"x-route-provider": "opencode", "x-gateway-token": self.gw.token, "x-run-id": RUN_ID,
             "x-data-class": "general"}
        h.update(override)
        out = [(k, v) for k, v in h.items() if v is not None]
        return out + [("x-api-key", "CLIENT-SECRET-KEY"), ("Authorization", "Bearer CLIENT-OAUTH"),
                      ("anthropic-version", "2023-06-01"), ("Content-Type", "application/json")]

    def log_lines(self):
        if not os.path.exists(self.logfile):
            return []
        with open(self.logfile, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def wait_log(self, count):
        """The gateway writes its log line after the response; wait for the line, not a clock."""
        deadline = time.monotonic() + 5
        while len(self.log_lines()) < count and time.monotonic() < deadline:
            time.sleep(0.01)

    def last_log(self):
        lines = self.log_lines()
        return lines[-1] if lines else {}

    def assert_no_cors(self, headers):
        bad = [k for k, _ in headers if k.lower().startswith("access-control-")]
        self.assertEqual(bad, [], "Access-Control-* header relayed")

    def assert_refused(self, reason, method="POST", path="/v1/messages", headers=None, body=None, host=None):
        status, headers_out, data = self.send(method, path, self.oc_headers() if headers is None else headers,
                                              self.body() if body is None else body, host=host)
        self.assertEqual(status, 403, data)
        self.assertEqual(self.default.received, [], "a refused request reached the default stub")
        self.assertEqual(self.opencode.received, [], "a refused request reached the OpenCode stub")
        self.assert_no_cors(headers_out)
        rec = self.last_log()
        self.assertEqual((rec.get("route"), rec.get("reason")), ("refused", reason))
        return data


class TestDefaultRoute(GatewayCase):
    def test_default_route_unchanged(self):
        body = b'{"model":"any","messages":[],"x":"\xc3\xa9"}'
        sent = [("x-api-key", "CLIENT-KEY"), ("anthropic-version", "2023-06-01"), ("X-Custom", "a"),
                ("X-Custom", "b"), ("Content-Type", "application/json")]
        status, headers, data = self.send("POST", "/v1/messages?beta=true", sent, body)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.default.received), 1)
        self.assertEqual(self.opencode.received, [])
        got = self.default.received[0]
        self.assertEqual((got["method"], got["path"], got["body"]), ("POST", "/v1/messages?beta=true", body))
        forwarded = [(k.lower(), v) for k, v in got["headers"] if k.lower() != "host"]
        expected = [(k.lower(), v) for k, v in sent] + [("content-length", str(len(body)))]
        self.assertEqual(sorted(forwarded), sorted(expected))
        host = [v for k, v in got["headers"] if k.lower() == "host"]
        self.assertEqual(host, [self.default.url[len("http://"):]])
        self.assertEqual(data, json.dumps({"id": "msg_stub", "type": "message", "model": MODEL, "role": "assistant",
                                           "content": [{"type": "text", "text": "STUB-REPLY"}]}).encode())
        self.assertIn(("X-Stub", "yes"), headers)

    def test_default_route_strips_cors(self):
        status, headers, data = self.send("GET", "/v1/models", [("x-api-key", "CLIENT-KEY")])
        self.assertEqual(status, 200)
        self.assert_no_cors(headers)

    def test_default_route_streams(self):
        events = [start_event("whatever-model"), DELTA, STOP]
        self.default.answer = Stub.sse_answer(events)
        status, headers, data = self.send("POST", "/v1/messages", [("x-api-key", "K")], b'{"stream":true}')
        self.assertEqual(status, 200)
        self.assertEqual(data, b"".join(events))
        self.assert_no_cors(headers)
        self.assertEqual(self.opencode.received, [])

    def test_default_route_chunked_body(self):
        req = (b"POST /v1/messages HTTP/1.1\r\nHost: " + self.host.encode() + b"\r\nx-api-key: K\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n")
        status, resp = self.raw(req)
        self.assertEqual(status, 200)
        self.assertEqual(self.default.received[0]["body"], b"abcde")
        sent = [k.lower() for k, _ in self.default.received[0]["headers"]]
        self.assertNotIn("transfer-encoding", sent)  # the body goes on with a Content-Length instead
        self.assertIn("content-length", sent)

    def test_default_route_strips_hop_by_hop_response_headers(self):
        self.default.answer = Stub.json_answer(MODEL, extra=[("Keep-Alive", "timeout=5"), ("Upgrade", "h2c")])
        status, headers, data = self.send("GET", "/v1/models", [("x-api-key", "K")])
        names = [k.lower() for k, _ in headers]
        self.assertEqual(status, 200)
        self.assertNotIn("keep-alive", names)
        self.assertNotIn("upgrade", names)

    def test_default_route_head_answer_has_no_body(self):
        status, resp = self.raw(b"HEAD /v1/models HTTP/1.1\r\nHost: " + self.host.encode() + b"\r\nx-api-key: K\r\n\r\n")
        self.assertEqual(status, 200)
        head, _, body = resp.partition(b"\r\n\r\n")
        self.assertEqual(body, b"")
        self.assertIn(b"content-length: 141", head.lower())  # the upstream's own length, not a relayed body

    def test_default_route_drops_proxy_authorization(self):
        self.send("GET", "/v1/models", [("x-api-key", "K"), ("Proxy-Authorization", "Basic UFJPWFktU0VDUkVU")])
        sent = [k.lower() for k, _ in self.default.received[0]["headers"]]
        self.assertNotIn("proxy-authorization", sent)

    def header_lines(self, resp):
        return [line.split(b":", 1)[0].strip().lower() for line in resp.split(b"\r\n\r\n", 1)[0].split(b"\r\n")[1:]]

    def test_default_route_sends_one_content_length(self):
        status, resp = self.raw(b"GET /v1/models HTTP/1.1\r\nHost: " + self.host.encode() + b"\r\nx-api-key: K\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertEqual(self.header_lines(resp).count(b"content-length"), 1)  # Node rejects a repeated one

    def test_default_route_head_sends_one_content_length(self):
        status, resp = self.raw(b"HEAD /v1/models HTTP/1.1\r\nHost: " + self.host.encode() + b"\r\nx-api-key: K\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertEqual(self.header_lines(resp).count(b"content-length"), 1)

    def test_default_route_relays_upstream_status(self):
        self.default.answer = Stub.raw_answer(429, "application/json", b'{"type":"error","error":{"type":"rate"}}')
        status, _, data = self.send("POST", "/v1/messages", [("x-api-key", "K")], b"{}")
        self.assertEqual((status, data), (429, b'{"type":"error","error":{"type":"rate"}}'))


class TestOpenCodeRoute(GatewayCase):
    def test_opencode_route_forwards_with_user_key(self):
        body = self.body()
        status, headers, data = self.send("POST", "/v1/messages", self.oc_headers(), body)
        self.assertEqual(status, 200, data)
        self.assertEqual(self.default.received, [])
        self.assertEqual(len(self.opencode.received), 1)
        got = self.opencode.received[0]
        self.assertEqual((got["method"], got["path"], got["body"]), ("POST", "/zen/go/v1/messages", body))
        h = {}
        for k, v in got["headers"]:
            h.setdefault(k.lower(), []).append(v)
        self.assertEqual(h.get("authorization"), ["Bearer " + KEY])
        self.assertEqual(h.get("x-api-key"), [KEY])
        self.assertEqual(h.get("user-agent"), [gateway.USER_AGENT])
        self.assertEqual(h.get("anthropic-version"), [gateway.ANTHROPIC_VERSION])
        self.assertEqual(h.get("content-type"), ["application/json"])
        for name in gateway.ROUTE_HEADERS:
            self.assertNotIn(name, h)
        flat = json.dumps(got["headers"])
        for secret in ("CLIENT-SECRET-KEY", "CLIENT-OAUTH", self.gw.token):
            self.assertNotIn(secret, flat)
        self.assertIn(b"STUB-REPLY", data)
        self.assert_no_cors(headers)
        rec = self.last_log()
        self.assertEqual((rec["route"], rec["status"], rec["model_reported"], rec["run_id"]),
                         ("opencode", 200, MODEL, RUN_ID))

    def test_capitalised_route_headers_route_to_opencode(self):
        headers = [(k.title(), v) for k, v in self.oc_headers()]
        status, _, data = self.send("POST", "/v1/messages", headers, self.body())
        self.assertEqual(status, 200, data)
        self.assertEqual((len(self.default.received), len(self.opencode.received)), (0, 1))

    def test_upper_and_mixed_case_route_headers_route_to_opencode(self):
        mixed = lambda k: "".join(c.upper() if i % 2 else c for i, c in enumerate(k))  # x-RoUtE-PrOvIdEr
        for spell in (str.upper, mixed):
            self.opencode.received[:] = []
            headers = [(spell(k), v) for k, v in self.oc_headers()]
            status, _, data = self.send("POST", "/v1/messages", headers, self.body())
            self.assertEqual(status, 200, (spell("x-route-provider"), data))
            self.assertEqual((len(self.default.received), len(self.opencode.received)), (0, 1))

    def test_answers_carry_their_content_types(self):
        _, headers, _ = self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertIn(("Content-Type", "application/json"), headers)
        self.opencode.answer = Stub.sse_answer([start_event(MODEL), DELTA, STOP])
        _, headers, _ = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertIn(("Content-Type", "text/event-stream"), headers)

    def test_nonstream_cut_mid_body_withheld(self):
        body = json.dumps({"type": "message", "model": MODEL, "content": "RAW-UPSTREAM-TEXT"}).encode()
        head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % (len(body) + 50)
        self.opencode.answer = Stub.wire_answer(head, [body])  # 50 bytes short, then the upstream closes
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertEqual(status, 502)
        self.assertNotIn(b"RAW-UPSTREAM", data)

    def test_nonstream_chunked_cut_mid_chunk_withheld(self):
        body = json.dumps({"type": "message", "model": MODEL, "content": "RAW-UPSTREAM-TEXT"}).encode()
        head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n"
        self.opencode.answer = Stub.wire_answer(head, [b"%x\r\n" % (len(body) + 50) + body])  # the chunk never ends
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertEqual(status, 502)
        self.assertNotIn(b"RAW-UPSTREAM", data)
        self.assertEqual(self.last_log()["reason"], "upstream_unreadable")

    UPSTREAM_SECRET = b"RAW-UPSTREAM k-SECRETKEY-123"

    def raw_through_opencode(self, head_status, extra_headers, body, stream=False):
        """Send a raw OpenCode request; the stub answers with the given status line, headers and body."""
        head = (b"HTTP/1.1 " + head_status + b"\r\n" + b"".join(h + b"\r\n" for h in extra_headers)
                + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body))
        self.opencode.answer = Stub.wire_answer(head, [body])
        return self.raw(self.raw_oc_request(self.oc_lines(), body=self.body(stream=stream)))

    def test_upstream_status_line_headers_and_error_type_not_relayed_on_refusal(self):
        body = json.dumps({"type": "error", "error": {"type": self.UPSTREAM_SECRET.decode(),
                                                      "message": self.UPSTREAM_SECRET.decode()}}).encode()
        status, resp = self.raw_through_opencode(b"401 " + self.UPSTREAM_SECRET,
                                                 [b"Content-Type: application/json", b"X-Upstream-Debug: " + self.UPSTREAM_SECRET], body)
        self.assertEqual(status, 502)
        self.assertNotIn(b"RAW-UPSTREAM", resp)
        self.assertNotIn(b"SECRETKEY", resp)

    def test_upstream_status_line_and_headers_not_relayed_on_success(self):
        body = json.dumps({"type": "message", "model": MODEL, "content": [{"type": "text", "text": "fine"}]}).encode()
        status, resp = self.raw_through_opencode(b"200 " + self.UPSTREAM_SECRET,
                                                 [b"Content-Type: application/json", b"X-Upstream-Debug: " + self.UPSTREAM_SECRET], body)
        self.assertEqual(status, 200)
        self.assertNotIn(b"RAW-UPSTREAM", resp)
        self.assertNotIn(b"SECRETKEY", resp)

    def test_upstream_status_line_and_headers_not_relayed_on_stream(self):
        body = start_event(MODEL) + DELTA + STOP
        status, resp = self.raw_through_opencode(b"200 " + self.UPSTREAM_SECRET,
                                                 [b"Content-Type: text/event-stream", b"X-Upstream-Debug: " + self.UPSTREAM_SECRET], body, stream=True)
        self.assertEqual(status, 200)
        self.assertIn(b"message_stop", resp)
        self.assertNotIn(b"RAW-UPSTREAM", resp)
        self.assertNotIn(b"SECRETKEY", resp)

    def test_nonstream_nan_withheld(self):
        body = ('{"type":"message","model":"%s","usage":{"input_tokens":NaN},"note":"RAW-UPSTREAM-ERROR"}' % MODEL).encode()
        self.opencode.answer = Stub.raw_answer(200, "application/json", body)
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertEqual(status, 502)
        self.assertNotIn(b"RAW-UPSTREAM", data)

    def test_query_string_ignored(self):
        status, _, data = self.send("POST", "/v1/messages?beta=true", self.oc_headers(), self.body())
        self.assertEqual(status, 200, data)
        self.assertEqual(self.opencode.received[0]["path"], "/zen/go/v1/messages")

    def test_localhost_host_accepted(self):
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(),
                                    host="localhost:%d" % self.gw.port)
        self.assertEqual(status, 200, data)

    def test_sensitive_class_with_opencode_approved_passes(self):
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(**{"x-data-class": "internal"}),
                                    self.body())
        self.assertEqual(status, 200, data)

    def test_stream_relays(self):
        events = [start_event(MODEL), DELTA, STOP]
        self.opencode.answer = Stub.sse_answer(events)
        status, headers, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(status, 200)
        self.assertEqual(data, b"".join(rebuilt(e) for e in events))
        self.assert_no_cors(headers)

    def test_stream_model_mismatch_withheld(self):
        self.opencode.answer = Stub.sse_answer([start_event("some-other-model"), DELTA, STOP])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, gateway.WITHHELD_EVENT)
        self.assertNotIn(b"STREAM-TEXT", data)
        self.assertEqual(self.last_log()["reason"], "model_mismatch")

    def test_stream_later_error_replaced(self):
        raw_error = sse("error", {"type": "error", "error": {"type": "overloaded", "message": "RAW-UPSTREAM-ERROR"}})
        self.opencode.answer = Stub.sse_answer([start_event(MODEL), DELTA, raw_error, DELTA, STOP])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + gateway.WITHHELD_EVENT)
        self.assertNotIn(b"RAW-UPSTREAM-ERROR", data)

    def test_stream_non_json_data_withheld(self):
        bad = b"event: content_block_delta\ndata: RAW-UPSTREAM-ERROR upstream 500\n\n"
        self.opencode.answer = Stub.sse_answer([start_event(MODEL), DELTA, bad, DELTA, STOP])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + gateway.WITHHELD_EVENT)

    def test_stream_duplicate_key_error_withheld(self):
        bad = (b'event: content_block_delta\ndata: {"type":"content_block_delta","type":"error",'
               b'"error":{"type":"overloaded_error","message":"RAW-UPSTREAM-ERROR"}}\n\n')
        self.opencode.answer = Stub.sse_answer([start_event(MODEL), DELTA, bad, STOP])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + gateway.WITHHELD_EVENT)

    def test_stream_comment_not_relayed(self):
        comment = b": RAW-UPSTREAM-ERROR in a comment\n\n"
        self.opencode.answer = Stub.sse_answer([start_event(MODEL), comment, DELTA, STOP])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(STOP))

    def test_stream_later_model_switch_withheld(self):
        self.opencode.answer = Stub.sse_answer([start_event(MODEL), DELTA, start_event("some-other-model"), DELTA])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + gateway.WITHHELD_EVENT)

    def stream_through(self, *middle):
        """Stream message_start, DELTA, the given events and STOP through the gateway; return what arrived."""
        self.opencode.answer = Stub.sse_answer([start_event(MODEL), DELTA] + list(middle) + [STOP])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(status, 200)
        return data

    def assert_stream_withheld(self, *middle):
        data = self.stream_through(*middle)
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + gateway.WITHHELD_EVENT)
        self.assertNotIn(b"RAW-UPSTREAM", data)

    def test_stream_event_rebuilt_from_checked_fields(self):
        noisy = (b"event: content_block_delta\n: RAW-UPSTREAM-ERROR in a comment\nid: RAW-UPSTREAM-ID\n"
                 b"retry: 1000\nx-upstream: RAW-UPSTREAM-FIELD\ndata: " + DELTA_DATA + b"\n\n")
        data = self.stream_through(noisy)
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(DELTA) + rebuilt(STOP))
        self.assertNotIn(b"RAW-UPSTREAM", data)

    def test_stream_lone_cr_withheld(self):
        hidden = (b'x: 1\revent: error\rdata: {"type":"error","error":{"type":"api_error",'
                  b'"message":"RAW-UPSTREAM-ERROR"}}\r\rx: 2\nevent: content_block_delta\ndata: ' + DELTA_DATA + b"\n\n")
        self.assert_stream_withheld(hidden)

    def test_stream_data_only_non_string_type_withheld(self):
        self.assert_stream_withheld(b'data: {"type":["error"],"error":{"message":"RAW-UPSTREAM-ERROR"}}\n\n')

    def test_stream_data_only_non_json_withheld(self):
        self.assert_stream_withheld(b"data: RAW-UPSTREAM-ERROR upstream 500\n\n")

    def test_stream_type_not_a_plain_name_withheld(self):
        self.assert_stream_withheld(b'data: {"type":"text\\nevent: error","note":"RAW-UPSTREAM-ERROR"}\n\n')

    def test_stream_event_name_mismatch_withheld(self):
        self.assert_stream_withheld(b'event: content_block_delta\ndata: {"type":"message_delta","delta":{},'
                                    b'"note":"RAW-UPSTREAM-ERROR"}\n\n')

    def test_stream_nan_withheld(self):
        self.assert_stream_withheld(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":NaN,'
                                    b'"note":"RAW-UPSTREAM-ERROR"}\n\n')

    def test_stream_crlf_endings_relayed(self):
        events = [start_event(MODEL), DELTA, STOP]
        self.opencode.answer = Stub.sse_answer([e.replace(b"\n", b"\r\n") for e in events])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, b"".join(rebuilt(e) for e in events))

    def test_stream_out_of_range_number_withheld(self):
        self.assert_stream_withheld(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                                    b'"n":1e400,"note":"RAW-UPSTREAM-ERROR"}\n\n')
        self.assertEqual(self.last_log()["reason"], "upstream_error_event")

    def test_stream_negative_out_of_range_number_withheld(self):
        self.assert_stream_withheld(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                                    b'"n":-1e400,"note":"RAW-UPSTREAM-ERROR"}\n\n')

    def stream_ending_with(self, tail):
        """Stream message_start, DELTA and STOP, then the given bytes, then the upstream closes."""
        self.opencode.answer = Stub.sse_answer([start_event(MODEL), DELTA, STOP, tail])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(status, 200)
        return data

    def test_stream_unfinished_error_at_end_withheld(self):
        data = self.stream_ending_with(b'event: error\rdata: {"type":"error","error":{"type":"overloaded_error",'
                                       b'"message":"RAW-UPSTREAM-ERROR"}}\r\r')
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(STOP) + gateway.WITHHELD_EVENT)
        self.assertEqual(self.last_log()["reason"], "stream_truncated")

    def test_stream_ending_mid_event_withheld(self):
        data = self.stream_ending_with(b"event: content_block_delta\ndata: " + DELTA_DATA)
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(STOP) + gateway.WITHHELD_EVENT)

    def test_stream_ending_with_extra_line_breaks_relayed(self):
        data = self.stream_ending_with(b"\r\n")  # one line break left over after the last event
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(STOP))

    SSE_HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n"

    def test_stream_cut_at_event_boundary_under_content_length_withheld(self):
        full = start_event(MODEL) + DELTA + STOP
        head = self.SSE_HEAD + b"Content-Length: %d\r\n\r\n" % len(full)
        self.opencode.answer = Stub.wire_answer(head, [start_event(MODEL), DELTA])  # then closes, short
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + gateway.WITHHELD_EVENT)
        self.assertEqual(self.last_log()["reason"], "stream_truncated")

    def test_stream_cut_without_framing_withheld(self):
        self.opencode.answer = Stub.wire_answer(self.SSE_HEAD + b"\r\n", [start_event(MODEL), DELTA])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + gateway.WITHHELD_EVENT)

    def test_stream_complete_under_content_length_relayed(self):
        full = start_event(MODEL) + DELTA + STOP
        head = self.SSE_HEAD + b"Content-Length: %d\r\n\r\n" % len(full)
        self.opencode.answer = Stub.wire_answer(head, [start_event(MODEL), DELTA, STOP])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(STOP))
        self.assertIsNone(self.last_log()["reason"])

    def test_stream_upstream_dies_mid_chunk_withheld(self):
        start = start_event(MODEL)
        head = self.SSE_HEAD + b"Transfer-Encoding: chunked\r\n\r\n"
        self.opencode.answer = Stub.wire_answer(head, [b"%x\r\n%s\r\n" % (len(start), start), b"64\r\n" + DELTA[:10]])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start) + gateway.WITHHELD_EVENT)
        self.assertEqual(self.last_log()["reason"], "upstream_broken")

    def test_stream_empty_answer_withheld(self):
        self.opencode.answer = Stub.sse_answer([])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, gateway.WITHHELD_EVENT)
        self.assertEqual(self.last_log()["reason"], "stream_no_start")

    def test_stream_comments_only_withheld(self):
        self.opencode.answer = Stub.sse_answer([b": RAW-UPSTREAM-COMMENT\n\n"])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, gateway.WITHHELD_EVENT)
        self.assertEqual(self.last_log()["reason"], "stream_no_start")

    def test_stream_dataless_error_event_withheld(self):
        self.assert_stream_withheld(b"event: error\n\n")
        self.assertEqual(self.last_log()["reason"], "upstream_error_event")

    def test_stream_ping_before_start_not_relayed(self):
        ping = sse("ping", {"type": "ping", "note": "RAW-UPSTREAM-PING"})
        self.opencode.answer = Stub.sse_answer([ping, start_event(MODEL), DELTA, STOP])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(STOP))

    def test_stream_other_event_before_start_withheld(self):
        self.opencode.answer = Stub.sse_answer([DELTA, start_event(MODEL), STOP])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, gateway.WITHHELD_EVENT)
        self.assertEqual(self.last_log()["reason"], "stream_no_start")

    def test_nonstream_unreadable_body_withheld(self):
        self.opencode.answer = Stub.raw_answer(200, "application/json", b"RAW-UPSTREAM-ERROR, not JSON")
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertEqual(status, 502)
        self.assertNotIn(b"RAW-UPSTREAM-ERROR", data)
        self.assertEqual(self.last_log()["reason"], "upstream_unreadable")

    def test_nonstream_other_type_naming_model_withheld(self):
        body = json.dumps({"type": "overloaded_error", "model": MODEL, "message": "RAW-UPSTREAM-ERROR"}).encode()
        self.opencode.answer = Stub.raw_answer(200, "application/json", body)
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertEqual(status, 502)
        self.assertNotIn(b"RAW-UPSTREAM-ERROR", data)

    def assert_nonstream_withheld(self, answer):
        self.opencode.answer = Stub.raw_answer(200, "application/json", json.dumps(answer).encode())
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertEqual(status, 502)
        self.assertNotIn(b"RAW-UPSTREAM-ERROR", data)

    def test_nonstream_message_without_model_withheld(self):
        self.assert_nonstream_withheld({"type": "message", "content": [{"type": "text", "text": "RAW-UPSTREAM-ERROR"}]})

    def test_nonstream_redirect_withheld(self):
        self.opencode.answer = Stub.json_answer(MODEL, status=302)
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertEqual(status, 502)
        self.assertNotIn(b"STUB-REPLY", data)

    def test_nonstream_body_without_type_withheld(self):
        self.assert_nonstream_withheld({"model": MODEL, "error": {"message": "RAW-UPSTREAM-ERROR"}})

    def test_nonstream_type_in_other_case_withheld(self):
        self.assert_nonstream_withheld({"type": "Message", "model": MODEL, "note": "RAW-UPSTREAM-ERROR"})

    def test_nonstream_body_of_another_type_withheld(self):
        self.assert_nonstream_withheld({"type": "overloaded", "model": MODEL, "message": "RAW-UPSTREAM-ERROR"})

    def test_nonstream_error_body_naming_model_withheld(self):
        body = json.dumps({"type": "error", "model": MODEL, "error": {"message": "RAW-UPSTREAM-ERROR"}}).encode()
        self.opencode.answer = Stub.raw_answer(200, "application/json", body)
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertEqual(status, 502)
        self.assertNotIn(b"RAW-UPSTREAM-ERROR", data)

    def stream_of(self, *events):
        self.opencode.answer = Stub.sse_answer(list(events))
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(status, 200)
        return data

    def test_stream_ping_after_stop_ends_clean(self):
        data = self.stream_of(start_event(MODEL), PING, DELTA, STOP, PING)
        self.assertEqual(data, b"".join(rebuilt(e) for e in (start_event(MODEL), PING, DELTA, STOP, PING)))
        self.assertIsNone(self.last_log()["reason"])

    def test_stream_second_message_after_stop_withheld(self):
        data = self.stream_of(start_event(MODEL), DELTA, STOP, start_event("some-other-model"), DELTA, STOP)
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(STOP) + gateway.WITHHELD_EVENT)
        self.assertEqual(self.last_log()["reason"], "event_after_stop")

    def test_stream_error_after_stop_withheld(self):
        error = sse("error", {"type": "error", "error": {"type": "overloaded", "message": "RAW-UPSTREAM-ERROR"}})
        data = self.stream_of(start_event(MODEL), DELTA, STOP, error)
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(STOP) + gateway.WITHHELD_EVENT)

    def test_stream_content_after_stop_withheld(self):
        data = self.stream_of(start_event(MODEL), DELTA, STOP, DELTA, STOP)
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(STOP) + gateway.WITHHELD_EVENT)

    def test_stream_message_delta_after_stop_withheld(self):
        data = self.stream_of(start_event(MODEL), DELTA, STOP, MESSAGE_DELTA)
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(STOP) + gateway.WITHHELD_EVENT)

    def test_stream_chunked_clean_end_without_stop_withheld(self):
        data = self.stream_of(start_event(MODEL), DELTA)
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + gateway.WITHHELD_EVENT)
        self.assertEqual(self.last_log()["reason"], "stream_truncated")

    def test_stream_ending_on_block_stop_withheld(self):
        data = self.stream_of(start_event(MODEL), DELTA, BLOCK_STOP)
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(BLOCK_STOP) + gateway.WITHHELD_EVENT)

    def test_stream_ending_on_message_delta_withheld(self):
        data = self.stream_of(start_event(MODEL), DELTA, MESSAGE_DELTA)
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(MESSAGE_DELTA) + gateway.WITHHELD_EVENT)

    def test_stream_upper_case_type_withheld(self):
        self.assert_stream_withheld(b'data: {"type":"Content_Block_Delta","note":"RAW-UPSTREAM-ERROR"}\n\n')

    def test_stream_type_with_space_withheld(self):
        self.assert_stream_withheld(b'data: {"type":"content block delta","note":"RAW-UPSTREAM-ERROR"}\n\n')

    def test_stream_overlong_type_withheld(self):
        self.assert_stream_withheld(b'data: {"type":"' + b"a" * 65 + b'","note":"RAW-UPSTREAM-ERROR"}\n\n')

    def test_stream_type_with_trailing_newline_withheld(self):
        self.assert_stream_withheld(b'data: {"type":"content_block_delta\\n","note":"RAW-UPSTREAM-ERROR"}\n\n')

    def test_stream_event_name_case_mismatch_withheld(self):
        self.assert_stream_withheld(b"event: Content_block_delta\ndata: " + DELTA_DATA + b"\n\n")

    def test_stream_upstream_reset_withheld(self):
        head = self.SSE_HEAD + b"Transfer-Encoding: chunked\r\n\r\n"
        chunks = [b"%x\r\n%s\r\n" % (len(e), e) for e in (start_event(MODEL), DELTA)]
        self.opencode.answer = Stub.reset_after(head, chunks)
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + gateway.WITHHELD_EVENT)
        self.assertEqual(self.last_log()["reason"], "upstream_broken")

    def test_stream_comment_after_stop_ends_clean(self):
        data = self.stream_of(start_event(MODEL), DELTA, STOP, b": keep-alive\n\n")
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + rebuilt(STOP))
        self.assertIsNone(self.last_log()["reason"])

    def test_stream_ping_fields_are_dropped(self):
        noisy_ping = sse("ping", {"type": "ping", "error": {"message": "RAW-UPSTREAM-PING"}})
        data = self.stream_of(start_event(MODEL), noisy_ping, DELTA, STOP, noisy_ping)
        self.assertEqual(data, b"".join(rebuilt(e) for e in (start_event(MODEL), PING, DELTA, STOP, PING)))
        self.assertNotIn(b"RAW-UPSTREAM", data)

    def test_stream_non_ascii_text_relayed(self):
        delta = ('event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                 '"delta":{"type":"text_delta","text":"caf\u00e9 \u4e16\u754c \\ud83d \\u00e9"}}\n\n').encode("utf-8")
        data = self.stream_of(start_event(MODEL), delta, STOP)
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(delta) + rebuilt(STOP))
        self.assertIsNone(self.last_log()["reason"])

    def test_stream_charset_in_content_type_relayed(self):
        head = (b"HTTP/1.1 200 OK\r\nContent-Type: Text/Event-Stream; charset=utf-8\r\nConnection: close\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n")
        events = [start_event(MODEL), DELTA, STOP]
        self.opencode.answer = Stub.wire_answer(head, [b"%x\r\n%s\r\n" % (len(e), e) for e in events] + [b"0\r\n\r\n"])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, b"".join(rebuilt(e) for e in events))

    def test_stream_start_without_model_withheld(self):
        start = sse("message_start", {"type": "message_start", "message": {"id": "msg_stub"}})
        data = self.stream_of(start, DELTA, STOP)
        self.assertEqual(data, gateway.WITHHELD_EVENT)
        self.assertEqual(self.last_log()["reason"], "model_mismatch")

    def test_stream_start_message_not_an_object_withheld(self):
        data = self.stream_of(sse("message_start", {"type": "message_start", "message": MODEL}), DELTA, STOP)
        self.assertEqual(data, gateway.WITHHELD_EVENT)

    def test_stream_oversize_event_withheld(self):
        big = b"data: " + b"x" * (gateway.MAX_EVENT + 1024)  # never ends
        self.opencode.answer = Stub.sse_answer([start_event(MODEL), DELTA, big])
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body(stream=True))
        self.assertEqual(data, rebuilt(start_event(MODEL)) + rebuilt(DELTA) + gateway.WITHHELD_EVENT)
        self.assertEqual(self.last_log()["reason"], "event_too_large")

    def test_lone_lf_header_lines_accepted(self):
        body = self.body()
        lines = [b"POST /v1/messages HTTP/1.1", b"Host: " + self.host.encode(),
                 b"Content-Length: %d" % len(body)] + self.oc_lines()
        status, resp = self.raw(b"\n".join(lines) + b"\n\n" + body)
        self.assertEqual(status, 200, resp[:300])
        self.assertEqual((len(self.default.received), len(self.opencode.received)), (0, 1))

    def test_nonstream_model_mismatch_withheld(self):
        self.opencode.answer = Stub.json_answer("some-other-model")
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertEqual(status, 502)
        self.assertNotIn(b"STUB-REPLY", data)
        self.assertNotIn(b"some-other-model", data)

    def test_upstream_error_text_not_relayed(self):
        self.opencode.answer = Stub.raw_answer(400, "text/plain", b"RAW-UPSTREAM-ERROR details")
        status, _, data = self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertEqual(status, 502)
        self.assertNotIn(b"RAW-UPSTREAM-ERROR", data)


class TestRefusals(GatewayCase):
    def test_expect_continue_refusals_precede_provisional_response(self):
        cases = [("host", b"POST", b"evil.example", []),
                 ("origin", b"POST", self.host.encode(), [b"Origin: https://evil.example"]),
                 ("options", b"OPTIONS", self.host.encode(), [])]
        for reason, method, host, extra in cases:
            with self.subTest(reason=reason):
                lines = [method + b" /v1/messages HTTP/1.1", b"Host: " + host,
                         b"Expect: 100-continue", b"Content-Length: 1"] + self.oc_lines() + extra
                status, response = self.raw(b"\r\n".join(lines) + b"\r\n\r\n")
                self.assertNotIn(b"100 Continue", response)
                self.assertEqual(status, 403, response)
                self.assertEqual(self.last_log().get("reason"), reason)
                self.assertEqual((self.default.received, self.opencode.received), ([], []))
        _, accepted = self.raw(self.raw_oc_request(self.oc_lines() + [b"Expect: 100-continue"]))
        provisional, final = accepted.split(b"\r\n\r\n", 1)
        self.assertIn(b"100 Continue", provisional)
        self.assertTrue(final.startswith(b"HTTP/1.1 200"), final)
        self.assertEqual(len(self.opencode.received), 1)

    def test_refuse_foreign_host(self):
        self.assert_refused("host", host="evil.example:%d" % self.gw.port)

    def test_refuse_wrong_port_host(self):
        self.assert_refused("host", host="127.0.0.1:1")

    def test_refuse_foreign_host_on_default_route(self):
        self.assert_refused("host", headers=[("x-api-key", "K")], host="evil.example")

    def test_refuse_missing_host(self):
        self.assert_refused("host", host=False)

    def test_refuse_before_reading_body(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.gw.port, timeout=5)
        conn.putrequest("POST", "/v1/messages", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", "evil.example")
        conn.putheader("Content-Length", "1000000")
        conn.endheaders()
        resp = conn.getresponse()  # the body is never sent: a gateway that waited for it would time out
        self.assertEqual(resp.status, 403)
        conn.close()
        self.wait_log(1)
        self.assertEqual(self.last_log().get("reason"), "host")
        self.assertEqual((self.default.received, self.opencode.received), ([], []))

    def test_refuse_origin(self):
        self.assert_refused("origin", headers=self.oc_headers() + [("Origin", "http://127.0.0.1:%d" % self.gw.port)])

    def test_refuse_origin_on_default_route(self):
        self.assert_refused("origin", headers=[("x-api-key", "K"), ("Origin", "https://evil.example")])

    def test_refuse_options(self):
        self.assert_refused("options", method="OPTIONS", headers=[("Access-Control-Request-Method", "POST")], body=b"")

    def test_refuse_provider_wrong_case(self):
        self.assert_refused("provider", headers=self.oc_headers(**{"x-route-provider": "OpenCode"}))

    def test_refuse_provider_duplicated(self):
        self.assert_refused("provider", headers=self.oc_headers() + [("x-route-provider", "opencode")])

    def test_refuse_only_route_provider(self):
        self.assert_refused("token", headers=[("x-route-provider", "opencode"), ("x-api-key", "K")])

    def test_refuse_only_gateway_token(self):
        self.assert_refused("provider", headers=[("x-gateway-token", self.gw.token), ("x-api-key", "K")])

    def test_refuse_only_run_id(self):
        self.assert_refused("provider", headers=[("x-run-id", RUN_ID), ("x-api-key", "K")])

    def test_refuse_absolute_form_target(self):
        status, resp = self.raw(b"GET http://evil.example/v1/models HTTP/1.1\r\nHost: " + self.host.encode()
                                + b"\r\nx-api-key: K\r\n\r\n")
        self.assertEqual(status, 403)
        self.assertEqual((self.default.received, self.opencode.received), ([], []))
        self.assertEqual(self.last_log().get("reason"), "path")

    def test_refuse_route_headers_on_status_page(self):
        status, _, data = self.send("GET", "/_gateway/status", self.oc_headers())
        self.assertEqual(status, 403)
        self.assertNotIn(b"approved_models", data)

    def test_status_page_answers_only_get(self):
        status, _, data = self.send("POST", "/_gateway/status", [], b"")
        self.assertEqual(status, 404)
        self.assertNotIn(b"approved_models", data)

    def test_refuse_empty_origin(self):
        self.assert_refused("origin", headers=[("x-api-key", "K"), ("Origin", "")])

    def test_refuse_duplicate_host(self):
        req = self.raw_oc_request(self.oc_lines(), extra_first=[b"Host: evil.example"])
        status, resp = self.raw(req)
        self.assertEqual(status, 403)
        self.assertEqual((self.default.received, self.opencode.received), ([], []))
        self.assertEqual(self.last_log().get("reason"), "host")

    def test_refuse_run_id_of_wrong_length(self):
        self.assert_refused("run_id", headers=self.oc_headers(**{"x-run-id": RUN_ID + "0"}))

    def test_refuse_nul_in_header_value(self):
        self.assert_raw_refused(self.raw_oc_request([b"X-Pad: a\x00b"] + self.oc_lines()), "header_syntax")

    def test_refuse_other_transfer_encoding(self):
        self.assert_body_refused("body", [b"Transfer-Encoding: gzip, chunked"], b"5\r\nhello\r\n0\r\n\r\n")

    def test_refuse_capitalised_gateway_token_alone(self):
        self.assert_refused("provider", headers=[("X-Gateway-Token", self.gw.token), ("x-api-key", "K")])

    def test_refuse_upper_and_mixed_case_token_alone(self):
        for name in ("X-GATEWAY-TOKEN", "x-GaTeWaY-tOkEn"):
            self.assert_refused("provider", headers=[(name, self.gw.token), ("x-api-key", "K")])

    def test_refuse_empty_token(self):
        self.assert_refused("token", headers=self.oc_headers(**{"x-gateway-token": ""}))

    def test_refuse_partial_token(self):
        self.assert_refused("token", headers=self.oc_headers(**{"x-gateway-token": self.gw.token[:63]}))
        self.assert_refused("token", headers=self.oc_headers(**{"x-gateway-token": self.gw.token[1:]}))

    def test_refuse_route_headers_with_empty_values(self):
        for name in gateway.ROUTE_HEADERS:  # an empty value still makes it an OpenCode request
            self.assert_refused("provider", headers=[(name, ""), ("x-api-key", "K")])
        self.assert_refused("provider", headers=[(n, "") for n in gateway.ROUTE_HEADERS] + [("x-api-key", "K")])

    def test_refuse_empty_token_line_before_the_real_one(self):
        self.assert_refused("provider", headers=[("x-gateway-token", ""), ("x-gateway-token", self.gw.token),
                                                 ("x-api-key", "K")])

    def test_refuse_capitalised_run_id_alone(self):
        self.assert_refused("provider", headers=[("X-Run-Id", RUN_ID), ("x-api-key", "K")])

    def test_refuse_control_characters_in_header_value(self):
        for ch in (b"\x0c", b"\x1c", b"\x1f", b"\x7f"):
            self.default.received[:] = []
            self.assert_raw_refused(self.raw_oc_request([b"X-Pad: a" + ch + b"b"] + self.oc_lines()), "header_syntax")

    def test_refuse_only_data_class(self):
        self.assert_refused("provider", headers=[("x-data-class", "general"), ("x-api-key", "K")])

    def test_refuse_missing_token(self):
        self.assert_refused("token", headers=self.oc_headers(**{"x-gateway-token": None}))

    def test_refuse_wrong_token(self):
        self.assert_refused("token", headers=self.oc_headers(**{"x-gateway-token": "0" * 64}))

    def test_refuse_duplicated_token(self):
        self.assert_refused("token", headers=self.oc_headers() + [("x-gateway-token", self.gw.token)])

    def test_refuse_missing_run_id(self):
        self.assert_refused("run_id", headers=self.oc_headers(**{"x-run-id": None}))

    def test_refuse_bad_run_id(self):
        self.assert_refused("run_id", headers=self.oc_headers(**{"x-run-id": RUN_ID.upper()}))

    def test_refuse_duplicated_run_id(self):
        self.assert_refused("run_id", headers=self.oc_headers() + [("x-run-id", RUN_ID)])

    def test_refuse_get(self):
        self.assert_refused("method", method="GET", body=b"")

    def test_refuse_count_tokens(self):
        self.assert_refused("path", path="/v1/messages/count_tokens")

    def test_refuse_other_path(self):
        self.assert_refused("path", path="/v1/complete")

    def test_refuse_unknown_data_class(self):
        self.assert_refused("data_class", headers=self.oc_headers(**{"x-data-class": "no-such-class"}))

    def test_refuse_missing_data_class(self):
        self.assert_refused("data_class", headers=self.oc_headers(**{"x-data-class": None}))

    def test_refuse_duplicated_data_class(self):
        self.assert_refused("data_class", headers=self.oc_headers() + [("x-data-class", "general")])

    def test_refuse_sensitive_class_not_approved(self):
        self.assert_refused("data_class_not_approved", headers=self.oc_headers(**{"x-data-class": "client-data"}))

    def test_refuse_null_sensitive_class(self):
        self.assert_refused("data_class_not_approved", headers=self.oc_headers(**{"x-data-class": "unset-null"}))

    def test_refuse_class_without_sensitive_key(self):
        self.assert_refused("data_class_not_approved", headers=self.oc_headers(**{"x-data-class": "unset-missing"}))

    def test_refuse_string_false_sensitive_class(self):
        self.assert_refused("data_class_not_approved", headers=self.oc_headers(**{"x-data-class": "string-false"}))

    def test_refuse_non_json_body(self):
        self.assert_refused("body", body=b"not json")

    def test_refuse_non_object_body(self):
        self.assert_refused("body", body=b'["model"]')

    def test_refuse_duplicate_key_top(self):
        self.assert_refused("body", body=b'{"model":"%s","model":"%s"}' % (MODEL.encode(), MODEL.encode()))

    def test_refuse_duplicate_key_nested(self):
        self.assert_refused("body", body=b'{"model":"%s","messages":[{"role":"user","role":"x"}]}' % MODEL.encode())

    def test_refuse_unlisted_model(self):
        self.assert_refused("model_not_approved", body=self.body(model="fictional-coder-9"))

    def test_refuse_claude_in_model(self):
        self.assert_refused("model_name", body=self.body(model="fictional-CLAUDE-mirror"))

    def test_refuse_anthropic_in_model(self):
        self.assert_refused("model_name", body=self.body(model="fictional-AnThRoPiC-relay"))

    def test_refuse_malformed_header_line_before_route_headers(self):
        self.assert_raw_refused(self.raw_oc_request([b"X-Pad : 1"] + self.oc_lines()), "header_syntax")

    def test_refuse_space_before_colon_in_route_header(self):
        lines = self.oc_lines()
        lines[0] = b"x-route-provider : opencode"
        self.assert_raw_refused(self.raw_oc_request(lines), "header_syntax")

    def test_refuse_non_ascii_header_name(self):
        self.assert_raw_refused(self.raw_oc_request([b"X-\xe9t\xe9: 1"] + self.oc_lines()), "header_syntax")

    def test_refuse_obs_fold(self):
        self.assert_raw_refused(self.raw_oc_request([b"X-Foo: a", b" x-route-provider: opencode"]), "header_syntax")

    def test_refuse_obs_fold_on_default_route(self):
        self.assert_raw_refused(self.raw_oc_request([b"X-Foo: a", b"\tfolded"]), "header_syntax")

    def test_refuse_route_headers_folded_under_empty_values(self):
        tok, run = self.gw.token.encode(), RUN_ID.encode()
        lines = [b"X-A:", b" x-route-provider: opencode", b"X-B:", b" x-gateway-token: " + tok,
                 b"X-C:", b" x-run-id: " + run, b"X-D:", b" x-data-class: general"]
        self.assert_raw_refused(self.raw_oc_request(lines), "header_syntax")

    def test_refuse_empty_value_fold_on_default_route(self):
        self.assert_raw_refused(self.raw_oc_request([b"X-Foo:", b"\tfolded"]), "header_syntax")

    def test_refuse_lone_cr_in_header_line(self):
        self.assert_raw_refused(self.raw_oc_request(self.oc_lines(), extra_first=[b"X-Pad: 1\r"]), "header_syntax")

    def test_refuse_nameless_header_line(self):
        self.assert_raw_refused(self.raw_oc_request([b": sneaky"] + self.oc_lines()), "header_syntax")

    def test_refuse_control_character_in_header_value(self):
        self.assert_raw_refused(self.raw_oc_request([b"X-Pad: a\x0bb"] + self.oc_lines()), "header_syntax")

    def test_refuse_out_of_range_number_in_body(self):
        self.assert_refused("body", body=b'{"model":"%s","max_tokens":16,"temperature":1e400,"messages":[]}' % MODEL.encode())

    def test_refuse_negative_out_of_range_number_in_body(self):
        self.assert_refused("body", body=b'{"model":"%s","max_tokens":16,"temperature":-1e400,"messages":[]}' % MODEL.encode())

    def default_raw(self, header_lines, body=b"", half_close=False):
        lines = [b"POST /v1/messages HTTP/1.1", b"Host: " + self.host.encode(), b"x-api-key: K"] + header_lines
        return self.raw(b"\r\n".join(lines) + b"\r\n\r\n" + body, half_close=half_close)

    def assert_body_refused(self, reason, header_lines, body=b"", half_close=False):
        status, resp = self.default_raw(header_lines, body, half_close)
        self.assertEqual(status, 403, resp[:300])
        self.assertEqual((self.default.received, self.opencode.received), ([], []))
        self.assertEqual((self.last_log().get("route"), self.last_log().get("reason")), ("refused", reason))

    def test_refuse_content_length_with_transfer_encoding(self):
        self.assert_body_refused("body", [b"Content-Length: 10", b"Transfer-Encoding: chunked"], b"5\r\nhello\r\n0\r\n\r\n")

    def test_refuse_duplicate_content_length(self):
        self.assert_body_refused("body", [b"Content-Length: 5", b"Content-Length: 5"], b"hello")

    def test_refuse_short_body(self):
        self.assert_body_refused("body", [b"Content-Length: 100"], b"only ten b", half_close=True)

    def test_refuse_body_too_large(self):
        self.assert_body_refused("body_too_large", [b"Content-Length: %d" % (gateway.MAX_BODY + 1)], half_close=True)

    def test_refuse_chunks_too_large(self):
        self.assert_body_refused("body_too_large", [b"Transfer-Encoding: chunked"], b"%x\r\n" % (gateway.MAX_BODY + 1),
                                 half_close=True)

    def test_refuse_chunk_without_line_break(self):
        self.assert_body_refused("body", [b"Transfer-Encoding: chunked"], b"3\r\nabc0\r\n\r\n")

    def test_refuse_unterminated_header_block(self):
        lines = [b"POST /v1/messages HTTP/1.1", b"Host: " + self.host.encode()] + self.oc_lines()
        status, resp = self.raw(b"\r\n".join(lines) + b"\r\n", half_close=True)
        self.assertEqual(status, 403, resp[:300])
        self.assertEqual((self.default.received, self.opencode.received), ([], []))
        self.assertEqual((self.last_log().get("route"), self.last_log().get("reason")), ("refused", "header_syntax"))

    def test_refuse_http09(self):
        status, resp = self.raw(b"GET /_gateway/status\r\n\r\n")
        self.assertIsNone(status)  # an HTTP/0.9 answer has no status line
        rec = self.last_log()
        self.assertEqual((rec.get("route"), rec.get("reason"), rec.get("status")), ("refused", "http09", 403))
        self.assertNotIn(b"opencode_key_present", resp)

    def test_refuse_deeply_nested_body(self):
        deep = b'{"model":"%s","x":' % MODEL.encode() + b"[" * 100000 + b"]" * 100000 + b"}"
        self.assert_refused("body", body=deep)

    def test_refuse_unsupported_method_with_route_headers(self):
        self.assert_refused("method", method="PROPFIND")

    def test_refuse_bad_chunk_size(self):
        req = (b"POST /v1/messages HTTP/1.1\r\nHost: " + self.host.encode() +
               b"\r\nTransfer-Encoding: chunked\r\n\r\n0x3\r\nabc\r\n0\r\n\r\n")
        self.assert_raw_refused(req, "body")

    def test_malformed_request_is_logged(self):
        status, resp = self.raw(b"GARBAGE\r\n\r\n")
        self.assertIn(status, (400, None))  # None: no status line before Python 3.13 (see raw)
        self.assertIn(b"Error code: 400", resp)
        rec = self.last_log()
        self.assertEqual((rec.get("route"), rec.get("reason"), rec.get("status")), ("refused", "malformed_request", 400))
        self.assertEqual((self.default.received, self.opencode.received), ([], []))


class TestEventSplitter(unittest.TestCase):
    STREAM = b"".join(e.replace(b"\n", b"\r\n") for e in (start_event(MODEL), DELTA, STOP))
    EVENTS = [e[:-2] for e in (start_event(MODEL), DELTA, STOP)]

    def feed_all(self, pieces):
        splitter, out = gateway.EventSplitter(), []
        for piece in pieces:
            out += splitter.feed(piece)
        return out

    def test_crlf_normalised(self):
        self.assertEqual(self.feed_all([self.STREAM]), self.EVENTS)

    def test_split_at_every_byte_matches_whole(self):
        self.assertEqual(self.feed_all([self.STREAM[i:i + 1] for i in range(len(self.STREAM))]), self.EVENTS)

    def test_events_split_across_reads(self):
        half = len(DELTA) // 2
        self.assertEqual(self.feed_all([DELTA[:half], DELTA[half:]]), [DELTA[:-2]])

    def test_cr_cr_lf_split_across_reads_keeps_lone_cr(self):
        read1 = (b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"x"},"pad":[\r\r\n'
                 b'data: {"type":"error","error":{"type":"overloaded_error","message":"RAW-UPSTREAM-ERROR"}}\r\r\n')
        events = self.feed_all([read1, b"data: ]}\n\n"])
        self.assertEqual(len(events), 1)
        self.assertEqual(gateway.parse_event(events[0])[0], "error")

    def test_lone_cr_at_end_of_read_is_kept(self):
        events = self.feed_all([b'event: ping\ndata: {"type":"ping"}\n: c\r', b"x\n\n"])
        self.assertEqual(gateway.parse_event(events[0])[0], "error")


class TestSplitterCost(unittest.TestCase):
    def test_large_unfinished_event_in_small_reads_is_linear(self):
        # each read searches only the bytes it added: a whole-buffer search takes seconds to minutes here
        splitter, piece = gateway.EventSplitter(), b"x" * 64
        started = time.monotonic()
        for _ in range((2 * 1024 * 1024) // len(piece)):
            self.assertEqual(splitter.feed(piece), [])
        self.assertLess(time.monotonic() - started, 2.0)


class TestWithoutKey(GatewayCase):
    environ = {}

    def test_refuse_without_key(self):
        self.assert_refused("no_key")


class TestUnusableKey(GatewayCase):
    environ = {"OPENCODE_API_KEY": "not a\tusable key"}

    def test_refuse_with_unusable_key(self):
        self.assert_refused("no_key")


class TestSpacedKey(GatewayCase):
    environ = {"OPENCODE_API_KEY": "key with spaces"}

    def test_refuse_with_spaced_key(self):
        self.assert_refused("no_key")


class TestRecord(GatewayCase):
    def test_log_has_no_prompt_key_or_token(self):
        self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.send("POST", "/v1/messages", self.oc_headers(**{"x-data-class": "client-data"}), self.body())
        self.send("POST", "/v1/messages", [("x-api-key", "K")], self.body())
        self.opencode.answer = Stub.raw_answer(400, "text/plain", b"RAW-UPSTREAM-ERROR")
        self.send("POST", "/v1/messages", self.oc_headers(), self.body())
        self.assertGreaterEqual(len(self.log_lines()), 4)
        with open(self.logfile, encoding="utf-8") as f:
            text = f.read()
        for secret in (PROMPT, KEY, self.gw.token, "CLIENT-SECRET-KEY", "CLIENT-OAUTH", "RAW-UPSTREAM-ERROR"):
            self.assertNotIn(secret, text)

    def test_log_fields_and_truncation(self):
        long_model = "m" * 300
        self.send("POST", "/v1/messages", self.oc_headers(**{"x-run-id": "not-a-run-id"}), self.body())
        rec = self.last_log()
        self.assertIsNone(rec["run_id"])
        self.send("POST", "/v1/messages", self.oc_headers(), self.body(model=long_model))
        rec = self.last_log()
        self.assertEqual(set(rec), {"time", "route", "reason", "model_requested", "model_reported", "status", "run_id"})
        self.assertEqual(rec["model_requested"], "m" * 100)
        self.assertTrue(rec["time"].endswith("Z"))

    def test_status_endpoint(self):
        status, headers, data = self.send("GET", "/_gateway/status")
        self.assertEqual(status, 200)
        info = json.loads(data)
        self.assertIs(info["opencode_key_present"], True)
        self.assertEqual(info["approved_models"], ROUTES["providers"]["opencode"]["models"])
        self.assertEqual(info["routes_file"], os.path.abspath(self.routes))
        self.assertNotIn(KEY, data.decode())
        self.assertEqual((self.default.received, self.opencode.received), ([], []))
        self.assert_no_cors(headers)

    def test_idle_client_is_dropped(self):
        gw = gateway.Gateway(self.routes, self.logfile, port=0, environ={}, _client_timeout=0.3)
        gw.start(poll_interval=0.05)
        try:
            s = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
            s.sendall(b"POST /v1/messages HTTP/1.1\r\n")  # and then nothing more
            self.assertEqual(s.recv(1024), b"")  # the gateway gives up and closes the connection
            s.close()
        finally:
            gw.stop()

    def test_token_random_per_start(self):
        other = gateway.Gateway(self.routes, self.logfile, port=0, environ={})
        try:
            self.assertRegex(self.gw.token, r"^[0-9a-f]{64}$")
            self.assertNotEqual(self.gw.token, other.token)
        finally:
            other.server.server_close()

    def test_endpoints_are_constants(self):
        self.assertEqual(gateway.DEFAULT_UPSTREAM, "https://api.anthropic.com")
        self.assertEqual(gateway.OPENCODE_UPSTREAM, "https://opencode.ai/zen/go/v1/messages")
        with self.assertRaises(ValueError):
            gateway.Gateway(self.routes, self.logfile, port=0, environ={},
                            _test_upstreams={"default": "https://example.com", "opencode": self.opencode.url})
        listens = self.gw.server.server_address[0]
        self.assertEqual(listens, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
