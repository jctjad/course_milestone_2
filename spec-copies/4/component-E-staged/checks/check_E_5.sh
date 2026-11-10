#!/usr/bin/env bash
# Self-check for E.5 (web UI: sign-up, channels, messages). Prints PASS/FAIL per rule in /spec/component-E-staged/E-5-ui-flows.md.
# Usage: bash /spec/component-E-staged/checks/check_E_5.sh      Exit 0 only if every rule passes.
# Drives the page in headless Chromium (Playwright) at 1280x900; takes up to about 25 seconds.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_E_5() { run_ui e5 E5.1 E5.2 E5.3; }

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_E_5; finish; }
