#!/usr/bin/env bash
#
# Installer. Works out which patch it belongs to from the CLI next to it.
#
# Two things happen:
#   1. this repo's bin/ goes on your PATH, ahead of the real claude
#   2. the patch is turned on
#
# Step 1 matters because Claude Code updates itself, and an update replaces the
# binary and drops every patch. `/update` restarts by resolving `claude` on
# PATH and spawning it directly, not through your shell, so a shell alias is
# stepped over silently. A real file named `claude` earlier on PATH is not, and
# it re-applies every patch you have enabled before starting.
#
# Nothing here is irreversible. Turn the patch off with `<name> restore`, and
# remove the PATH line to undo step 1.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$DIR/bin"
DRY=0; NO_PATH=0; YES=0

CLI=""
for f in "$BIN"/claude-*; do
  [ -x "$f" ] && CLI="$f" && break
done
[ -n "$CLI" ] || { echo "no patch CLI found in $BIN" >&2; exit 1; }
NAME="$(basename "$CLI")"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --no-path) NO_PATH=1 ;;
    --yes|-y)  YES=1 ;;
    -h|--help)
      cat <<EOF
usage: ./install.sh [--dry-run] [--no-path] [--yes]

  --dry-run   show what would happen, change nothing
  --no-path   patch only, do not touch your shell config
              (the patch is then lost on the next Claude Code update)
  --yes       do not ask
EOF
      exit 0 ;;
  esac
done

say() { printf '%s\n' "$*"; }

chmod +x "$BIN"/claude "$CLI" 2>/dev/null || true

say "$NAME"
say ""
say "This will:"
[ "$NO_PATH" -eq 0 ] && say "  1. put $BIN at the front of your PATH"
say "  2. turn on $NAME"
say ""
say "Undo any time with: $NAME restore"
say ""

if [ "$DRY" -eq 1 ]; then
  say "(dry run, nothing changed)"
  "$CLI" doctor || true
  exit 0
fi

if [ "$YES" -eq 0 ] && [ -t 0 ]; then
  printf 'continue? [y/N] '
  read -r reply
  case "$reply" in [yY]*) ;; *) say "cancelled"; exit 1 ;; esac
fi

if [ "$NO_PATH" -eq 0 ]; then
  LINE="export PATH=\"$BIN:\$PATH\"  # $NAME"
  for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
    [ -f "$rc" ] || continue
    if grep -q "# claude-" "$rc" 2>/dev/null; then
      say "a launcher is already on your PATH via $rc, leaving it alone"
    else
      printf '\n%s\n' "$LINE" >> "$rc"
      say "added to $rc"
    fi
  done
  say ""
  say "open a new terminal, or run:  export PATH=\"$BIN:\$PATH\""
  say ""
fi

"$CLI" install

say ""
say "Check any time with:  $NAME status"
