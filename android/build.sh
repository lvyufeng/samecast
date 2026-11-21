#!/usr/bin/env bash
# 在没有 Android Studio 的机器上构建 APK（只需要 docker）。
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=samecast-android-builder

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "首次构建会下载 Android SDK / Gradle（约 1GB），耐心等几分钟…"
  docker build -f Dockerfile.build -t "$IMAGE" .
fi

mkdir -p .gradle-home
docker run --rm \
  -v "$PWD":/project \
  -v "$PWD/.gradle-home":/gradle-home \
  -e GRADLE_USER_HOME=/gradle-home \
  -e HOME=/gradle-home \
  -u "$(id -u):$(id -g)" \
  -w /project \
  "$IMAGE" gradle assembleDebug --no-daemon

echo
echo "APK: $PWD/app/build/outputs/apk/debug/SameCast-*.apk"
