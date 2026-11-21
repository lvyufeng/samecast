#!/usr/bin/env bash
# sdkmanager 失败时的兜底：直接用官方 zip 装 platform-tools / build-tools 34 / platforms 34。
# 2024 年后的包名（macOS 归档）：
#   platform-tools            platform-tools_r37.0.3-darwin.zip
#   build-tools;34.0.0        build-tools_r34-macosx.zip   （zip 内顶层目录叫 android-14）
#   platforms;android-34      platform-34-ext7_r03.zip     （zip 内顶层目录叫 android-34）
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="$HERE/android-sdk"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fetch() {  # fetch <url> <outdir>
  echo ">> 下载 $(basename "$1")"
  curl -sSL --fail -m 900 -o "$TMP/$(basename "$1")" "$1"
  unzip -q "$TMP/$(basename "$1")" -d "$2"
}

if [ ! -x "$SDK/platform-tools/adb" ]; then
  fetch https://dl.google.com/android/repository/platform-tools_r37.0.3-darwin.zip "$TMP/pt"
  mkdir -p "$SDK/platform-tools"
  cp -R "$TMP/pt/platform-tools/." "$SDK/platform-tools/"
fi

if [ ! -d "$SDK/build-tools/34.0.0" ]; then
  fetch https://dl.google.com/android/repository/build-tools_r34-macosx.zip "$TMP/bt"
  mkdir -p "$SDK/build-tools"
  cp -R "$TMP/bt/android-14" "$SDK/build-tools/34.0.0"
fi

if [ ! -d "$SDK/platforms/android-34" ]; then
  fetch https://dl.google.com/android/repository/platform-34-ext7_r03.zip "$TMP/pf"
  mkdir -p "$SDK/platforms"
  cp -R "$TMP/pf/android-34" "$SDK/platforms/android-34"
fi

xattr -dr com.apple.quarantine "$SDK" 2>/dev/null || true
chmod +x "$SDK/platform-tools/adb" 2>/dev/null || true
echo ">> 完成：$SDK"
