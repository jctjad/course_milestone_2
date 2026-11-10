# E.3: the IRC gateway: channels and bridging

Routes covered: `JOIN`, `NAMES`, `WHO`, `LIST`, `TOPIC`, `MODE` and `PRIVMSG` on the gateway, and the bridge
between IRC and web messages.
Check: `bash /spec/component-E-staged/checks/check_E_3.sh`. The rule numbers below match its output.

Do not start this spec until `check_E_1.sh` and `check_E_2.sh` pass.

## Uses

| Function | From | Used here for |
|---|---|---|
| `get_workspace_by_slug(slug)`, `get_channel(...)` | A.1 | Turning `#slug/name` into a channel |
| `can_read_channel(channel, user_id)`, `add_channel_member(...)` | B.2, B.1 | Who may join; joining a public channel adds the member |
| `create_message(...)`, `message_to_json(message)` | B.2 | `PRIVMSG` from IRC posts a normal message |
| `emit_event(channel_id, kind, payload)` | B.5 | IRC messages use the same dense `event_id` stream as web messages |

## Adds

| Function | What it does |
|---|---|
| `irc_channel_name(workspace, channel)` | `#{workspace-slug}/{channel-name}`, e.g. `#acme/general`. The separator is a literal `/` |
| `irc_find_channel(name)` | The channel for an IRC name, or None (`#acme-general` is unknown) |
| `irc_pump()` | In the gateway: reads new `message.created` events from the shared store (like the nodes' event pump) and sends each to the IRC clients in that channel, except the one who sent it from IRC |

## Rules:

E3.1 **JOIN.** `JOIN #<slug>/<channel>` for a public channel sends the joiner's
  `:<nick>!<user>@<host> JOIN #<slug>/<channel>`, then `353` and `366` (`332` or `331` may also be sent). An
  unknown channel, including the hyphenated form `#<slug>-<channel>`, → `403`.

E3.2 **Accepted commands.** After joining, `NAMES`, `WHO`, `LIST`, `TOPIC` and `MODE` on the channel are
  accepted: none of them gets `421`.

E3.3 **IRC to web.** `PRIVMSG #<slug>/<channel> :<text>` from an IRC client is stored as a normal message by
  the token's user: it appears in `GET /api/channels/{id}/messages`, and web subscribers on every node receive
  it as `message.created`. Web and IRC writers share one dense `event_id` stream: a web post, an IRC message
  and another web post give three consecutive `event_id`s.

E3.4 **Web to IRC.** A message posted through the web API reaches every IRC client in that channel as
  `:<nick>!<user>@<host> PRIVMSG #<slug>/<channel> :<body>`, where `<nick>` is the author's username, within 2
  seconds.

E3.5 **Bridging while an HTTP node is down.** With the process on port `8001` killed, a web post through
  `8000` still reaches IRC clients in the channel, and a `PRIVMSG` from IRC still reaches web subscribers on
  `8002`. (The gateway is its own process and reads the shared store, so it does not need any one node.)

Every request must answer within 5 seconds.
