#!/usr/bin/env bash
# Read a .env file into the environment, literally.
#
# Usage:  load_dotenv "$ROOT/.env"
#
# Sourced by scripts/run_local.sh. This exists instead of `. .env` because a
# Snowflake password is arbitrary text, and sourcing runs it as shell:
# `Sn0w$Flake!2026` would arrive as `Sn0w!2026`, and a value containing a
# space would abort the caller under `set -e`.
#
# The grammar matches scripts/dotenv_file.py exactly, and
# tests/test_dotenv_file.py runs both over the same fixtures to keep them in
# step. Values already set in the environment win, as with any dotenv.
load_dotenv() {
    local file="$1" line key value
    [ -f "$file" ] || return 0

    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"   # strip leading whitespace
        line="${line%$'\r'}"                      # tolerate CRLF
        case "$line" in
            ''|'#'*) continue ;;
            'export '*) line="${line#export }"; line="${line#"${line%%[![:space:]]*}"}" ;;
        esac

        key="${line%%=*}"
        [ "$key" = "$line" ] && continue          # no '=' on the line
        value="${line#*=}"

        case "$key" in
            ''|[!A-Za-z_]*|*[!A-Za-z0-9_]*) continue ;;
        esac

        # Strip one matching pair of surrounding quotes, nothing more.
        case "$value" in
            \"*\") value="${value#\"}"; value="${value%\"}" ;;
            \'*\') value="${value#\'}"; value="${value%\'}" ;;
        esac

        # No eval and no expansion: the value reaches the environment verbatim.
        [ -n "${!key+set}" ] || export "$key=$value"
    done < "$file"
}
