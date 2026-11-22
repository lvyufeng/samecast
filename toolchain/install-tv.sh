#!/usr/bin/env bash
# 一键把接收端 App 装到电视上：全程 adb 静默安装，不需要遥控器确认。
#
# 用法:
#   ./toolchain/install-tv.sh                 # 装到 $SAMECAST_DEVICES 里的电视
#   ./toolchain/install-tv.sh <电视A IP>:5555 <电视B IP>:5555
#   ./toolchain/install-tv.sh --no-start      # 只装不启动
#
# 电视地址也可以放在环境变量里:
#   export SAMECAST_DEVICES="<电视A IP>:5555 <电视B IP>:5555"
#
# 说明:
#   * 走 PackageInstaller 的 session 接口(install-create/write/commit)，
#     绕过部分国产电视把 pm install 劫持到“安装确认”界面的问题。
#   * 创维(酷开) 会弹 com.coocaa.os.service 的“正在通过adb安装此应用”确认框，
#     超时后自动拒绝。脚本在后台跑 commit 的同时，用遥控器键
#     （右移到「继续安装」再确定）自动确认。
#   * 其他机型若仍弹 packageinstaller 的确认页，用 uiautomator 点击“下一步/安装/确定”。
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADB="$ROOT/toolchain/android-sdk/platform-tools/adb"
APK_DIR="$ROOT/android/app/build/outputs/apk/debug"
# 新版产物叫 SameCast-<版本>-debug.apk，旧版是 app-debug.apk，都认
APK="$(ls -1 "$APK_DIR"/SameCast-*.apk "$APK_DIR"/app-debug.apk 2>/dev/null | head -1)"
PKG="com.samecast.receiver"
ACTIVITY="$PKG/.MainActivity"
REMOTE="/data/local/tmp/samecast.apk"
START=1
DEVICES=()

for arg in "$@"; do
  case "$arg" in
    --no-start) START=0 ;;
    -*) echo "未知参数: $arg" >&2; exit 2 ;;
    *) DEVICES+=("$arg") ;;
  esac
done
if [ ${#DEVICES[@]} -eq 0 ] && [ -n "${SAMECAST_DEVICES:-}" ]; then
  read -r -a DEVICES <<< "$SAMECAST_DEVICES"
fi
if [ ${#DEVICES[@]} -eq 0 ]; then
  echo "没有指定电视地址：./toolchain/install-tv.sh <电视IP:5555>... 或 export SAMECAST_DEVICES=..." >&2
  exit 2
fi
if [ -z "$APK" ] || [ ! -f "$APK" ]; then
  echo "找不到 APK（先构建: cd android && gradle assembleDebug）" >&2
  exit 1
fi

SIZE=$(wc -c < "$APK" | tr -d ' ')
echo "APK: $APK (${SIZE} 字节)"

sh_dev() { "$ADB" -s "$1" shell "$2" 2>&1 | tr -d '\r'; }

# 安装确认弹窗自动确认：创维走遥控按键，其他机型走 uiautomator 点击。
auto_confirm() {
  local dev="$1" tries="${2:-12}" i dump line bounds focused
  for i in $(seq 1 "$tries"); do
    sleep 1
    sh_dev "$dev" "uiautomator dump /sdcard/_inst.xml" >/dev/null 2>&1
    dump=$(sh_dev "$dev" "cat /sdcard/_inst.xml")
    case "$dump" in
      *继续安装*)
        # 创维/酷开：默认焦点在“拒绝”，右移到“继续安装”再按确定
        if printf '%s' "$dump" | sed 's/></>\n</g' | grep -q 'text="继续安装"[^>]*focused="true"'; then
          sh_dev "$dev" "input keyevent 23" >/dev/null
        else
          sh_dev "$dev" "input keyevent 22" >/dev/null
          sleep 1
          sh_dev "$dev" "input keyevent 23" >/dev/null
        fi
        echo "    已自动确认创维安装弹窗"
        return 0
        ;;
    esac
    line=$(printf '%s' "$dump" | sed 's/></>\n</g' | grep -E 'text="(下一步|安装|确定|仍然安装|完成|打开)"' | head -1)
    if [ -n "$line" ]; then
      bounds=$(printf '%s' "$line" | sed -n 's/.*bounds="\[\([0-9]*\),\?\([0-9]*\)\]\[\([0-9]*\),\?\([0-9]*\)\]".*/\1 \2 \3 \4/p')
      if [ -n "$bounds" ]; then
        set -- $bounds
        sh_dev "$dev" "input tap $(( ($1 + $3) / 2 )) $(( ($2 + $4) / 2 ))" >/dev/null
        echo "    已自动点击安装确认"
        return 0
      fi
    fi
  done
  return 0
}

# 后台跑一条会阻塞的安装命令，同时自动确认弹窗。
blocking_install() {
  local dev="$1" cmd="$2" log pid out
  log=$(mktemp)
  ( "$ADB" -s "$dev" shell "$cmd" > "$log" 2>&1 ) &
  pid=$!
  auto_confirm "$dev"
  wait $pid 2>/dev/null
  out=$(tr -d '\r' < "$log")
  rm -f "$log"
  printf '%s' "$out"
}

install_session() {
  local dev="$1" out sid
  sh_dev "$dev" "rm -f $REMOTE" >/dev/null
  "$ADB" -s "$dev" push "$APK" "$REMOTE" >/dev/null 2>&1 || { echo "  push 失败"; return 1; }
  out=$(sh_dev "$dev" "pm install-create -r -d")
  sid=$(printf '%s' "$out" | sed -n 's/.*\[\([0-9][0-9]*\)\].*/\1/p')
  if [ -z "$sid" ]; then
    echo "  install-create 失败: $out"
    return 1
  fi
  out=$(sh_dev "$dev" "pm install-write -S $SIZE $sid base $REMOTE")
  case "$out" in
    *Success*) ;;
    *) echo "  install-write 失败: $out"; sh_dev "$dev" "pm install-abandon $sid" >/dev/null; return 1 ;;
  esac
  out=$(blocking_install "$dev" "pm install-commit $sid")
  case "$out" in
    *Success*) echo "  静默安装成功(session $sid)"; return 0 ;;
    *) echo "  install-commit 结果: $out"; return 1 ;;
  esac
}

install_plain() {
  local dev="$1" out
  out=$(blocking_install "$dev" "pm install -r -d $REMOTE")
  case "$out" in
    *Success*) echo "  安装成功"; return 0 ;;
    *USER_RESTRICTED*)
      echo "  创维限制安装，打开 adb 安装开关后重试"
      sh_dev "$dev" "setprop persist.sys.openadb 1" >/dev/null
      sleep 2
      out=$(blocking_install "$dev" "pm install -r -d $REMOTE")
      case "$out" in *Success*) echo "  安装成功"; return 0 ;; esac
      ;;
  esac
  echo "  安装失败: $out"
  return 1
}

RC=0
for DEV in "${DEVICES[@]}"; do
  echo "==== $DEV"
  "$ADB" connect "$DEV" >/dev/null 2>&1
  if ! "$ADB" -s "$DEV" get-state >/dev/null 2>&1; then
    echo "  连不上，跳过"; RC=1; continue
  fi
  sh_dev "$DEV" "am force-stop $PKG" >/dev/null
  if ! install_session "$DEV"; then
    echo "  session 方式失败，改用普通安装 + 自动确认"
    install_plain "$DEV" || { auto_confirm "$DEV"; }
  fi
  if [ "$START" = "1" ]; then
    sh_dev "$DEV" "am start -n $ACTIVITY" | sed 's/^/  /'
  fi
done

echo "完成。"
exit $RC
