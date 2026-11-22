# 贡献指南

SameCast 是一个很小的项目：一个纯标准库的 Python 服务端 + 一个 Kotlin 电视端 App。
欢迎 issue / PR，尤其是这几种：

- 在你的电视/手机上实测通过的机型（连不上、黑屏、不同步、被别的投屏抢占等）；
- 能稳定复现的 bug，附 `journalctl -u samecast -n 50` 与 `adb logcat -s samecast:V` 的相关片段；
- 中文文档的纠错。

## 开发环境

服务端只要 Python 3.9+（无第三方依赖）：

```bash
cp config.example.json config.json
python3 server/app.py -c config.json
# 另开一个终端跑测试
cd server && python3 -m unittest discover -s tests
```

电视端 App 需要 JDK 17 + Android SDK 34（不需要 Android Studio）：

```bash
source toolchain/env.sh
cd android && ./gradlew assembleDebug
../toolchain/install-tv.sh <电视IP>:5555    # 静默安装，不需要遥控器确认
```

## 提交前检查

1. `cd server && python3 -m unittest discover -s tests` 全绿；
2. 改了 `android/` 的话，`./gradlew assembleDebug` 能出 APK；
3. 别把 `config.json`、`data/`、真实 IP、账号密码、APK 提交上来（`.gitignore` 已覆盖大部分）。

## 代码风格

- Python：4 空格缩进，尽量只用标准库，注释用中文；
- Kotlin：4 空格缩进，`MainActivity` 里以“最小可用”为准，不引入多余依赖；
- 提交信息用英文的 `feat:` / `fix:` / `docs:` / `chore:` 前缀，正文可写中文。

## 协议约定（改动前请看一眼）

| 方向 | 接口 |
|---|---|
| 手机 → 服务端 | 标准 DLNA：SSDP + `/device.xml` + `/AVTransport/control` 等 |
| 电视 App ↔ 服务端 | `GET /api/recv/<id>/poll`（长轮询指令）、`POST /api/recv/<id>/state`（上报状态） |
| 服务端 ↔ 控制台 | `GET /api/status`、`GET /api/receivers`、`POST /api/control` |

电视端 App 首次上报时会自动注册，不需要写进 `config.json`。