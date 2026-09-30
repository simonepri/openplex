# Defines the Snazzy 16-color ANSI palette for interactive Zsh terminal prompt formatting in dev workspaces.

typeset -gA snazzy=(
  bg      '#282a36'
  fg      '#eff0eb'
  red     '#ff5c57'
  green   '#5af78e'
  yellow  '#f3f99d'
  blue    '#57c7ff'
  magenta '#ff6ac1'
  cyan    '#9aedfe'
  white   '#f1f1f0'
  grey    '#686868'
)

# Pure prompt styles
zstyle ':prompt:pure:path' color "$snazzy[blue]"
zstyle ':prompt:pure:prompt:success' color "$snazzy[magenta]"
zstyle ':prompt:pure:prompt:error' color "$snazzy[red]"
zstyle ':prompt:pure:git:arrow' color "$snazzy[cyan]"
zstyle ':prompt:pure:git:stash' color "$snazzy[cyan]"
zstyle ':prompt:pure:git:branch' color "$snazzy[grey]"
zstyle ':prompt:pure:git:dirty' color "$snazzy[magenta]"
zstyle ':prompt:pure:execution_time' color "$snazzy[yellow]"

# Configure terminal emulator 16-color ANSI palette
if [[ -t 1 ]]; then
  printf '\033]11;#282a36\007\033]10;#eff0eb\007'
  printf '\033]4;0;#282a36\007\033]4;1;#ff5c57\007\033]4;2;#5af78e\007\033]4;3;#f3f99d\007\033]4;4;#57c7ff\007\033]4;5;#ff6ac1\007\033]4;6;#9aedfe\007\033]4;7;#f1f1f0\007'
  printf '\033]4;8;#686868\007\033]4;9;#ff5c57\007\033]4;10;#5af78e\007\033]4;11;#f3f99d\007\033]4;12;#57c7ff\007\033]4;13;#ff6ac1\007\033]4;14;#9aedfe\007\033]4;15;#eff0eb\007'
fi
