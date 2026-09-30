# Seeds default interactive Zsh prompt themes, tool paths, command history, and auto-completion for workspaces.

autoload -Uz colors promptinit
colors
setopt PROMPT_SUBST


# Command history configuration
HISTFILE="${XDG_STATE_HOME:-$HOME/.local/state}/zsh/history"
[[ -d "${HISTFILE:h}" ]] || mkdir -p "${HISTFILE:h}" 2>/dev/null || true
HISTSIZE=50000
SAVEHIST=50000
setopt EXTENDED_HISTORY
setopt SHARE_HISTORY
setopt HIST_EXPIRE_DUPS_FIRST
setopt HIST_IGNORE_DUPS
setopt HIST_IGNORE_SPACE
setopt HIST_VERIFY

# Ephemeral build and toolchain caches
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/tmp/cache}"
export BAZEL_OUTPUT_ROOT="${BAZEL_OUTPUT_ROOT:-/tmp/bazel}"
export GOCACHE="${GOCACHE:-/tmp/cache/go-build}"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-/tmp/cargo-target}"


antidote_root="${XDG_DATA_HOME:-$HOME/.local/share}/workspace/antidote/current"
if [[ -r "$antidote_root/antidote.zsh" ]]; then
  source "$antidote_root/antidote.zsh"
  antidote load "${ZDOTDIR:-$HOME}/.zsh_plugins.txt" >/dev/null 2>&1
fi

if (( $+commands[fzf] )); then
  source <(fzf --zsh)
fi

# Load Snazzy theme
typeset -gA snazzy=()
[[ -r ~/.config/zsh/themes/snazzy.zsh ]] && source ~/.config/zsh/themes/snazzy.zsh

# Thin vertical beam cursor for interactive terminals
if [[ -t 1 ]]; then
  _set_beam_cursor() {
    printf '\e[5 q'
  }
  autoload -Uz add-zsh-hook
  add-zsh-hook precmd _set_beam_cursor
  _set_beam_cursor
fi

# Pure prompt setup matching local terminal
PURE_CMD_MAX_EXEC_TIME=1
zstyle ':prompt:pure:host' show no
promptinit
prompt pure
prompt_pure_precmd
psvar[13]=
prompt_pure_state[username]=""
parts=("${(@s/${prompt_newline}/)PROMPT}")
PROMPT="${parts[1]}${prompt_newline}%F{${snazzy[fg]:-white}}%* ${parts[2]}"

alias reboot='reboot'

# Modern file navigation and S3 protection defaults
if (( $+commands[eza] )); then
  alias ls='eza --group-directories-first --color=auto'
  alias ll='eza -lh --group-directories-first --color=auto --git'
  alias la='eza -lah --group-directories-first --color=auto --git'
  alias tree='eza --tree'
fi
if (( $+commands[fd] )); then
  alias find='fd'
fi
if (( $+commands[rg] )); then
  alias grep='rg'
fi

# Private user runtime directory for temporary files and cross-session messaging sockets (e.g. Claude Code)
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-${HOME}/.runtime}"
[[ -d "${XDG_RUNTIME_DIR}" ]] || mkdir -p -m 0700 "${XDG_RUNTIME_DIR}" 2>/dev/null || true
export CLAUDE_CODE_TMPDIR="${XDG_RUNTIME_DIR}"

# Codex sandbox binary shim configuration
if ! (( $+commands[bwrap] )); then
  _bwrap_bundled="$(find "${XDG_DATA_HOME:-$HOME/.local/share}/mise/installs/codex" -name bwrap -type f -perm -111 2>/dev/null | head -n 1)"
  if [[ -n "$_bwrap_bundled" ]]; then
    mkdir -p "${XDG_DATA_HOME:-$HOME/.local/share}/mise/shims" 2>/dev/null || true
    ln -sf "$_bwrap_bundled" "${XDG_DATA_HOME:-$HOME/.local/share}/mise/shims/bwrap" 2>/dev/null || true
  fi
  unset _bwrap_bundled
fi
