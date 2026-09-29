#!/usr/bin/env zsh

workspace() {
  eval "$($(workspace_data_dir)/workspace.py $@)"
}

alias ws=workspace

reload_completion() {
  local f
  f=("$(workspace_data_dir)/completion/_workspace")
  unfunction $f:t 2> /dev/null
  autoload -U $f:t
}

alias ws='workspace '
alias wscd='workspace cd'
alias wss='workspace source '
