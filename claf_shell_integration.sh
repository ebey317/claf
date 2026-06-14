# CLAF shell integration — source this file in ~/.bashrc (or ~/.zshrc).
#
# Provides:
#   - claf-mode <args>   : get/set/cycle CLAF permission mode and export it
#                          to the current shell.
#   - Shift+Tab binding  : cycles mode just like Claude Code's Shift+Tab.
#
# Add to ~/.bashrc:
#   source "$HOME/projects/claf/claf_shell_integration.sh"

CLAF_MODE_TOOL="$HOME/projects/claf/toolbox/claf_mode.py"

claf-mode() {
    if [ ! -f "$CLAF_MODE_TOOL" ]; then
        echo "[claf-mode] not found: $CLAF_MODE_TOOL" >&2
        return 1
    fi
    python3 "$CLAF_MODE_TOOL" "$@"
    # Sync the current shell's env var with the persisted mode.
    eval "$(python3 "$CLAF_MODE_TOOL" --export)"
}

# Shift+Tab sends escape sequence "\e[Z" in most terminals.
# Bind it to cycle CLAF permission mode.
if [ -n "$BASH_VERSION" ]; then
    bind '"\e[Z": "claf-mode --cycle\n"' 2>/dev/null || true
fi
