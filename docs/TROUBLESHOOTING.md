# 常见问题与排错指南

## 报错：`BLEDevice` 没有 `rssi` 属性

新版 Bleak 已经把 RSSI 从 `BLEDevice` 移到 `AdvertisementData`。扫描时需要
使用 `return_adv=True`，然后从 `(device, advertisement)` 中读取
`advertisement.rssi`。当前版本的脚本已经包含这一兼容处理。

## 内置日历能显示，但传输的图片始终空白

内置日历能显示，说明屏幕硬件、引脚配置、初始化和刷新链路基本正常，问题
通常出在自定义图像的发送顺序。

在实测的 SSD1619 驱动中，清屏命令完成刷新后会关闭驱动。因此必须在清屏后
重新执行一次初始化，再发送图像数据。同时要使用协商后的 MTU，并按照固件
网页客户端的流控方式发送数据。

## 屏幕被清空了，但新画面没有出现

检查日志中是否完整出现以下内容：

```text
Sent black 62/62 chunks
Sent red 62/62 chunks
Requesting screen refresh …
Refresh command sent.
```

如果传输在中途停止：

- 把电子价签移动到 Mac 附近；
- 确认浏览器或其他 BLE 客户端没有连接该设备；
- 重新给电子价签上电后再试；
- 查看 `logs/error.log` 中的具体异常。

## 找不到匹配的墨水屏设备

- 确认电子价签已经上电；
- 确认广播名称为 `NRF_EPD_*`；
- 断开其他正在占用设备的 BLE 客户端；
- 首次测试时让设备尽量靠近 Mac；
- 使用固件内置日历测试区分硬件问题和图像编码问题。

```zsh
.venv/bin/python epd_status.py --calendar-test
```

## Codex 配额请求失败

程序要求本机已经使用 ChatGPT 账号登录 Codex，并读取
`~/.codex/auth.json`。如果当前使用的是 API key 模式，则无法通过这里的接口
读取订阅配额。

常见情况：

- HTTP 401 或 403：Codex 登录可能过期，需要重新登录；
- DNS 或超时错误：检查 Mac 的网络连接；
- 找不到 `auth.json`：先完成 Codex 登录；
- 返回内容没有配额窗口：当前账号类型可能不提供对应字段。

## 定时更新没有执行

Mac 必须处于开机、用户已登录且系统唤醒的状态。先检查任务状态和日志：

```zsh
launchctl print gui/$(id -u)/com.local.epd-ai-quota-display
tail -n 100 logs/update.log
tail -n 100 logs/error.log
```

`launchd` 不会仅仅为了这个任务唤醒正在睡眠的 Mac。睡眠期间错过的更新，
需要等系统恢复可用后才能再次执行。

## 后台任务可以联网，但没有蓝牙权限

首次手动运行脚本时，macOS 可能要求授予 Terminal 或 Python 蓝牙权限。请在：

```text
系统设置 → 隐私与安全性 → 蓝牙
```

确认相关程序已经获得权限。重新安装 LaunchAgent 后，可以通过
`scripts/update-now.sh` 立即触发一次，并观察错误日志。

## 屏幕上的小字难以看清

墨水屏的实际显示效果与电脑预览不同。不要只看浏览器缩放后的页面，应该先
生成 400×300 原始预览，再执行一次实体屏测试。当前设计已经针对实屏放大了：

- `CODEX` 和 `CLAUDE CODE`；
- `5 HOURS` 和 `7 DAYS`；
- 百分号；
- 重置时间；
- 底部的完整更新时间。

## `99%` 与下面的进度条重叠

这不是进度条宽度的问题，而是字体可见区域与 Pillow 文字坐标之间存在偏移。
尤其是 PingFang 的大号数字，实际字形底部可能比指定坐标低很多。

当前版本已经把大数字上移、进度条下移，并使用 `99%` 做过边界测试。如果
修改字号或字体，应重新生成一张两个窗口都是 `99%` 的 400×300 预览，不要
只使用较短的 `8%` 或 `16%` 判断间距。

参考预览：

![99% 压力测试](assets/quota-display-99-percent-test.png)

