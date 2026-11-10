# E.6: the web UI: threads, reactions and layout

Routes covered: thread replies and reactions (API and UI), and the page layout.
Check: `bash /spec/component-E-staged/checks/check_E_6.sh`. The rule numbers below match its output.

Do not start this spec until `check_E_1.sh` to `check_E_5.sh` pass.

## Backend the UI needs

If Spec B is part of this run, threads are B2.6; if Spec C is part of this run, reactions are C.1. Reuse
them; do not write a second version. Otherwise build exactly this:

- **Threads.** `POST /api/channels/{id}/messages` with `parent_id` posts a reply (`201`); the parent's
  `reply_count` goes up by one. `GET /api/messages/{parent_id}/replies` returns
  `200 {replies: [MessageObj, ...], next_cursor}`, oldest first. Unknown parent → `404`.
- **Reactions.** `POST /api/messages/{id}/reactions` with `{emoji}` and
  `DELETE /api/messages/{id}/reactions/{emoji}` both return `200 {reactions}`, where `reactions` is
  `[{emoji, count, user_ids}]`, one entry per emoji; adding the same emoji twice changes nothing. Every
  `MessageObj` carries its `reactions`.

## Extra selectors (used by the checks)

| Selector | Where |
|---|---|
| `data-testid="thread-reply-count"` | Inside a parent `message` with replies: visible text like `1 reply` or `3 replies` |
| `data-testid="channel-empty-state"` | Visible, non-empty text in `message-list` when the channel has no messages |
| `data-testid="sidebar"` | The left pane that holds `workspace-header` and `channel-list` |

`thread-panel` is the right-hand pane: hidden until `open-thread-btn` is pressed, closed by `close-thread`.
`emoji-picker` is a grid of at least 8 `emoji-option` buttons, opened by `reaction-button` in a message's hover
toolbar; choosing one adds that reaction and closes the picker. Each reaction on a message is shown as a
`reaction-chip` with the emoji and its count.

## Rules:

E6.1 **Threads.** Hovering a message and pressing `open-thread-btn` shows `thread-panel` to the right of
  `message-list`. Typing in `thread-input` and pressing `thread-send` adds the reply inside `thread-panel`,
  and the parent message's `thread-reply-count` shows `1 reply`. `close-thread` hides the panel again.

E6.2 **Reactions.** Hovering a message and pressing `reaction-button` opens `emoji-picker` with at least 8
  `emoji-option`s (no browser dialog). Choosing one shows a `reaction-chip` on that message whose text
  contains the emoji's count, `1`, and the reaction is stored (`GET /api/channels/{id}/messages` lists it).

E6.3 **Layout and empty state.** `sidebar` has a solid, non-white background colour and shows
  `workspace-header` with the workspace name and `channel-list`. The channel header shows `channel-title`
  (the channel name) and `channel-topic` (its topic). Each `message` shows `message-avatar`, `message-author`,
  a `message-time` that is not a raw ISO timestamp, and `message-body`. A channel with no messages shows
  `channel-empty-state` with visible text. `thread-panel` is not visible until a thread is opened, and
  `create-channel-form` is not visible until `new-channel-btn` is pressed. The page reads like a real app to a
  person: every visible text input (such as `message-input`) has a visible `<label>` or `placeholder` (for example
  `Message #general`), every visible button shows text or an emoji or has an `aria-label`, button text is
  readable against its background (contrast at least 3:1), and `message-avatar` is
  a visible box at least 24x24 px (for example a coloured square with the author's initial).

Every request must answer within 5 seconds.
