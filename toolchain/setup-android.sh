#!/usr/bin/env bash
# 在 macOS 上把 Android 构建工具链装到项目目录内（不动系统、不需要 brew / Android Studio）。
# 用法：./toolchain/setup-android.sh   然后  ./toolchain/env.sh && cd android && gradle assembleDebug
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWN="$HERE/downloads"
TC="$HERE"
mkdir -p "$DOWN"

JAVA_DIR="$TC/jdk17"
SDK_DIR="$TC/android-sdk"
GRADLE_DIR="$TC/gradle-8.7"

# 1. JDK
if [ ! -x "$JAVA_DIR/Contents/Home/bin/java" ] && [ ! -x "$JAVA_DIR/bin/java" ]; then
  echo ">> 解压 JDK"
  mkdir -p "$JAVA_DIR"
  tar -xzf "$DOWN/jdk17.tar.gz" -C "$JAVA_DIR" --strip-components=1
fi

# 2. Gradle
if [ ! -x "$GRADLE_DIR/bin/gradle" ]; then
  echo ">> 解压 Gradle"
  unzip -q "$DOWN/gradle.zip" -d "$TC"
fi

# 3. Android SDK 命令行工具
if [ ! -x "$SDK_DIR/cmdline-tools/latest/bin/sdkmanager" ]; then
  echo ">> 安装 Android commandline-tools"
  mkdir -p "$SDK_DIR/cmdline-tools"
  unzip -q "$DOWN/cmdline-tools.zip" -d "$TC/tmp-cmdline"
  mv "$TC/tmp-cmdline/cmdline-tools" "$SDK_DIR/cmdline-tools/latest"
  rm -rf "$TC/tmp-cmdline"
fi

export JAVA_HOME="$JAVA_DIR/Contents/Home"
[ -d "$JAVA_HOME" ] || export JAVA_HOME="$JAVA_DIR"
export ANDROID_SDK_ROOT="$SDK_DIR"
export ANDROID_HOME="$SDK_DIR"
# sdkmanager 的缓存目录也放项目内，避免 ~/.android 不可写导致"下载清单失败"
export ANDROID_USER_HOME="$TC/android-user"
mkdir -p "$ANDROID_USER_HOME"
export PATH="$JAVA_HOME/bin:$GRADLE_DIR/bin:$SDK_DIR/cmdline-tools/latest/bin:$PATH"

# 4. 安装平台与构建工具
if [ ! -d "$SDK_DIR/platforms/android-34" ]; then
  echo ">> 安装 platforms;android-34 / build-tools;34.0.0"
  yes | sdkmanager --licenses > /dev/null || true
  sdkmanager "platform-tools" "platforms;android-34" "build-tools;34.0.0" || {
    echo ">> sdkmanager 失败，改用 curl 直接下载官方 zip"
    "$HERE/fetch-sdk-manual.sh"
  }
fi

echo
echo "OK. 用法："
echo "  source $HERE/env.sh"
echo "  cd $HERE/../android && gradle assembleDebug --no-daemon"
