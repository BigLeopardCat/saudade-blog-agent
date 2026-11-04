#!/usr/bin/env bash
# Install the checksum-verified Noto CJK font used by browser geometry tests.
# SIL Open Font License; the font is cached only on ephemeral CI runners.
set -euo pipefail
font_name=NotoSansCJKsc-Regular.otf
font_sha=2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b
font_dir="${HOME}/.local/share/fonts/saudade-ci"
verify() { printf '%s  %s\n' "$font_sha" "$1" | sha256sum --check --status; }
if [[ ! -f "$font_dir/$font_name" ]] || ! verify "$font_dir/$font_name"; then
  work_dir="$(mktemp -d)"
  trap 'rm -rf "$work_dir"' EXIT
  font_path="$work_dir/$font_name"
  object=notofonts/noto-cjk
  revision=f8d157532fbfaeda587e826d4cd5b21a49186f7c
  asset="Sans/OTF/SimplifiedChinese/$font_name"
  sources=("https://cdn.jsdelivr.net/gh/$object@$revision/$asset"
           "https://raw.githubusercontent.com/$object/$revision/$asset")
  valid=0
  for source_url in "${sources[@]}"; do
    if curl --fail --silent --show-error --location --connect-timeout 10 --max-time 60 \
      "$source_url" -o "$font_path" && verify "$font_path"; then
      valid=1
      break
    fi
  done
  [[ "$valid" == 1 ]] || { echo 'Unable to obtain the pinned CJK font' >&2; exit 1; }
  mkdir -p "$font_dir"
  install -m 644 "$font_path" "$font_dir/$font_name"
fi
fc-cache -f "$font_dir"
fc-match 'Noto Sans CJK SC'
