#!/usr/bin/env bash
# End-to-end check of how ncsync compares files, on a throwaway repo where the
# "nc" remote is a local folder standing in for the server.
#   bash test_ncsync.sh [path/to/ncsync]
set -eu
SRC="${1:-$(cd "$(dirname "$0")" && pwd)/ncsync}"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/_sync" "$T/files/proj" "$T/server/proj" "$T/empty"; : > "$T/.repo-id"
cp "$SRC" "$T/_sync/ncsync"; : > "$T/_sync/filters.txt"; echo proj > "$T/_sync/folders.txt"
# a union keeps the name "nc" in rclone's state file names; an alias would not
printf '[nc]\ntype = union\nupstreams = %s %s\n' "$T/server" "$T/empty" > "$T/_sync/rclone.conf"
S="$T/server/proj"; D="$T/files/proj"; ST="$T/_sync/state"; L="$ST/nc_proj..local_proj.path2.lst"
bad=0

nc()    { bash "$T/_sync/ncsync" "$@" > "$T/out" 2>&1 || true; }
dates() { grep '"Modtime"' "$(ls -t "$T"/_sync/logs/*.log | head -1)" | tail -1 | grep -o 'true\|false'; }
mtime() { date -r "$1" +%s; }
shift_dates() { DT="$1" perl -e 'for (@ARGV) { $t = (stat)[9] + $ENV{DT}; utime $t, $t, $_ }' "${@:2}"; }
check() {
  if [ "$2" = "$3" ]; then echo "ok    $1"; return; fi
  echo "FAIL  $1: got '$2', want '$3'"; bad=1
  grep -hE "ERROR|NOTICE" "$(ls -t "$T"/_sync/logs/*.log | head -1)" | sed "s/^/        /" | tail -6
}

# history as written before dates were compared: checksums, no dates
printf 'v034\n' > "$S/a.txt"; printf 'hello\n' > "$S/notes.txt"
enc="$(sed -n 's/.*--local-encoding "\(.*\)"/\1/p' "$SRC")"
(cd "$T/files" && rclone bisync nc:proj proj --config "$T/_sync/rclone.conf" --workdir "$ST" \
  --filters-file "$T/_sync/filters.txt" --local-encoding "$enc" --resync --compare size,checksum -q)
mkdir -p "$ST/initialised"; date > "$ST/initialised/proj"

printf 'v035\n' > "$D/a.txt"; nc
check "old history keeps full checksums" "$(dates)" "false"
check "  and still sees a same-size edit" "$(cat "$S/a.txt")" "v035"

printf 'server\n' > "$S/notes.txt"; sleep 3; printf 'local!\n' > "$D/notes.txt"; nc --resync
check "fresh sync keeps the newer copy" "$(cat "$S/notes.txt")" "local!"
check "  and records every date" "$(grep -c ' 0001-01-01T' "$L" || true)" "0"

sleep 3; printf 'v036\n' > "$D/a.txt"; nc
check "then compares by date" "$(dates)" "true"
check "  and sees a same-size edit" "$(cat "$S/a.txt")" "v036"

before="$(mtime "$S/a.txt")"
shift_dates 3600 "$T/.repo-id" "$D"/*; nc
check "shifted drive dates stop the sync" "$(grep -c 'dates moved by +3600s' "$T/out")" "1"
check "  before anything is copied" "$(mtime "$S/a.txt") $(cat "$S/a.txt")" "$before v036"
shift_dates -3600 "$T/.repo-id" "$D"/*; nc
check "  shifting them back resumes syncing" "$(grep -c 'done' "$T/out") $(dates)" "1 true"
check "  still without copying" "$(mtime "$S/a.txt")" "$before"

exit $bad
