#!/usr/bin/env bash
# Shared bounded input and lookup helpers for Claude Code hook launchers.

mnemos_read_json_payload() {
  MNEMOS_HOOK_PAYLOAD=""
  MNEMOS_HOOK_ROUTE_STATUS=""

  local mode="${1:-read}"
  local helper_dir="${BASH_SOURCE[0]%/*}"
  local reader_output=""
  if command -v perl >/dev/null 2>&1; then
    if [[ "${mode}" == "route" ]]; then
      reader_output=$(perl "${helper_dir}/hook_input_reader.pl" --route 2>/dev/null) || return 1
      [[ "${reader_output}" == [012]$'\n'* ]] || return 1
      MNEMOS_HOOK_ROUTE_STATUS="${reader_output%%$'\n'*}"
      MNEMOS_HOOK_PAYLOAD="${reader_output#*$'\n'}"
    else
      MNEMOS_HOOK_PAYLOAD=$(perl "${helper_dir}/hook_input_reader.pl" 2>/dev/null) || return 1
    fi
  elif command -v python3 >/dev/null 2>&1; then
    MNEMOS_HOOK_PAYLOAD=$(python3 -S "${helper_dir}/hook_input.py" 2>/dev/null) || return 1
  else
    return 1
  fi

  [[ -n "${MNEMOS_HOOK_PAYLOAD}" ]]
}

mnemos_sha256_available() {
  command -v shasum >/dev/null 2>&1 \
    || command -v sha256sum >/dev/null 2>&1 \
    || command -v openssl >/dev/null 2>&1
}

mnemos_sha256_stream() {
  local output=""

  if command -v shasum >/dev/null 2>&1; then
    output=$(shasum -a 256 2>/dev/null) || return 1
    printf '%s' "${output%% *}"
    return 0
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    output=$(sha256sum 2>/dev/null) || return 1
    printf '%s' "${output%% *}"
    return 0
  fi
  if command -v openssl >/dev/null 2>&1; then
    output=$(openssl dgst -sha256 2>/dev/null) || return 1
    printf '%s' "${output##* }"
    return 0
  fi

  return 1
}

mnemos_json_cache_key() {
  local payload=$1
  local repo_root=$2
  local key_type=$3
  local jq_filter=""

  command -v jq >/dev/null 2>&1 || return 1
  mnemos_sha256_available || return 1

  if [[ "${key_type}" == "exact" ]]; then
    jq_filter='if ((.session_id? | type) == "string" and (.prompt? | type) == "string") then $repo, "\u0000", .session_id, "\u0000", .prompt else empty end'
  elif [[ "${key_type}" == "session" ]]; then
    jq_filter='if ((.session_id? | type) == "string") then $repo, "\u0000", .session_id else empty end'
  else
    return 1
  fi

  printf '%s' "${payload}" \
    | jq -e -j --arg repo "${repo_root}" "${jq_filter}" 2>/dev/null \
    | mnemos_sha256_stream
}

mnemos_file_mtime() {
  local path=$1

  stat -f '%m' "${path}" 2>/dev/null && return 0
  stat -c '%Y' "${path}" 2>/dev/null && return 0
  return 1
}

mnemos_cache_file_fresh() {
  local path=$1
  local raw_ttl="${MNEMOS_CONTEXT_CACHE_TTL_SECONDS:-300}"
  local ttl_seconds=""
  local modified_at=""
  local now=""

  [[ -f "${path}" ]] || return 1
  if [[ ! "${raw_ttl}" =~ ^-?([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]]; then
    return 0
  fi
  ttl_seconds="${raw_ttl%%.*}"
  if [[ -z "${ttl_seconds}" || "${ttl_seconds}" == "-" || "${ttl_seconds}" == "-0" ]]; then
    ttl_seconds=0
  fi
  if [[ "${ttl_seconds}" -lt 0 ]]; then
    return 0
  fi

  modified_at=$(mnemos_file_mtime "${path}") || return 0
  now=$(date +%s 2>/dev/null) || return 0
  [[ "${modified_at}" =~ ^[0-9]+$ && "${now}" =~ ^[0-9]+$ ]] || return 0
  [[ $((now - modified_at)) -le "${ttl_seconds}" ]]
}

mnemos_any_fresh_default_cache() {
  local cache_dir=$1
  local cache_path=""

  for cache_path in "${cache_dir}/exact/"*.txt "${cache_dir}/session/"*.txt; do
    if mnemos_cache_file_fresh "${cache_path}"; then
      return 0
    fi
  done

  return 1
}

mnemos_default_cache_hit() {
  local payload=$1
  local repo_root=$2
  local cache_dir=$3
  local cache_path=""
  local exact_key=""
  local session_key=""
  local cache_exists=0

  for cache_path in "${cache_dir}/exact/"*.txt "${cache_dir}/session/"*.txt; do
    if [[ -f "${cache_path}" ]]; then
      cache_exists=1
      break
    fi
  done
  if [[ "${cache_exists}" == "0" ]]; then
    return 1
  fi

  if [[ -z "${MNEMOS_CONTEXT_CACHE_FILE:-}" ]]; then
    exact_key=$(mnemos_json_cache_key "${payload}" "${repo_root}" exact) || {
      mnemos_any_fresh_default_cache "${cache_dir}" && return 2
      return 1
    }
    if [[ -n "${exact_key}" ]] \
      && mnemos_cache_file_fresh "${cache_dir}/exact/${exact_key}.txt"; then
      return 0
    fi
  fi

  session_key=$(mnemos_json_cache_key "${payload}" "${repo_root}" session) || {
    mnemos_any_fresh_default_cache "${cache_dir}" && return 2
    return 1
  }
  if [[ -n "${session_key}" ]] \
    && mnemos_cache_file_fresh "${cache_dir}/session/${session_key}.txt"; then
    return 0
  fi

  return 1
}

mnemos_file_inode_size() {
  local path=$1

  stat -f '%i %z' "${path}" 2>/dev/null && return 0
  stat -c '%i %s' "${path}" 2>/dev/null && return 0
  return 1
}

mnemos_promotion_output_possible() {
  local repo_root=$1
  local obs_log="${repo_root}/.agent/observability.jsonl"
  local cursor_path="${MNEMOS_PROMO_CURSOR:-${HOME:-}/.mnemos/.cache/promotion-cursor.txt}"
  local file_stat=""
  local cursor_stat=""
  local file_inode=""
  local file_size=""
  local cursor_inode=""
  local cursor_offset=""

  [[ -f "${obs_log}" ]] || return 1
  [[ -f "${cursor_path}" ]] || return 0
  command -v jq >/dev/null 2>&1 || return 0

  file_stat=$(mnemos_file_inode_size "${obs_log}") || return 0
  cursor_stat=$(jq -e -r 'if ((.inode | type) == "number" and (.offset | type) == "number") then [.inode, .offset] | @tsv else empty end' "${cursor_path}" 2>/dev/null) || return 0
  read -r file_inode file_size <<< "${file_stat}"
  read -r cursor_inode cursor_offset <<< "${cursor_stat}"

  [[ "${file_inode}" =~ ^[0-9]+$ && "${file_size}" =~ ^[0-9]+$ ]] || return 0
  [[ "${cursor_inode}" =~ ^[0-9]+$ && "${cursor_offset}" =~ ^[0-9]+$ ]] || return 0
  if [[ "${file_inode}" == "${cursor_inode}" && "${file_size}" == "${cursor_offset}" ]]; then
    return 1
  fi

  return 0
}
