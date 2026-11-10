#!/usr/bin/env bash
# Runs the C.1 to C.5 checks in order and prints one total.
# Usage: bash /spec/component-C-staged/checks/check_C_all.sh      Exit 0 only if every C rule passes.
set -u
CHECK_ALL=1
D="$(dirname "${BASH_SOURCE[0]}")"
. "$D/check_C_1.sh"; . "$D/check_C_2.sh"; . "$D/check_C_3.sh"; . "$D/check_C_4.sh"; . "$D/check_C_5.sh"
pass=0; fail=0
boot; rules_C_1; rules_C_2; rules_C_3; rules_C_4; rules_C_5; finish
