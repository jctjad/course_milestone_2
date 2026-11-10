#!/usr/bin/env bash
# Self-check for E.6 (web UI: threads, reactions, layout). Prints PASS/FAIL per rule in /spec/component-E-staged/E-6-ui-threads-reactions.md.
# Usage: bash /spec/component-E-staged/checks/check_E_6.sh      Exit 0 only if every rule passes.
# Drives the page in headless Chromium (Playwright) at 1280x900; takes up to about 25 seconds.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_E_6() { run_ui e6 E6.1 E6.2 E6.3; }

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_E_6; finish; }
