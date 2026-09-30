#!/usr/bin/env bash
# Seeds default shell configuration dotfiles into user home directories while preserving existing configurations.

set -euo pipefail

zshrc_source="${1:-/etc/workspace/config/workspace.zshrc}"
zsh_plugins_source="${2:-/etc/workspace/config/workspace.zsh_plugins.txt}"
zellij_config_source="${3:-/etc/workspace/config/workspace-zellij.kdl}"
herdr_config_source="${4:-/etc/workspace/config/workspace-herdr.toml}"
snazzy_theme_source="${5:-/etc/workspace/config/workspace-snazzy.zsh}"

mkdir -p "${HOME}"
if [[ ! -e "${HOME}/.zshrc" && ! -L "${HOME}/.zshrc" ]]; then
  cp -- "${zshrc_source}" "${HOME}/.zshrc"
  chmod 0644 "${HOME}/.zshrc"
fi
if [[ ! -e "${HOME}/.zsh_plugins.txt" && ! -L "${HOME}/.zsh_plugins.txt" ]]; then
  cp -- "${zsh_plugins_source}" "${HOME}/.zsh_plugins.txt"
  chmod 0644 "${HOME}/.zsh_plugins.txt"
fi
if [[ -f ${zellij_config_source} && ! -e "${HOME}/.config/zellij/config.kdl" && ! -L "${HOME}/.config/zellij/config.kdl" ]]; then
  mkdir -p "${HOME}/.config/zellij"
  cp -- "${zellij_config_source}" "${HOME}/.config/zellij/config.kdl"
  chmod 0644 "${HOME}/.config/zellij/config.kdl"
fi
if [[ -f ${herdr_config_source} && ! -e "${HOME}/.config/herdr/config.toml" && ! -L "${HOME}/.config/herdr/config.toml" ]]; then
  mkdir -p "${HOME}/.config/herdr"
  cp -- "${herdr_config_source}" "${HOME}/.config/herdr/config.toml"
  chmod 0644 "${HOME}/.config/herdr/config.toml"
fi
if [[ -f ${snazzy_theme_source} && ! -e "${HOME}/.config/zsh/themes/snazzy.zsh" && ! -L "${HOME}/.config/zsh/themes/snazzy.zsh" ]]; then
  mkdir -p "${HOME}/.config/zsh/themes"
  cp -- "${snazzy_theme_source}" "${HOME}/.config/zsh/themes/snazzy.zsh"
  chmod 0644 "${HOME}/.config/zsh/themes/snazzy.zsh"
fi
