#!/bin/bash
# install.sh - set up the prompt-cache status line segment for Claude Code.
#
#   curl -fsSL https://raw.githubusercontent.com/jimmc414/claude_cache_statusline/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --project
#
#   ./install.sh [--global]          every session, via ~/.claude/settings.json (default)
#   ./install.sh --project [DIR]     one project only, via DIR/.claude/settings.local.json
#   ./install.sh --uninstall [--global | --project [DIR]]
#
# Installs cache_warm.sh next to the settings file it configures. If no status
# line is configured yet, it sets one up (after backing the settings file up).
# An existing custom status line is never modified or shadowed: the installer
# prints the lines to add to it instead.
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/jimmc414/claude_cache_statusline/main"
GLOBAL_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
REFRESH=30

die() { echo "error: $*" >&2; exit 1; }

action=install; scope=global; project_dir=""
while [ $# -gt 0 ]; do
    case "$1" in
        --global)     scope=global ;;
        --project)    scope=project
                      if [ $# -gt 1 ] && [ "${2#--}" = "$2" ]; then project_dir=$2; shift; fi ;;
        --uninstall)  action=uninstall ;;
        -h|--help)    sed -n '2,14p' "${BASH_SOURCE[0]:-/dev/null}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

if [ "$scope" = project ]; then
    [ -d "${project_dir:-.}" ] || die "not a directory: $project_dir"
    project_dir=$(cd "${project_dir:-.}" && pwd)
    CLAUDE_DIR="$project_dir/.claude"
    SETTINGS="$CLAUDE_DIR/settings.local.json"
else
    CLAUDE_DIR="$GLOBAL_DIR"
    SETTINGS="$CLAUDE_DIR/settings.json"
fi
DEST="$CLAUDE_DIR/cache_warm.sh"
CMD="bash \"$DEST\""

need_jq() {
    command -v jq >/dev/null 2>&1 && return
    echo "error: jq is required but was not found." >&2
    case "$(uname -s)" in
        Darwin) echo "  install it with: brew install jq" >&2 ;;
        *)      echo "  install it with: sudo apt install jq   (or dnf / pacman / apk)" >&2 ;;
    esac
    exit 1
}

# Copy from a checkout when there is one next to this file; otherwise download.
# Under `curl | bash` BASH_SOURCE is empty, so that path always downloads.
fetch_script() {
    local here=""
    [ -n "${BASH_SOURCE[0]:-}" ] && here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    if [ -n "$here" ] && [ -f "$here/cache_warm.sh" ]; then
        [ "$here/cache_warm.sh" -ef "$DEST" ] || cp "$here/cache_warm.sh" "$DEST"
    else
        command -v curl >/dev/null 2>&1 || die "curl is required to download cache_warm.sh"
        curl -fsSL "$REPO_RAW/cache_warm.sh" -o "$DEST.download" || { rm -f "$DEST.download"; die "download failed: $REPO_RAW/cache_warm.sh"; }
        # Guard against saving an error page in place of the script.
        head -n 1 "$DEST.download" | grep -q '^#!/bin/bash' || { rm -f "$DEST.download"; die "downloaded file is not the expected script"; }
        mv "$DEST.download" "$DEST"
    fi
    chmod +x "$DEST"
}

# status_line_of <settings file>: its statusLine command, or nothing.
status_line_of() {
    [ -f "$1" ] || return 0
    jq -r 'objects | .statusLine.command // empty' "$1" 2>/dev/null || true
}

# update_settings <jq filter> [jq args...]: rewrite $SETTINGS, keeping a backup,
# the file's mode, and (for dotfile setups) a symlink if it is one.
update_settings() {
    local filter=$1; shift
    local tmp="$SETTINGS.cache-warm.$$"
    cp -p "$SETTINGS" "$tmp"
    if ! jq "$@" "$filter" "$SETTINGS" >"$tmp"; then
        rm -f "$tmp"; die "could not update $SETTINGS (left unchanged)"
    fi
    cp -p "$SETTINGS" "$SETTINGS.bak-cache-warm"
    if [ -L "$SETTINGS" ]; then cat "$tmp" >"$SETTINGS"; rm -f "$tmp"; else mv "$tmp" "$SETTINGS"; fi
    echo "Updated $SETTINGS (backup: $SETTINGS.bak-cache-warm)"
}

# print_manual_steps <existing command> <file it is defined in>
print_manual_steps() {
    cat <<EOF

A status line is already configured in $2:
    $1
It was left untouched. To add the cache segment, have that script pass the JSON
it receives on stdin to cache_warm.sh and append the result. In bash:

    input=\$(cat)     # most status line scripts already do this
    cache=\$(printf '%s' "\$input" | bash "$DEST" 2>/dev/null)
    echo "your existing output\${cache:+ · \$cache}"

If that status line is a command you don't own (npx something), wrap it: see
"Wrapping another status line" in the README.

Then let the countdown tick while the session is idle by adding
"refreshInterval": $REFRESH to that statusLine object.
EOF
}

install() {
    need_jq
    mkdir -p "$CLAUDE_DIR"
    fetch_script
    printf '{}' | bash "$DEST" >/dev/null || die "$DEST failed its self-test"
    echo "Installed $DEST"

    # A project-level statusLine replaces the one from any broader scope, so a
    # custom status line there counts as existing too.
    local f existing
    if [ "$scope" = project ]; then
        for f in "$CLAUDE_DIR/settings.json" "$GLOBAL_DIR/settings.json"; do
            existing=$(status_line_of "$f")
            if [ -n "$existing" ] && [ "$existing" != "bash \"$GLOBAL_DIR/cache_warm.sh\"" ]; then
                print_manual_steps "$existing" "$f"; return
            fi
        done
    fi

    if [ ! -e "$SETTINGS" ]; then
        jq -n --arg cmd "$CMD" --argjson r "$REFRESH" \
            '{statusLine: {type: "command", command: $cmd, refreshInterval: $r}}' >"$SETTINGS"
        echo "Created $SETTINGS with the cache status line."
    elif ! jq -e 'type == "object"' "$SETTINGS" >/dev/null 2>&1; then
        echo "warning: $SETTINGS is not valid JSON; left unchanged." >&2
        echo "  Fix it, then re-run this installer to configure the status line." >&2
        return
    else
        existing=$(status_line_of "$SETTINGS")
        if [ -z "$existing" ]; then
            update_settings '.statusLine = {type: "command", command: $cmd, refreshInterval: $r}' \
                --arg cmd "$CMD" --argjson r "$REFRESH"
        elif [ "$existing" = "$CMD" ]; then
            if jq -e '.statusLine.refreshInterval == null' "$SETTINGS" >/dev/null; then
                update_settings '.statusLine.refreshInterval = $r' --argjson r "$REFRESH"
            else
                echo "Status line already configured."
            fi
        else
            print_manual_steps "$existing" "$SETTINGS"; return
        fi
    fi
    if [ "$scope" = project ]; then
        echo "Done. Applies to Claude Code sessions started in $project_dir."
        echo "settings.local.json is personal; add .claude/cache_warm.sh to .gitignore if you don't want it committed."
    else
        echo "Done. The segment appears after your next message in Claude Code."
    fi
}

uninstall() {
    rm -f "$DEST"
    echo "Removed $DEST"
    [ -e "$SETTINGS" ] && command -v jq >/dev/null 2>&1 || return 0
    jq -e 'type == "object"' "$SETTINGS" >/dev/null 2>&1 || return 0
    local existing
    existing=$(status_line_of "$SETTINGS")
    if [ "$existing" = "$CMD" ]; then
        update_settings 'del(.statusLine)'
    elif [ -n "$existing" ]; then
        echo "Your status line ($existing) was left untouched;"
        echo "remove the cache_warm.sh lines from it if you added them."
    fi
}

"$action"
