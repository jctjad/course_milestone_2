# E.5: the web UI: sign-up, channels and messages

Routes covered: the SPA's behaviour in a browser (Chromium at 1280x900), using the API routes of the earlier
steps.
Check: `bash /spec/component-E-staged/checks/check_E_5.sh`. The rule numbers below match its output.

Do not start this spec until `check_E_1.sh` to `check_E_4.sh` pass.

The check drives the page with Playwright. It finds elements only through the pinned selectors (E.4) and the
extra selectors below, so use exactly these names. Keep every action instant: no animations longer than
200 ms, no `alert()`, `confirm()` or `prompt()`.

## Extra selectors (used by the checks; add them alongside the pinned ones)

| Selector | Where |
|---|---|
| `auth-form` has `data-mode="signup"` or `data-mode="login"`; `auth-toggle` switches it | The sign-up / login form; it opens in `signup` mode |
| inputs `name="username"`, `name="password"`, `name="display_name"` inside `auth-form` | `display_name` may be hidden in `login` mode |
| `data-testid="auth-error"` | Visible after a failed sign-up or login (the API's `400` or `401`): a sentence saying what is wrong, such as `Password must be at least 8 characters`, not a bare status code |
| inputs `name="slug"`, `name="name"` and a submit `data-testid="workspace-create-submit"` inside `workspace-create-form` | Shown (with `empty-state-create-workspace`) when the user has no workspace |
| input `name="name"` inside `create-channel-form`; `data-testid="create-channel-error"` | Before sending, the UI turns the typed name into a valid one: lowercase, spaces become hyphens (`Team Updates` → `team-updates`). The error is visible when creating a channel fails (for example a duplicate name, `409`): a sentence such as `A channel with that name already exists`, not a bare status code |
| `data-message-id="<id>"` on each `message` element | Also on messages in the thread panel |
| `data-testid="message-author"`, `message-time`, `message-avatar` | Inside each `message`: the author's display name, a human-readable time (like `3:07 PM`, never a raw ISO string) and an avatar element |
| `data-testid="message-edit-btn"`, `message-delete-btn` | In each `message`'s hover toolbar (they may be hidden until the message is hovered); `reaction-button` and `open-thread-btn` belong to the same toolbar |
| `data-testid="message-edit-input"` | The edit box that `message-edit-btn` opens; `Enter` saves, `Escape` cancels |
| `data-testid="message-edited"` | Visible text containing `edited`, inside a message whose `edited_at` is set |

**Login state.** After sign-up or login, store the token in `localStorage` under `huddle.token` and show the
app. On load, if `huddle.token` is set and `GET /api/auth/me` accepts it, skip the login form and open the
user's workspace (the most recently used one, or the first). `logout-btn` clears the token and shows
`auth-form` again.

## Rules:

E5.1 **Sign-up and login.** Before login, the page shows only the login screen: `logout-btn`,
  `message-input` and `new-channel-btn` are not visible. Each visible input in `auth-form` (`username`,
  `password`, `display_name`) has a visible `<label>` or `placeholder` naming it (for example `Username`), and
  `auth-submit` and `auth-toggle` show text saying what they do (for example `Sign up` and
  `Have an account? Log in`). Every visible button's text is readable against its background (contrast at
  least 3:1, for example dark text on a light button or white text on a dark one). In `signup` mode, an
  invalid sign-up (a password shorter than 8 characters) shows `auth-error` with a visible sentence of at least
  two words saying what is wrong (not just a status code) and does not log in. A valid sign-up logs in at
  once: `current-user` is visible and `localStorage["huddle.token"]` holds a token that `GET /api/auth/me`
  accepts. After the user creates a workspace with `workspace-create-form`, `logout-btn` returns to `auth-form`, and logging in again
  (in `login` mode) with the same username and password shows the same workspace in `workspace-header`.
  After the sign-up and after creating the workspace, each `data-testid` from E.4's first list matches exactly
  one element in the page (no hidden copies).

E5.2 **Channels.** `channel-list` lists the workspace's channels as `channel-entry` elements whose text starts
  with `#` and which carry `data-channel-id` and `data-channel-name`. `new-channel-btn` opens
  `create-channel-form`; submitting a new name adds the channel to the list and switches to it
  (`channel-title` shows its name). A name typed with capitals and a space, such as `Team 42`, creates
  `#team-42`. Submitting the same name again shows `create-channel-error` with a sentence of at least two words
  (not just a status code) and adds nothing. Clicking another `channel-entry` switches `channel-title` to that
  channel.

E5.3 **Messages.** Typing in `message-input` and pressing `send-btn` adds a `message` whose `message-body`
  is the text; sending an empty input adds nothing. Hovering a message and pressing `message-edit-btn`, then
  typing in `message-edit-input` and pressing `Enter`, changes its body and shows `message-edited`. Pressing
  `message-delete-btn` removes the message at once (no dialog). After a hard reload, the edited message still
  shows its new body and `message-edited`, and the deleted message is still gone.

Every request must answer within 5 seconds.
