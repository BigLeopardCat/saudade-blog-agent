#!/usr/bin/env bash
# 浏览器截图需要中文字体。固定 Noto 官方源和摘要，避免 apt 镜像的 61MB 字体包长时间卡住。
# Noto CJK 字体按 SIL Open Font License 分发；这里只安装到一次性 CI 用户目录，不入仓。
set -euo pipefail
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
font_name=NotoSansCJKsc-Regular.otf
font_path="$work_dir/$font_name"
curl --fail --silent --show-error --location --retry 3 --retry-max-time 180 \
  --connect-timeout 15 --max-time 90 \
  https://raw.githubusercontent.com/notofonts/noto-cjk/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf \
  -o "$font_path"
printf '%s  %s\n' '2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b' "$font_path" | sha256sum --check
font_dir="${HOME}/.local/share/fonts/saudade-ci"
mkdir -p "$font_dir"
install -m 644 "$font_path" "$font_dir/$font_name"
fc-cache -f "$font_dir"
fc-match 'Noto Sans CJK SC'
