#!/usr/bin/env bash
# Self-check for C.2 (DMs and files). Prints PASS/FAIL per rule in /spec/component-C-staged/C-2-dms-files.md.
# Usage: bash /spec/component-C-staged/checks/check_C_2.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

RFC3339='re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)$", d["file"]["created_at"]) is not None'
# header <name> <file> -> prints that header's value from a curl -D dump (case-insensitive), or nothing
header() { tr -d '\r' < "$2" | grep -i "^$1:" | head -1 | cut -d: -f2- | sed 's/^ *//'; }

rules_C_2() {
  local all="C2.1 C2.2 C2.3 C2.4 C2.5" at aid bt bid ct g d1 d2 d3 f1 f2 f3 f4 a b c e h l s1 s2 s3 s4 s5 s6 n f

  # setup: A owns a workspace; B and C are members of it
  new_user || { setup_fail "$all" "could not register a user, fix the basics first"; return; }
  at=$TOKEN; aid=$USER_ID
  new_ws "$at" || { setup_fail "$all" "could not create a workspace, fix the basics first"; return; }
  g=$GEN_ID
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  bt=$TOKEN; bid=$USER_ID
  new_member "$g" || { setup_fail "$all" "could not join #general, fix the basics first"; return; }
  ct=$TOKEN

  # C2.1 DMs: 200 {channel_id}; same id both ways and on repeat, on any node; both post and read; third user 403
  dm "$at" "$bid" $N0; a=$ST; d1=$DM_ID
  dm "$bt" "$aid" $N1; b=$ST; d2=$DM_ID
  dm "$at" "$bid" $N2; c=$ST; d3=$DM_ID
  if [ -n "$d1" ]; then
    post "$at" "$d1" "hello dm" $N0; s1=$ST
    post "$bt" "$d1" "hi back" $N1; s2=$ST
    l=$(call GET $N2/api/channels/$d1/messages "" "$bt"); s3=$(has 'sorted(m["body"] for m in d["messages"])==["hello dm","hi back"]')
    s4=$(call GET $N0/api/channels/$d1/messages "" "$ct")
  else s1=-; s2=-; l=-; s3=-; s4=-; fi
  dm "$at" 999999999 $N1; e=$ST
  new_user                               # Z: registered, in no workspace (if this fails, the DM below fails too)
  dm "$at" "$USER_ID" $N2; s5=$ST        # Z shares no workspace with A
  dm "$at" "$aid" $N0; s6=$ST            # DM with yourself
  dm "" "$bid" $N1; f=$ST                # no token
  if [ "$a" = 200 ] && [ -n "$d1" ] && [ "$b" = 200 ] && [ "$d2" = "$d1" ] && [ "$c" = 200 ] && [ "$d3" = "$d1" ] \
     && [ "$s1" = 201 ] && [ "$s2" = 201 ] && [ "$l" = 200 ] && [ "$s3" = yes ] && [ "$s4" = 403 ] && [ "$e" = 404 ] \
     && [ "$s5" = 200 ] && [ "$s6" = 400 ] && [ "$f" = 401 ]; then
    ok C2.1 "DMs: 200 {channel_id}, same channel both ways and on repeat; both users post and read; third user 403; unknown user 404; no shared workspace 200; self 400; no token 401"
  else
    bad C2.1 "DMs: A->B $a '$d1', B->A $b '$d2', again $c '$d3' (want 200 and one id); posts $s1/$s2 (want 201/201); B reads on 8002 $l, both messages: $s3 (want 200 yes); third user reads $s4 (want 403); unknown user $e (want 404); user with no shared workspace $s5 (want 200); yourself $s6 (want 400); no token $f (want 401)"
  fi

  # C2.2 upload: 201 {file: FileObj}; GET on another node; unknown 404; no file part 400
  printf 'quarterly numbers\nline two\n' > $TMPC.txt; n=$(wc -c < $TMPC.txt | tr -d ' ')
  upload "$at" $TMPC.txt report.txt text/plain $N0; a=$ST; f1=$FILE_ID
  s1=$(has 'str(d["file"]["uploader_id"])==a[0] and d["file"]["filename"]=="report.txt" and d["file"]["content_type"].split(";")[0]=="text/plain" and int(d["file"]["size"])==int(a[1])' "$aid" "$n")
  s2=$(has "$RFC3339")
  b=$(call GET $N2/api/files/$f1 "" "$bt"); s3=$(has 'str(d["file"]["id"])==a[0] and d["file"]["filename"]=="report.txt"' "$f1")
  c=$(call GET $N1/api/files/999999999 "" "$at")
  e=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST $N0/api/files -H "Authorization: Bearer $at" -F "note=no file here" 2>/dev/null || true)
  h=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST $N1/api/files -F "file=@$TMPC.txt;filename=report.txt;type=text/plain" 2>/dev/null || true)
  l=$(call GET $N2/api/files/$f1)
  if [ "$a" = 201 ] && [ -n "$f1" ] && [ "$s1" = yes ] && [ "$s2" = yes ] && [ "$b" = 200 ] && [ "$s3" = yes ] && [ "$c" = 404 ] && [ "$e" = 400 ] \
     && [ "$h" = 401 ] && [ "$l" = 401 ]; then
    ok C2.2 "upload: 201 {file} with uploader, filename, content_type, size, created_at; GET on any node; unknown 404; no file part 400; no token 401"
  else
    bad C2.2 "upload: got $a (id '$f1'; uploader/filename/content_type/size $n right: $s1; created_at RFC 3339: $s2); GET on 8002 $b (same file: $s3); unknown $c (want 404); no file part $e (want 400); no token upload $h, GET $l (want 401 each)"
  fi

  # C2.3 download: exact bytes, Content-Type, Content-Disposition with the filename, on any node (text and binary)
  if [ -n "$f1" ]; then
    a=$(curl -s -D $TMPC.h1 -o $TMPC.dl1 -w '%{http_code}' --max-time 5 $N1/api/files/$f1/download -H "Authorization: Bearer $bt" 2>/dev/null || true)
    cmp -s $TMPC.txt $TMPC.dl1 && s1=yes || s1=no
    h=$(header content-type $TMPC.h1); s2=$(header content-disposition $TMPC.h1)
  else a=-; s1=-; h=-; s2=-; fi
  head -c 3000 /dev/urandom > $TMPC.bin
  upload "$at" $TMPC.bin pixel.png image/png $N2; f2=$FILE_ID
  b=$(curl -s -D $TMPC.h2 -o $TMPC.dl2 -w '%{http_code}' --max-time 5 $N0/api/files/$f2/download -H "Authorization: Bearer $at" 2>/dev/null || true)
  cmp -s $TMPC.bin $TMPC.dl2 && s3=yes || s3=no
  s4=$(header content-type $TMPC.h2)
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 $N1/api/files/999999999/download -H "Authorization: Bearer $at" 2>/dev/null || true)
  if [ "$a" = 200 ] && [ "$s1" = yes ] && [ "${h%%;*}" = text/plain ] && [[ "$s2" == *report.txt* ]] \
     && [ "$b" = 200 ] && [ "$s3" = yes ] && [ "${s4%%;*}" = image/png ] && [ "$c" = 404 ]; then
    ok C2.3 "download: exact bytes with matching Content-Type and a Content-Disposition filename, text and binary, any node"
  else
    bad C2.3 "download: text on 8001 $a (same bytes: $s1, Content-Type '$h', Content-Disposition '$s2'); binary on 8000 $b (same bytes: $s3, Content-Type '$s4'); want 200, yes, text/plain, *report.txt*, 200, yes, image/png; unknown id $c (want 404)"
  fi

  # C2.4 size limit: exactly 10 MiB is accepted, one byte more is 413
  head -c 10485760 /dev/zero > $TMPC.10m; upload "$at" $TMPC.10m exact.bin application/octet-stream $N0; a=$ST
  head -c 10485761 /dev/zero > $TMPC.10m1; upload "$at" $TMPC.10m1 over.bin application/octet-stream $N1; b=$ST
  rm -f $TMPC.10m $TMPC.10m1
  if [ "$a" = 201 ] && [ "$b" = 413 ]; then
    ok C2.4 "size limit: exactly 10 MiB is 201; 10 MiB + 1 byte is 413"
  else
    bad C2.4 "size limit: 10485760 bytes got $a (want 201); 10485761 bytes got $b (want 413)"
  fi

  # C2.5 attach: own unattached file -> 201 with files; reused, someone else's or unknown -> 400 and no message
  upload "$at" $TMPC.txt notes.txt text/plain $N0; f3=$FILE_ID
  upload "$bt" $TMPC.txt theirs.txt text/plain $N1; f4=$FILE_ID
  post_json "$at" $g "{\"body\":\"with file\",\"file_ids\":$(ids_json $f3)}" $N0; a=$ST
  s1=$(has '[str(f["id"]) for f in d["message"]["files"]]==[a[0]] and d["message"]["files"][0]["filename"]=="notes.txt"' "$f3")
  post_json "$at" $g "{\"body\":\"reuse attempt\",\"file_ids\":$(ids_json $f3)}" $N1; b=$ST
  post_json "$at" $g "{\"body\":\"not my file\",\"file_ids\":$(ids_json $f4)}" $N2; c=$ST
  post_json "$at" $g "{\"body\":\"no such file\",\"file_ids\":$(ids_json 999999999)}" $N0; e=$ST
  l=$(call GET $N2/api/channels/$g/messages "" "$bt")
  s2=$(has 'any(m["body"]=="with file" and [str(f["id"]) for f in m["files"]]==[a[0]] for m in d["messages"]) and not any(m["body"] in ("reuse attempt","not my file","no such file") for m in d["messages"])' "$f3")
  if [ -n "$f3" ] && [ -n "$f4" ] && [ "$a" = 201 ] && [ "$s1" = yes ] && [ "$b" = 400 ] && [ "$c" = 400 ] && [ "$e" = 400 ] && [ "$l" = 200 ] && [ "$s2" = yes ]; then
    ok C2.5 "attach: own file 201 with files; reused, someone else's or unknown file 400 and nothing posted"
  else
    bad C2.5 "attach: own file $a (files right: $s1, want 201 yes); already attached $b, someone else's $c, unknown $e (want 400 each); history on 8002 $l shows only the good message with its file: $s2"
  fi
  rm -f $TMPC.txt $TMPC.bin $TMPC.dl1 $TMPC.dl2 $TMPC.h1 $TMPC.h2
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_C_2; finish; }
