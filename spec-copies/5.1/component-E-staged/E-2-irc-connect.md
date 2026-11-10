# E.2: the IRC gateway: connecting

Routes covered: a TCP IRC gateway on `0.0.0.0:6667`: `PASS`, `NICK`, `USER`, `PING`, `QUIT` and unknown
commands.
Check: `bash /spec/component-E-staged/checks/check_E_2.sh`. The rule numbers below match its output.

Do not start this spec until `check_E_1.sh` passes.

## Uses

| Function | From | Used here for |
|---|---|---|
| `user_from_token(token)` | A.1 | `PASS <token>`: the IRC client acts as that user |

## Adds

| Function | What it does |
|---|---|
| `irc_server()` | A separate gateway process, started (and restarted when it dies) by `/app/start.sh`, listening on `0.0.0.0:6667`. One thread per client; lines end with `\r\n` |
| `irc_register(client)` | After `PASS`, `NICK` and `USER`, sends `001` to `005` and then `422` |
| `irc_reply(client, code, text)` | Sends `:huddle <code> <nick> <text>` |

The gateway reads and writes the same database as the HTTP nodes; it never calls them over HTTP, so it keeps
working when one HTTP node is down.

**Nicks.** A client may pick any nick of letters, digits, `_` and `-`, up to 30 characters, as long as no
other connected client uses it. Messages that arrive from the web use the author's username as the nick.

## Rules:

E2.1 **Registration.** A client that sends `PASS <bearer token>`, then `NICK <nick>` and
  `USER <nick> 0 * :<name>` receives the numerics `001`, `002`, `003`, `004` and `005`, in that order,
  followed by `422` (no MOTD).

E2.2 **Bad password.** A `PASS` with an invalid token → `464`, and the client is not registered.

E2.3 **Nick collision.** `NICK` with a nick another connected client is using → `433`, and the connection
  stays open (a later `PING` still gets a `PONG`).

E2.4 **Other commands.** An unknown command → `421`. `PING :<text>` → `PONG` with the same text. `QUIT`
  closes the connection without an error reply (no `ERROR` line, no numeric).

Every request must answer within 5 seconds.
