#!/usr/bin/env bash
# source 这个文件，给当前 shell 配上项目内的 JDK / Android SDK / Gradle
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export JAVA_HOME="$HERE/jdk17/Contents/Home"
[ -d "$JAVA_HOME" ] || export JAVA_HOME="$HERE/jdk17"
export ANDROID_SDK_ROOT="$HERE/android-sdk"
# sdkmanager 默认往 ~/.android 写缓存；本项目把用户目录也放进项目内，避免权限问题
export ANDROID_USER_HOME="$HERE/android-user"
[ -d "$ANDROID_USER_HOME" ] || mkdir -p "$ANDROID_USER_HOME"
export ANDROID_HOME="$HERE/android-sdk"
# Gradle 的用户目录默认在 ~/.gradle，沙箱内不可写，也放到项目里
export GRADLE_USER_HOME="$HERE/gradle-home"
[ -d "$GRADLE_USER_HOME" ] || mkdir -p "$GRADLE_USER_HOME"
# 本机 Gradle 的原生服务库加载不了（未签名 dylib 被 macOS 拦），关掉原生服务
export GRADLE_OPTS="${GRADLE_OPTS:-} -Dorg.gradle.native=false"
export PATH="$JAVA_HOME/bin:$HERE/gradle-8.7/bin:$HERE/android-sdk/cmdline-tools/latest/bin:$HERE/android-sdk/platform-tools:$PATH"
echo "JAVA_HOME=$JAVA_HOME"
java -version 2>&1 | head -1
gradle --version 2>/dev/null | grep -E '^Gradle' || true
