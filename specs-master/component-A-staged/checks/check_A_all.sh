#!/usr/bin/env bash
# Runs the A.1, A.2, A.3 and A.4 checks in order and prints one total.
# Usage: bash /spec/component-A-staged/checks/check_A_all.sh      Exit 0 only if all 20 rules pass.
set -u
CHECK_ALL=1
D="$(dirname "${BASH_SOURCE[0]}")"
. "$D/check_A_1.sh"; . "$D/check_A_2.sh"; . "$D/check_A_3.sh"; . "$D/check_A_4.sh"
pass=0; fail=0
boot; rules_A_1; rules_A_2; rules_A_3; rules_A_4; finish
