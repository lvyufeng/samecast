# SameCast 电视端 App（Android）

电视端只装这一个 App。它**不是**一个投屏设备，而是网关的一个"播放端"：
自动找到网关 → 接收播放指令 → 播放并上报进度与暂停状态。

- 手机投屏：系统投屏按钮只看到网关那**一台**虚拟设备（`SameCast`）
- 两台电视：同时播放同一个流，任一台按遥控器「确定」暂停，两边一起暂停
- 无播放器 UI，全屏，开机即用；不依赖 Kodi

## 构建 APK（不需要 Android Studio）

只要有 docker：

```bash
cd android
./build.sh
# 产物：android/app/build/outputs/apk/debug/SameCast-0.3.0-debug.apk
```

首次会拉 build 镜像（约 1GB，含 Android SDK 34 + Gradle 8.7）。之后重编译只用十几秒。
构建需要联网下载 Gradle 依赖（AndroidX Media3）。

## 安装到电视

```bash
adb connect 192.168.1.21:5555
adb install -r app/build/outputs/apk/debug/SameCast-*.apk
```

没有 ADB 就用 U 盘：把 APK 拷到 FAT32 U 盘 → 电视文件管理器/应用调试里安装
（创维 55A3：工程模式 → 应用调试，密码 `69573028`；微鲸 W40F：设置里允许未知来源 → 本地播放里装）。

## 首次运行

App 打开后显示「正在查找网关…」，2–4 秒内会自动通过 SSDP 找到同一局域网里的网关，
然后显示「已连接网关」并进入等待投屏状态。**不需要在电视上输入任何 IP。**

网关侧会自动注册这台电视（`/api/recv/<id>/...` 动态创建），无需改配置重启。

## 工作方式

| 方向 | 接口 |
|---|---|
| 发现网关 | SSDP `M-SEARCH` + `ST: urn:samecast:service:Gateway:1` |
| 收指令 | `GET /api/recv/<id>/poll?since=<seq>` （350ms 轮询） |
| 上报状态 | `POST /api/recv/<id>/state` `{"state":"playing","position":12.3}` |
| 上报遥控操作 | `POST /api/recv/<id>/command` `{"cmd":"pause","position":12.3}` |

和网页接收端 `/tv` 用的是同一套协议，所以可以先在浏览器里验证网关，再上电视。

## 实现要点

- **ExoPlayer (Media3)** 播放 HLS/MP4，硬件解码；不依赖系统播放器
- 遥控器 `DPAD_CENTER` / `MEDIA_PLAY_PAUSE` 映射为全组暂停/播放
- 进度每 800ms 上报一次；网关侧发现偏差 >0.6s 会下发 seek 校正
- 明文 HTTP 已允许（`usesCleartextTraffic`），局域网直连不需要证书
- `minSdk 21`（Android 5.0），覆盖微鲸 W40F 的 Android 5.x

## 可能踩的坑

| 现象 | 处理 |
|---|---|
| 一直「正在查找网关」 | Android 9+ 需要允许 App 发组播；部分电视会限制未知来源 App 的组播，可在网关配置里改成固定地址（见下） |
| 播放失败 `ERROR_CODE_IO_BAD_HTTP_STATUS` | 直链带防盗链（爱奇艺/B站），需要网关侧加 HLS 中继（下一步） |
| 花屏/黑屏但声音正常 | 老芯片硬解 H.264 High profile 4K 吃力，让网关转成 1080p 中继 |
| 装不上 | 酷开系统要先用「应用调试」打开安装权限；微鲸要在设置里允许未知来源 |

## 后备：手动指定网关地址

如果自动发现失败，可以改 `GatewayClient.kt` 里的常量，或在首次启动前用 ADB 设置：

```bash
adb shell am start -n com.samecast.receiver/.MainActivity --es gateway http://192.168.1.10:8090
```
（当前版本还需读取该参数，见 TODO；自动发现正常时不必要）
