#!/usr/bin/env bash
# Runs the B.1 to B.6 checks in order and prints one total.
# Usage: bash /spec/component-B-staged/checks/check_B_all.sh      Exit 0 only if all 26 rules pass.
set -u
CHECK_ALL=1
D="$(dirname "${BASH_SOURCE[0]}")"
. "$D/check_B_1.sh"; . "$D/check_B_2.sh"; . "$D/check_B_3.sh"; . "$D/check_B_4.sh"; . "$D/check_B_5.sh"; . "$D/check_B_6.sh"
pass=0; fail=0
boot; rules_B_1; rules_B_2; rules_B_3; rules_B_4; rules_B_5; rules_B_6; finish
