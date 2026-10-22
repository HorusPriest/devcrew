# Gateway: send chosen tasks to an approved OpenCode model

`gateway.py` is a small local gateway between Claude Code and its model providers. A Claude Code session that opts in sends its requests to an approved OpenCode model on **your own** OpenCode account. Every other request goes to the default provider (`https://api.anthropic.com`) exactly as if the gateway were not there. Python standard library only.

## How it decides

- **No route headers: the default route.** A request that carries none of `x-route-provider`, `x-gateway-token`, `x-run-id` and `x-data-class` is forwarded unchanged (same method, path, headers apart from `Host`, and body), and the answer is relayed, streaming included. Hop-by-hop headers such as `Connection` belong to one connection and are not relayed.
- **Any route header: the OpenCode route.** The request goes to OpenCode only when every check below passes. Otherwise it is refused with `403`. It is never sent anywhere else, and never falls back to the default route.

An OpenCode request passes only when all of these hold:

1. exactly one `x-route-provider`, and its value is exactly `opencode` (lower case);
2. exactly one `x-gateway-token`, equal to the token this start of the gateway printed;
3. exactly one `x-run-id` of 32 lower-case hex characters;
4. it is a `POST` to `/v1/messages` (a query string is ignored). Token counting (`/v1/messages/count_tokens`) is refused on this route;
5. the body is a JSON object with no duplicated key at any depth;
6. its `model` is listed for `opencode` in your routes file, and does not contain `claude` or `anthropic` in any letter case;
7. exactly one `x-data-class` naming a class in your routes file. A class is sensitive unless it says exactly `"sensitive": false`, and a sensitive class passes only if it lists `opencode` under `approved_providers`;
8. `OPENCODE_API_KEY` was set in the gateway's environment when it started.

The calling session sets its own data-class label. The gateway validates that label against
your policy; it does not inspect the request's content to decide its sensitivity. Set the
label for the actual data in the task, rather than copying `general` from the example blindly.

Before any of that, and before it reads any body, the gateway refuses every HTTP/0.9 request and every request whose header block is not made only of plain header lines (each a name, a colon and a value, with no folded line, no stray CR, no space before the colon and no non-ASCII name), every request whose `Host` is not exactly `127.0.0.1:<port>` or `localhost:<port>`, every request that carries an `Origin` header, and every `OPTIONS` request. The gateway reads those lines itself and routes on what it read, never on Python's header parser: that parser stops at a malformed line and silently drops the route headers after it, and its handling of folded lines differs between Python versions. It never sends an `Access-Control-*` header and strips any it receives. Together these keep web pages in your browser from using it.

What goes upstream on the OpenCode route is built by the gateway: your key as `Authorization: Bearer` and `x-api-key`, a fixed `User-Agent`, `content-type` and `anthropic-version`. The credentials Claude Code sends, the gateway token and the route headers are never forwarded. The gateway checks the answer before you see it:

- a non-streamed answer is withheld (`502`) unless it is a complete `message` whose `model` is the approved id you asked for;
- a streamed answer is checked at its first `message_start` event; if the model is not the one you asked for, nothing more is relayed and the stream ends with a fixed error event;
- an error event that arrives later in a stream is replaced by that fixed error event, and the stream ends. So is any event whose data is not a JSON object with a plain lower-case `type` that matches its event name (including one that repeats a key, or holds NaN or a number too large to hold, such as `1e400`), any event holding a lone CR, and a second `message_start`;
- a stream that ends in the middle of an event, or ends without its closing `message_stop`, is ended with that fixed error event too, never cut short in silence. After `message_stop` only pings and comments may follow; any other event ends the stream with the fixed error event;
- every event that passes is rebuilt from its type and its data before it is relayed (a ping from its type alone), so comment lines, `id`, `retry` and any other field the upstream adds never reach you;
- OpenCode's own error text is never relayed.

The upstream endpoints are constants in `gateway.py`: `https://api.anthropic.com` and OpenCode's Anthropic-compatible messages endpoint, `https://opencode.ai/zen/go/v1/messages`. No flag or config value changes them. Check that endpoint against OpenCode's documentation before you rely on it.

Only models served on OpenCode's `/v1/messages` endpoint work through this gateway. Approving
an id does not convert its API: a model served only on another endpoint will fail upstream.
Consult the [current model list](https://opencode.ai/zen/go/v1/models) and
[OpenCode Go documentation](https://opencode.ai/docs/go/) when choosing ids. No startup
compatibility check is performed.

After the route is approved, the request body goes upstream byte for byte, including any
system prompt or `metadata` fields. The header filtering does not remove fields from the body.

## Set it up

1. Copy `routes.example.json` to `routes.json` (git ignores it). Replace the fictional model ids with the OpenCode model ids you approve, and mark your data classes. The example's ids do not exist.
2. Export your OpenCode key and start the gateway:

   ```
   export OPENCODE_API_KEY=<your OpenCode key>
   python gateway/gateway.py
   ```

   It listens on `127.0.0.1` only, on the `port` from `routes.json` (default 8787). At each start it prints a fresh random token once. The token is never written to disk or to the log; restart the gateway to get a new one.

3. Check it: `curl http://127.0.0.1:8787/_gateway/status` shows whether the key is present (never its value), the approved model ids and the routes file path.

## Launch a Claude Code session through it

```
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_CUSTOM_HEADERS="x-route-provider: opencode
x-gateway-token: <the token the gateway printed>
x-run-id: $(python -c 'import secrets; print(secrets.token_hex(16))')
x-data-class: general"
export ANTHROPIC_DEFAULT_HAIKU_MODEL=<an approved OpenCode model id>
export ANTHROPIC_SMALL_FAST_MODEL=<the same id>
claude --model <the same approved OpenCode model id>
```

The example uses one `Name: value` header per line in `ANTHROPIC_CUSTOM_HEADERS`. Model-variable
support and precedence depend on your installed client version; check its documentation.
For clients that honor the model variables above, set both to the approved id for background
calls. If subagents request another tier, configure that tier's model variable as well.
Any request that names an unapproved model is refused rather than sent elsewhere.

Expect refusals for anything the OpenCode route does not carry, such as token counting.

**Anything on this machine that holds the token can spend your OpenCode key** for as long as this start of the gateway runs. Keep the token out of shared shells, scripts and screenshots, and stop the gateway when you are done.

## Run a session without the gateway

Open a new shell, or clear the variables, and start Claude Code as usual:

```
unset ANTHROPIC_BASE_URL ANTHROPIC_CUSTOM_HEADERS ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_SMALL_FAST_MODEL
claude
```

To keep using the gateway's default route only, set `ANTHROPIC_BASE_URL` and leave out `ANTHROPIC_CUSTOM_HEADERS`: those requests go to the default provider unchanged.

## The log

One JSON line per request is appended to `gateway/gateway.log` (git ignores it; change it with `--log`). Each line holds the time (UTC), the route (`default`, `opencode`, `refused`, or `local` for the status page; a request too malformed to read is logged as `refused`), a fixed reason code for a refusal or a withheld answer, the model requested and the model reported (each cut to 100 characters), the HTTP status, and the run id when it is valid. It never holds the prompt, the response text, a key, the gateway token or an auth header.

## Tests

```
python gateway/test_gateway.py
```

The tests start two stub upstreams on `127.0.0.1` and point the gateway at them through a test-only constructor argument that accepts loopback addresses only. No test sends a request to OpenCode or Anthropic. `python smoke.py` runs them too.
