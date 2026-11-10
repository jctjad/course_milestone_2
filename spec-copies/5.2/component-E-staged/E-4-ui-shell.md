# E.4: the web UI: page, selectors and button colours

Routes covered: `GET /` (the single-page app) on all three nodes.
Check: `bash /spec/component-E-staged/checks/check_E_4.sh`. The rule numbers below match its output.

Do not start this spec until `check_E_1.sh` to `check_E_3.sh` pass.

The UI is graded by an automated browser (Playwright selectors, then a Computer-Use agent at 1280x900), so
it must be found through the pinned attributes below, exactly as written. The Computer-Use agent works from
screenshots, like a person: every input and button must say on screen what it is for (E5.1, E6.3). If you
hide elements with the `hidden` attribute, add `[hidden]{display:none !important}` to your CSS: otherwise a
`display` rule on the same element (such as `.app{display:flex}`) overrides it and the element stays visible.

**Build.** Write the UI as one file, `/app/static/index.html`: plain HTML, CSS and JavaScript, no framework
and no build step. Every node serves it at `GET /`. Every pinned element below is written literally in that
file, so it is in the initial HTML; JavaScript only fills in data. Each pinned element exists once in the
page: do not keep hidden copies in the HTML while JavaScript renders a second set (the browser checks fail
when a pinned `data-testid` matches two elements). Never use `alert()`, `confirm()` or `prompt()`: the grader
cannot answer them.

## Adds

| Function | What it does |
|---|---|
| `serve_index()` | `GET /` returns `200` with `Content-Type: text/html` and the SPA, on every node |

## Pinned selectors (copied from the task; do not rename)

**`data-testid` present in the initial served HTML:** `auth-form`, `auth-submit`, `auth-toggle`,
`workspace-create-form`, `empty-state-create-workspace`, `join-workspace-form`, `join-workspace-submit`,
`channel-list`, `dms-list`, `new-channel-btn`, `current-user`, `logout-btn`, `workspace-header`,
`workspace-settings-btn`, `workspace-settings-close`, `workspace-name-input`, `workspace-join-mode`,
`workspace-general-submit`, `channel-title`, `channel-topic`, `channel-settings-btn`,
`channel-settings-title`, `channel-settings-close`, `channel-members-list`, `channel-add-member-form`,
`channel-add-member-input`, `channel-add-member-submit`, `channel-topic-input`, `channel-topic-submit`,
`archive-channel-btn`, `unarchive-channel-btn`, `create-channel-form`, `create-channel-private`,
`create-channel-submit`, `create-channel-cancel`, `message-list`, `message-input`, `send-btn`,
`thread-panel`, `thread-input`, `thread-send`, `close-thread`, `settings-tab-members`,
`settings-tab-invitations`, `settings-tab-general`, `settings-pane-members`, `settings-pane-invitations`,
`settings-pane-general`, `create-invitation-btn`, `create-invitation-form`, `create-invitation-submit`,
`invitations-list`.

**Created when the data renders:** `join-workspace-error`, `channel-entry`, `channel-member-row`, `message`,
`message-body`, `open-thread-btn`, `reaction-button`, `reaction-chip`, `emoji-picker`, `emoji-option`,
`member-row`, `role-badge`, `role-select`, `invitation-code`.

**DOM ids in the initial HTML:** `auth-modal`, `create-channel-modal`, `channel-settings-modal`,
`workspace-settings-modal`, `create-invitation-form`.

**Channel sidebar items** carry `data-channel-id="<id>"` and `data-channel-name="<name>"` on the clickable
element. **Token:** after login the SPA stores the bearer token in `localStorage` under `huddle.token`.

**Button roles.** Each button below carries `data-button-role` in the initial HTML, and at rest (not
hovered, focused or disabled) its computed `background-color` and `color` are exactly:

| Role | Background, text | Buttons |
|---|---|---|
| `primary` | `#007a5a`, `#ffffff` | `auth-submit`, `send-btn`, `thread-send`, `create-channel-submit`, `join-workspace-submit`, `workspace-general-submit`, `create-invitation-submit`, `channel-topic-submit`, `channel-add-member-submit` |
| `danger` | `#e01e5a`, `#ffffff` | `archive-channel-btn`, `logout-btn` |
| `secondary` | `#1264a3`, `#ffffff` | `new-channel-btn`, `create-invitation-btn`, `unarchive-channel-btn`, `create-channel-cancel`, `close-thread`, `channel-settings-close`, `workspace-settings-close`, `channel-settings-btn`, `workspace-settings-btn` |

Use plain solid colours on these buttons (no gradients, images or transparency).

## Rules:

E4.1 **The page.** `GET /` on `8000`, `8001` and `8002` returns `200` with `Content-Type: text/html`.

E4.2 **Pinned selectors.** The served HTML contains every `data-testid` from the first list and every DOM
  id above.

E4.3 **Button roles.** In the served HTML, each listed button carries its `data-button-role` (`primary`,
  `danger` or `secondary`).

E4.4 **Button colours.** Loaded in Chromium at 1280x900, each listed button's computed `background-color`
  and `color` at rest are exactly its role's colours: `rgb(0, 122, 90)` / `rgb(255, 255, 255)` for `primary`,
  `rgb(224, 30, 90)` / `rgb(255, 255, 255)` for `danger`, `rgb(18, 100, 163)` / `rgb(255, 255, 255)` for
  `secondary`. Hidden buttons count too (their styles still apply).

Every request must answer within 5 seconds.
