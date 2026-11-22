#!/usr/bin/env bash
# 构建 SameCast 电视端 APK。
#
# 两种方式，自动选择：
#   1) 本机已装 JDK 17 + Android SDK（ANDROID_SDK_ROOT / ANDROID_HOME / local.properties）→ 直接用 gradlew
#   2) 本机有 docker → 用 Dockerfile.build 里的构建镜像（不用装 JDK/SDK）
set -euo pipefail
cd "$(dirname "$0")"

have_sdk() {
  [ -n "${ANDROID_SDK_ROOT:-}" ] || [ -n "${ANDROID_HOME:-}" ] || [ -f local.properties ]
}

if command -v java >/dev/null 2>&1 && have_sdk; then
  echo "==> 本机构建：./gradlew assembleDebug"
  ./gradlew assembleDebug --no-daemon
elif command -v docker >/dev/null 2>&1; then
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
else
  cat >&2 <<'EOF'
找不到可用的构建环境。

请任选一种：
  A. 装 JDK 17 + Android SDK 34，设置 ANDROID_SDK_ROOT（或写 local.properties 里的 sdk.dir），再跑本脚本；
  B. 装 Docker，本脚本会自动用 Dockerfile.build 构建。

详见 android/README.md。
EOF
  exit 1
fi

echo
echo "APK: $PWD/app/build/outputs/apk/debug/SameCast-*.apk"
