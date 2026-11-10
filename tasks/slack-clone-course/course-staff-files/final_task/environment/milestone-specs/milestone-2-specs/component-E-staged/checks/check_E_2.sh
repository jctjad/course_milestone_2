#!/usr/bin/env bash
# Self-check for E.2 (IRC gateway: connecting). Prints PASS/FAIL per rule in /spec/component-E-staged/E-2-irc-connect.md.
# Usage: bash /spec/component-E-staged/checks/check_E_2.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_E_2() {
  local all="E2.1 E2.2 E2.3 E2.4" t1 t2 n a b c e
  listening 6667 || { setup_fail "$all" "nothing is listening on port 6667: start the IRC gateway from /app/start.sh"; return; }
  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  t1=$TOKEN
  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  t2=$TOKEN

  # E2.1 PASS/NICK/USER -> 001..005 then 422
  a=$(irc register "$t1" $(new_nick))
  if [ "$a" = "001 002 003 004 005 422" ]; then
    ok E2.1 "registration: 001 to 005 then 422"
  else
    bad E2.1 "registration (PASS token, NICK, USER): numerics '$a' (want '001 002 003 004 005 422')"
  fi

  # E2.2 bad PASS -> 464, not registered
  b=$(irc badpass $(new_nick))
  if [[ " $b " == *" 464 "* ]] && [[ " $b " != *" 001 "* ]]; then
    ok E2.2 "bad password: 464 and no registration"
  else
    bad E2.2 "bad password: numerics '$b' (want 464 and no 001)"
  fi

  # E2.3 nick collision -> 433, connection stays open
  n=$(new_nick); c=$(irc collision "$t1" "$t2" $n)
  if [[ " $c " == *" 433 "* ]] && [[ "$c" == *alive ]]; then
    ok E2.3 "nick collision: 433 and the connection stays open"
  else
    bad E2.3 "nick collision: second client with the same nick got '$c' (want 433, then PING answered: 'alive')"
  fi

  # E2.4 unknown command 421; PING -> PONG; QUIT closes without an error reply
  e=$(irc misc "$t1" $(new_nick))
  if [[ "$e" == unknown=*421* ]] && [[ "$e" == *"ping=PONG"* ]] && [[ "$e" == *"quit=clean" ]]; then
    ok E2.4 "unknown command 421; PING gets PONG; QUIT closes cleanly"
  else
    bad E2.4 "other commands: got '$e' (want unknown=421, ping=PONG, quit=clean: closed with no ERROR line or numeric)"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_E_2; finish; }
