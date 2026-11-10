#!/usr/bin/env bash
# Self-check for E.4 (web UI: page, selectors, button roles and colours). Prints PASS/FAIL per rule in /spec/component-E-staged/E-4-ui-shell.md.
# Usage: bash /spec/component-E-staged/checks/check_E_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TESTIDS="auth-form auth-submit auth-toggle workspace-create-form empty-state-create-workspace join-workspace-form join-workspace-submit channel-list dms-list new-channel-btn current-user logout-btn workspace-header workspace-settings-btn workspace-settings-close workspace-name-input workspace-join-mode workspace-general-submit channel-title channel-topic channel-settings-btn channel-settings-title channel-settings-close channel-members-list channel-add-member-form channel-add-member-input channel-add-member-submit channel-topic-input channel-topic-submit archive-channel-btn unarchive-channel-btn create-channel-form create-channel-private create-channel-submit create-channel-cancel message-list message-input send-btn thread-panel thread-input thread-send close-thread settings-tab-members settings-tab-invitations settings-tab-general settings-pane-members settings-pane-invitations settings-pane-general create-invitation-btn create-invitation-form create-invitation-submit invitations-list"
DOMIDS="auth-modal create-channel-modal channel-settings-modal workspace-settings-modal create-invitation-form"
ROLES="primary:auth-submit,send-btn,thread-send,create-channel-submit,join-workspace-submit,workspace-general-submit,create-invitation-submit,channel-topic-submit,channel-add-member-submit danger:archive-channel-btn,logout-btn secondary:new-channel-btn,create-invitation-btn,unarchive-channel-btn,create-channel-cancel,close-thread,channel-settings-close,workspace-settings-close,channel-settings-btn,workspace-settings-btn"

# inspect <html-file> -> prints "missing_testids|missing_ids|wrong_roles" (space-separated lists, "-" when none)
inspect() {
  python3 - "$1" "$TESTIDS" "$DOMIDS" "$ROLES" <<'PY'
import sys
from html.parser import HTMLParser
html, testids, domids, roles = open(sys.argv[1], encoding="utf-8", errors="replace").read(), sys.argv[2].split(), sys.argv[3].split(), sys.argv[4]
tags = []
class P(HTMLParser):
    def handle_starttag(self, tag, attrs): tags.append(dict(attrs))
    handle_startendtag = handle_starttag
P().feed(html)
have_t = {a.get("data-testid") for a in tags}; have_i = {a.get("id") for a in tags}
wrong = []
for part in roles.split():
    role, names = part.split(":")
    for n in names.split(","):
        els = [a for a in tags if a.get("data-testid") == n]
        if not els or els[0].get("data-button-role") != role:
            wrong.append(f"{n}={els[0].get('data-button-role') if els else 'absent'}")
fmt = lambda xs: " ".join(xs) or "-"
print(fmt([t for t in testids if t not in have_t]) + "|" + fmt([i for i in domids if i not in have_i]) + "|" + fmt(wrong))
PY
}

rules_E_4() {
  local a p r mt mi wr
  # E4.1 GET / on every node: 200 text/html
  a=""
  for p in 8000 8001 8002; do
    r=$(curl -s -o $TMPC.index$p.html -w '%{http_code} %{content_type}' --max-time 5 "http://127.0.0.1:$p/" 2>/dev/null || true)
    a="$a $p:${r:-000}"
  done
  if [[ "$a" =~ 8000:200\ text/html.*8001:200\ text/html.*8002:200\ text/html ]]; then
    ok E4.1 "GET / answers 200 text/html on 8000, 8001 and 8002"
  else
    bad E4.1 "GET /: got '$a' (want 200 text/html on each port)"
  fi

  # E4.2 every pinned data-testid and DOM id is in the served HTML;  E4.3 each pinned button has its data-button-role
  r=$(inspect $TMPC.index8000.html 2>/dev/null || echo "?|?|?"); mt=${r%%|*}; mi=${r#*|}; mi=${mi%%|*}; wr=${r##*|}
  if [ "$mt" = - ] && [ "$mi" = - ]; then
    ok E4.2 "every pinned data-testid and DOM id is in the served HTML"
  else
    bad E4.2 "pinned selectors missing from the HTML served on 8000: data-testid [$mt]; DOM ids [$mi]"
  fi
  if [ "$wr" = - ]; then
    ok E4.3 "every pinned button carries its data-button-role"
  else
    bad E4.3 "buttons without the right data-button-role (testid=found): $wr"
  fi
  rm -f $TMPC.index800{0,1,2}.html

  # E4.4 computed button colours in headless Chromium at 1280x900
  run_ui colours E4.4
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_E_4; finish; }
