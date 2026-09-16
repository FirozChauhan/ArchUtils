# tdm bash completion (generated at build via: python -m shtab --shell=bash src.tdm.cli --prog tdm)
_tdm() {
    local cur="${COMP_WORDS[COMP_CWORD]}"
    local opts="--output --dest --continue --no-resume --max-connections --min-split-size --limit-rate --yes --no-prompt --impersonate --user-agent --referer --cookies --cookies-from-browser --proxy --timeout --retries --retry-delay --checksum --dry-run --no-extract --notify --json --progress --no-progress --config --dump-config --init --verbose --quiet --help --version"
    COMPREPLY=($(compgen -W "$opts" -- "$cur"))
}
complete -F _tdm tdm
