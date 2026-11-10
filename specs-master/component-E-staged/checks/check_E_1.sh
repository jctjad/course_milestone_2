#!/usr/bin/env bash
# Self-check for E.1 (editing and deleting messages). Prints PASS/FAIL per rule in /spec/component-E-staged/E-1-edit-delete.md.
# Usage: bash /spec/component-E-staged/checks/check_E_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

RFC3339='re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)$", str(d["message"]["edited_at"])) is not None'
# GONE: none of the given message ids is listed, unless marked deleted with no reactions
GONE='all(str(m["id"]) not in a or ((m.get("deleted") or m.get("is_deleted")) and not m.get("reactions")) for m in d["messages"])'

rules_E_1() {
  local all="E1.1 E1.2 E1.3 E1.4" ot mt xt g m1 m2 m3 m4 a b c e f h l s1 s2 s3 s4 s5

  # setup: owner O; members M (the author) and X
  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  ot=$TOKEN
  new_ws "$ot" || { setup_fail "$all" "could not create a workspace, fix the basics first"; return; }
  g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  mt=$TOKEN
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  xt=$TOKEN
  post "$mt" $g "original text" $N0; m1=$MSG_ID; s1=$(has 'd["message"]["edited_at"] is None')
  [ -n "$m1" ] || { setup_fail "$all" "could not post a message (got $ST), fix the basics first"; return; }

  # E1.1 edit by the author: 200 with the new body and edited_at; seen on another node; empty 400; unknown 404
  a=$(call PATCH $N1/api/messages/$m1 '{"body":"edited text"}' "$mt"); s2=$(has 'd["message"]["body"]=="edited text"'); s3=$(has "$RFC3339")
  b=$(call GET $N2/api/channels/$g/messages "" "$mt"); s4=$(has 'any(str(m["id"])==a[0] and m["body"]=="edited text" and m["edited_at"] for m in d["messages"])' "$m1")
  c=$(call PATCH $N0/api/messages/$m1 '{"body":""}' "$mt"); e=$(call PATCH $N0/api/messages/999999999 '{"body":"x"}' "$mt")
  if [ "$s1" = yes ] && [ "$a" = 200 ] && [ "$s2" = yes ] && [ "$s3" = yes ] && [ "$b" = 200 ] && [ "$s4" = yes ] && [ "$c" = 400 ] && [ "$e" = 404 ]; then
    ok E1.1 "edit: 200 with the new body and edited_at; seen on another node; empty body 400; unknown id 404"
  else
    bad E1.1 "edit: edited_at null before the edit: $s1; PATCH by the author $a (new body: $s2, edited_at RFC 3339: $s3; want 200 yes yes); history on 8002 $b shows it: $s4; empty body $c (want 400); unknown id $e (want 404)"
  fi

  # E1.2 only the author may edit (not even the owner); no token 401
  a=$(call PATCH $N2/api/messages/$m1 '{"body":"owner edit"}' "$ot"); b=$(call PATCH $N1/api/messages/$m1 '{"body":"other edit"}' "$xt")
  c=$(call PATCH $N0/api/messages/$m1 '{"body":"no token"}')
  if [ "$a" = 403 ] && [ "$b" = 403 ] && [ "$c" = 401 ]; then
    ok E1.2 "edit permissions: owner and other members 403; no token 401"
  else
    bad E1.2 "edit permissions: workspace owner $a, other member $b (want 403 each); no token $c (want 401)"
  fi

  # E1.3 delete: author or owner; 200 {"deleted":true}, again 200; others 403; gone from history; edit after 409
  post "$mt" $g "delete me" $N0; m2=$MSG_ID; post "$mt" $g "owner deletes me" $N0; m3=$MSG_ID
  a=$(call DELETE $N1/api/messages/$m2 "" "$xt")
  b=$(call DELETE $N2/api/messages/$m2 "" "$mt"); s1=$(has 'd.get("deleted") is True')
  c=$(call DELETE $N0/api/messages/$m2 "" "$mt"); s2=$(has 'd.get("deleted") is True')
  e=$(call DELETE $N1/api/messages/$m3 "" "$ot"); s3=$(has 'd.get("deleted") is True')
  f=$(call GET $N2/api/channels/$g/messages "" "$mt"); s4=$(has "$GONE" "$m2" "$m3")
  h=$(call PATCH $N0/api/messages/$m2 '{"body":"too late"}' "$mt")
  if [ "$a" = 403 ] && [ "$b" = 200 ] && [ "$s1" = yes ] && [ "$c" = 200 ] && [ "$s2" = yes ] && [ "$e" = 200 ] && [ "$s3" = yes ] \
     && [ "$f" = 200 ] && [ "$s4" = yes ] && [ "$h" = 409 ]; then
    ok E1.3 "delete: author and owner 200 deleted:true (again 200); others 403; gone from history; editing a deleted message 409"
  else
    bad E1.3 "delete: other member $a (want 403); author $b ($s1), again $c ($s2), owner $e ($s3) (want 200 deleted:true each); history on 8002 $f leaves them out: $s4; edit after delete $h (want 409)"
  fi

  # E1.4 events: message.updated then message.deleted, dense, on every node; deleting again emits nothing
  post "$mt" $g "event me" $N0; m4=$MSG_ID
  listen_ports "$mt" $g 2 6 8000 8001 8002
  s5="$(call PATCH $N0/api/messages/$m4 '{"body":"event edit"}' "$mt") $(call DELETE $N1/api/messages/$m4 "" "$mt") $(call DELETE $N2/api/messages/$m4 "" "$mt")"
  wait "${LPIDS[@]}" 2>/dev/null
  l=$(verdict_ports $g "message.updated,message.deleted")
  rm -f ${WSF}800{0,1,2}.jsonl
  if [ "$s5" = "200 200 200" ] && [ "$l" = yes ]; then
    ok E1.4 "events: message.updated and message.deleted with consecutive event_ids on every node; a repeat delete emits nothing"
  else
    bad E1.4 "events: edit, delete, delete again gave '$s5' (want 200 200 200); every node should get message.updated then message.deleted at head+1, head+2: $l"
  fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_E_1; finish; }
