#!/usr/bin/env bash
# One-time setup: marks the folder above this one as the repo, works out the
# right WebDAV URL and writes rclone.conf beside this script, so every machine
# that mounts the drive shares it.
#
# Unlike a plain config write, this asks the server who you are and builds the
# URL from the answer - the user ID in a WebDAV path is often not the name you
# log in with.
set -eu

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SELF")"
CONF="$SELF/rclone.conf"

for tool in rclone curl; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "$tool is not installed."
    [ "$tool" = rclone ] && {
      echo "  macOS:  brew install rclone"
      echo "  Linux:  curl https://rclone.org/install.sh | sudo bash"; }
    exit 1
  }
done
RCV="$(rclone version 2>/dev/null | sed -n '1s/^rclone v//p')"
printf '1.74.2\n%s\n' "$RCV" | sort -V -C || {
  echo "rclone $RCV is too old - ncsync needs 1.74.2 or newer."
  echo "  macOS:  brew upgrade rclone"
  echo "  Linux:  curl https://rclone.org/install.sh | sudo bash"
  exit 1
}

# ncsync refuses to run without this marker, so it never fills an empty
# mountpoint; and its date is how ncsync notices exFAT dates shifting. It is
# written once and never touched again.
if [ ! -f "$REPO/.repo-id" ]; then
  echo "Synced folders will go to:  $REPO/files"
  printf 'Use this location? [Y/n] '
  read -r a
  case "$a" in n|N|no|No)
    echo "Move this folder into the one that should hold your files, e.g."
    echo "  /Volumes/MYDRIVE/nextcloud/_sync"
    echo "and run setup.sh from there."
    exit 1 ;;
  esac
  printf 'ncsync repo v1\ncreated %s\n' "$(date +%Y-%m-%d)" > "$REPO/.repo-id"
  mkdir -p "$REPO/files"
  echo "ok: $REPO/.repo-id"
  echo
fi

if [ -f "$CONF" ]; then
  printf 'rclone.conf already exists. Overwrite? [y/N] '
  read -r a; case "$a" in y|Y) ;; *) echo "Keeping existing config."; exit 0 ;; esac
fi

trim() { printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }

echo
echo "Create an APP PASSWORD first - do not use your login password:"
echo "  Settings -> Security -> Devices & sessions -> Create new app password"
echo

printf 'Nextcloud address (e.g. https://cloud.example.com): '
read -r URL
URL="$(trim "$URL")"
[ -n "$URL" ] || { echo "No address given."; exit 1; }
URL="${URL%/}"; URL="${URL%/index.php}"; URL="${URL%%/remote.php*}"
case "$URL" in http://*|https://*) ;; *) URL="https://$URL" ;; esac

printf 'Login name: '
read -r LOGIN
LOGIN="$(trim "$LOGIN")"

printf 'App password (hidden): '
stty -echo 2>/dev/null || true; read -r PASS; stty echo 2>/dev/null || true; echo
PASS="$(trim "$PASS")"
# Nextcloud displays app passwords in space-separated groups; strip them.
PASS="$(printf '%s' "$PASS" | tr -d ' ')"

echo
echo "--- 1. is the server reachable? -------------------------------------"
VER="$(curl -fsS --max-time 20 "$URL/status.php" 2>/dev/null || true)"
if [ -z "$VER" ]; then
  echo "FAILED: no answer from $URL/status.php"
  echo "Check the URL - and whether you need to be on the VPN."
  exit 1
fi
NCVER="$(printf '%s' "$VER" | sed -n 's/.*"versionstring"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
echo "ok: reachable${NCVER:+ - Nextcloud $NCVER}"

echo
echo "--- 2. do the credentials work? -------------------------------------"
OCS_URL="$URL/ocs/v2.php/cloud/user?format=json"
BODY="$(curl -sS --max-time 25 -u "$LOGIN:$PASS" -H 'OCS-APIRequest: true' \
        -w '\n%{http_code}' "$OCS_URL" 2>/dev/null || true)"
CODE="$(printf '%s' "$BODY" | tail -n1)"
BODY="$(printf '%s' "$BODY" | sed '$d')"

case "$CODE" in
  200) ;;
  401)
    echo "FAILED: 401 - the server rejected this login name + app password."
    echo
    echo "  * Regenerate the app password and copy the whole string."
    echo "  * Use your LOGIN name here, not your display name."
    echo "  * If your account signs in through SSO, app passwords may be"
    echo "    disabled - ask whoever runs the server."
    exit 1 ;;
  404)
    echo "FAILED: 404 - reached a web server, but not a Nextcloud OCS API."
    echo "Is there a path prefix, e.g. https://host/nextcloud ?"
    exit 1 ;;
  *)
    echo "FAILED: unexpected HTTP $CODE from the API."
    exit 1 ;;
esac

# canonical user id - this, not the login name, is what the WebDAV path needs
if command -v python3 >/dev/null 2>&1; then
  UID_NC="$(printf '%s' "$BODY" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["ocs"]["data"]["id"])' 2>/dev/null || true)"
fi
[ -n "${UID_NC:-}" ] || UID_NC="$(printf '%s' "$BODY" \
  | sed -n 's/.*"data":{[^}]*"id":"\([^"]*\)".*/\1/p')"
[ -n "$UID_NC" ] || UID_NC="$LOGIN"

echo "ok: authenticated as user id '$UID_NC'"
[ "$UID_NC" = "$LOGIN" ] || \
  echo "note: you typed '$LOGIN' - the WebDAV path needs '$UID_NC', using that."

echo
echo "--- 3. writing config -----------------------------------------------"
# via stdin, so the password never shows up in the process list
OBS="$(printf '%s' "$PASS" | rclone obscure -)"
unset PASS
umask 077
cat > "$CONF" <<EOF
[nc]
type = webdav
url = $URL/remote.php/dav/files/$UID_NC/
vendor = nextcloud
user = $LOGIN
pass = $OBS
EOF
echo "ok: $CONF"
echo "note: the app password in it is obscured, not encrypted. Anyone holding"
echo "      the drive can use it - revoke it in Nextcloud if the drive goes missing."

echo
echo "--- 4. listing your folders -----------------------------------------"
set_url() { sed -i.bak "s|^url = .*|url = $1|" "$CONF"; rm -f "$CONF.bak"; }
try_list() { rclone --config "$CONF" lsd nc: --max-depth 1 2>&1; }

if OUT="$(try_list)"; then
  printf '%s\n' "$OUT"
  DONE=1
else
  echo "The modern endpoint did not list. Trying the legacy one..."
  set_url "$URL/remote.php/webdav/"
  if OUT="$(try_list)"; then
    printf '%s\n' "$OUT"
    echo "(using the legacy /remote.php/webdav/ endpoint)"
    DONE=1
  else
    set_url "$URL/remote.php/dav/files/$UID_NC/"
    echo
    echo "Credentials are valid but WebDAV listing failed both ways:"
    printf '%s\n' "$OUT" | tail -n 5
    exit 1
  fi
fi

if [ "${DONE:-0}" = "1" ]; then
  echo
  echo "Connected. Next:"
  echo "  python3 $SELF/ncweb          pick folders in the browser"
  echo "  or edit $SELF/folders.txt    and run: bash $SELF/ncsync"
fi
