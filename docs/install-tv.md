# 电视端安装指南（微鲸 W40F / 创维 55A3）

两台电视都装自己写的接收端 App（`com.samecast.receiver`），不需要 Kodi。
安装全程通过 adb 完成，**不需要用遥控器点任何确认框**。

## 一、前置：电视开启 adb 调试

| 电视 | 地址 | 系统 | 怎么开 |
| --- | --- | --- | --- |
| 创维 55A3 | 192.168.1.31:5555 | Android 9 | 设置 → 关于 → 连点“版本号”进开发者模式；工程模式菜单 `69573028`；小程序 `55559510` 里打开 adb |
| 微鲸 W40F | 192.168.1.32:5555 | Android 5.1.1 | 设置 → 关于 → 连点版本号 → 开发者选项 → USB/网络调试 |

连上后确认：

```bash
toolchain/android-sdk/platform-tools/adb connect 192.168.1.31:5555
toolchain/android-sdk/platform-tools/adb connect 192.168.1.32:5555
toolchain/android-sdk/platform-tools/adb devices
```

## 二、一键静默安装（推荐）

```bash
export SAMECAST_DEVICES="192.168.1.31:5555 192.168.1.32:5555"
./toolchain/install-tv.sh                   # 装到 SAMECAST_DEVICES 里的所有电视并自动启动
./toolchain/install-tv.sh 192.168.1.32:5555 # 只装某一台
./toolchain/install-tv.sh --no-start        # 只装不启动
```

脚本做的事：

1. 用 `adb push` 把 APK 传到 `/data/local/tmp/samecast.apk`；
2. 走 PackageInstaller 的 **session 接口**安装：
   `pm install-create` → `pm install-write` → `pm install-commit`；
3. 校验结果并 `am start` 启动接收端。

### 为什么必须用 session 接口

部分国产电视把 `adb shell pm install` 劫持到系统的“安装确认”界面
（微鲸上是 `com.android.packageinstaller.PackageInstallerActivity`，
显示“SameCast / 您要安装此应用吗？ / 取消 / 安装”），
不点“安装”就永远卡住。改用 session 接口后 `install-commit` 直接完成安装，
**不会弹出这个界面**，全程无需遥控器。

创维（酷开系统）还会额外弹一个厂商安全确认框：

```
com.coocaa.os.service / AdbInstallActivity
正在通过adb安装此应用，是否继续        [拒绝] [继续安装]
```

这个框默认焦点在“拒绝”上，**约 11 秒后自动拒绝**（系统日志会记
`state=false, op_source=auto_refuse`，安装命令返回 `INSTALL_FAILED_USER_RESTRICTED`）。
注意：`setprop persist.sys.openadb 1` 并不能绕过它，它只影响普通的
`pm install` 劫持路径；session 接口安装同样会被这个框拦下。

所以脚本把 `pm install-commit` 放到后台跑，同时循环 `uiautomator dump`
盯界面：一旦发现“继续安装”按钮，就用遥控器按键确认——先 `input keyevent 22`
（DPAD_RIGHT）把焦点从“拒绝”移到“继续安装”，再 `input keyevent 23`
（DPAD_CENTER）按下。**不能直接用 `input tap` 点坐标**，电视固件会忽略
shell 注入的触摸事件；按键事件才有效。

### 兜底：其他机型弹了 packageinstaller 的框

脚本同样会每 1 秒抓一次界面（`uiautomator dump`），
找到“下一步 / 安装 / 确定 / 继续 / 仍然安装”按钮就点它的中心点，直到安装界面消失。

## 三、安装后确认

```bash
ADB=toolchain/android-sdk/platform-tools/adb
$ADB -s 192.168.1.32:5555 shell dumpsys package com.samecast.receiver | grep versionName
curl -s http://192.168.1.10:8090/api/status     # 应能看到两台电视各有状态
```

电视端 App 启动后会自动发现局域网里的网关（SSDP 搜索
`urn:samecast:service:Gateway:1`），并注册成一个接收端，不用手工填 IP。

## 四、开机自启

APK 已带 `BootReceiver`：电视开机后收到 `BOOT_COMPLETED` 会自动拉起接收端。
（部分深度定制系统会拦截后台自启，若重启后没自启，用上面的 `am start` 命令补一次。）

## 五、手动启动 / 排错

```bash
ADB=toolchain/android-sdk/platform-tools/adb
D=192.168.1.32:5555

$ADB -s $D shell am start -n com.samecast.receiver/.MainActivity   # 手动启动
$ADB -s $D shell am force-stop com.samecast.receiver               # 强制停止
$ADB -s $D logcat -s samecast:V                                   # 看日志
```

常见问题：

- **有回声 / 两个声音同时在放**：电视自带的投屏播放器（微鲸是
  `com.xiaodianshi.tv.yst` 里的 `ProjectionPlayerActivityV2`）还活着，
  和我们的接收端在放同一路流。接收端现在会
  ① 自己管音频焦点（`USAGE_MEDIA` + `handleAudioFocus=false`），每次 load/play
  重新申请一次焦点，行为良好的播放器收到我们会话的焦点请求就会自己暂停；
  ② 每次 load/play 把自己切回前台，让厂商播放器的 Activity 暂停。
  若仍在响，先 `adb shell am force-stop <厂商投屏包名>` 停掉遗留会话。

- **装完没画面**：电视端 App 没启动，用上面的 `am start`。
- **手机搜不到设备**：网关服务没起，`systemctl status samecast`；手机和电视要在同一网段。
- **某一台电视卡顿**：服务端日志 `journalctl -u samecast -f` 里看是否频繁 seek；
  正常应该只有“倍速微调”，很少 seek。
- **一开机就从头播 / 播完自动重来**：不会。播到片尾（`position >= duration - 0.5`）
  时整组正常停止、进度归零，不会循环重载；之后再点播放才从头开始。
- **某一台掉出播放**：服务端会自动重发 load 拉回来，但做了退避（同一台 5 秒内最多一次，
  连续失败 3 次就停止重试等新的播放指令），源真的挂了不会变成无限重载。

## 六、和电视自带的投屏 App 共存（让出画面）

**不需要卸载电视自带的投屏/镜像 App**（乐播、酷开投屏、微鲸投屏等），
它们留着不影响 SameCast。SameCast 的策略是：**只要发现有别的投屏
真的接管了画面/声音，就整体让出，不跟它抢**。

判断依据（三条，任一命中即让出）：

| 情形 | 触发 | 结果 |
| --- | --- | --- |
| 音频焦点被永久抢走 | 系统回调 `AUDIOFOCUS_LOSS`（-1） | 接收端停止播放、清空媒体，上报 `preempted=true` |
| 检测到别的播放器在放 | API 26+ 每 800ms 看一次系统活跃播放配置，媒体/游戏类播放数 ≥2（我们 + 别人）且连续两次采样 | 同上 |
| 我们的界面被切到后台 | `onStop()`（例如用户按 Home、厂商投屏 App 抢到前台） | **只暂停**，整组跟着暂停，不算被接管 |

前两条会让整组 `STOPPED`：网关里记下是哪台被接管
（`journalctl -u samecast` 会看到 `xxx 被其他投屏接管，SameCast 让出画面`），
之后不再自动把这一台拉回播放。第三条只是暂停，用户回到 SameCast 按播放即可继续。

恢复方式：在手机上重新点投屏，或在电视上确认后由网关发 `load/play`；
接收端收到新的播放指令会重新申请音频焦点、切回前台，并清掉 `preempted` 标记。

已知取舍：

- `AUDIOFOCUS_LOSS_TRANSIENT`（-2，比如导航播报、短暂提示音）**不算**被接管，
  只在永久丢失时才让出；
- 微鲸（Android 5.1.1 / API 22）没有活跃播放配置接口，只有第一、第三条生效；
- 有的厂商 DLNA 渲染器会在后台返回 `PLAYING` 却根本不产生音频轨道
  （实测创维的厂商渲染器就是），这种"虚拟播放"不会触发第二条，也不会和我们抢声音；
- 行为良好的播放器（包括创维自带的音乐播放器）收到我们的焦点请求会自己暂停，
  正常不会出现两路同时出声。
