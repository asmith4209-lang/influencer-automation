#!/usr/bin/env bash
# Point every copy of the Apps Script deployment URL at a new /exec URL.
#
#   ./bookmarklet/set-url.sh https://script.google.com/macros/s/AKfyc.../exec
#
# Updates, in one shot:
#   bookmarklet/bookmarklet.js      (readable source)
#   bookmarklet/bookmarklet.min.js  (the line pasted into the browser bookmark)
#   watcher/.env                    (APPS_SCRIPT_URL, read by the dashboard health check)
#
# Then prints the ready-to-paste bookmarklet.
set -euo pipefail

URL="${1:-}"
if [ -z "$URL" ]; then
  echo "usage: $0 <apps-script-exec-url>" >&2
  exit 2
fi

# Restricting the shape also guarantees the URL is safe as a sed replacement.
if ! printf '%s' "$URL" | grep -Eq '^https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec$'; then
  echo "Refusing: '$URL' is not a .../macros/s/<id>/exec URL." >&2
  echo "Copy the Web app URL from Deploy -> Manage deployments (it ends in /exec)." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT/bookmarklet/bookmarklet.min.template.js"
MIN="$ROOT/bookmarklet/bookmarklet.min.js"
ENV="$ROOT/watcher/.env"

echo "Checking the deployment responds before wiring it in..."
BODY="$(curl -sL -m 20 "$URL" || true)"
if ! printf '%s' "$BODY" | grep -q '"status"'; then
  echo "WARNING: $URL did not return the doGet health JSON." >&2
  echo "         Check the deployment is 'Execute as: Me' / 'Who has access: Anyone'." >&2
  echo "         Wiring it in anyway - the dashboard health row will show red until it works." >&2
else
  echo "  OK: deployment answered $BODY"
fi

# bookmarklet.min.js and the install page carry the live URL, so they are
# generated here and gitignored - only the placeholder template is tracked.
sed "s|__APPS_SCRIPT_URL__|$URL|" "$TEMPLATE" > "$MIN"
if grep -q '__APPS_SCRIPT_URL__' "$MIN"; then
  echo "Refusing: placeholder survived substitution in $MIN" >&2
  exit 1
fi

if grep -q '^APPS_SCRIPT_URL=' "$ENV"; then
  sed -i -E "s|^APPS_SCRIPT_URL=.*|APPS_SCRIPT_URL=$URL|" "$ENV"
else
  printf 'APPS_SCRIPT_URL=%s\n' "$URL" >> "$ENV"
fi

# Regenerate the drag-to-install page so it never points at a stale URL.
python3 "$ROOT/bookmarklet/build-install-page.py"

echo
echo "Updated:"
grep -o 'var U="[^"]*"' "$MIN" | sed 's/^/  min.js: /'
grep -n '^APPS_SCRIPT_URL=' "$ENV" | sed 's/^/  .env: /'
echo
echo "Restart the dashboard so the health check picks up the new URL:"
echo "  cd $ROOT && docker compose restart dashboard"
echo
echo "--- paste this as the bookmark's URL ---"
cat "$MIN"
