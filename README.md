# Endfield FPV Helper

给《明日方舟：终末地》(ZML) 的自由镜头模组配的遥控器辅助程序。跑在 Linux 桌面上，
把一台 USB 遥控器（OpenTX / EdgeTX 之类）读进来，归一化成杆量，再用本机 UDP 以 250 Hz
发给游戏侧的模组。模组那边只收数字，遥控器是什么样的、通道怎么排、曲线多陡，
一概不管。

模组本体在 [ZML-FPVfreecam](https://github.com/snowinging/ZML-FPVfreecam)，两边用同一套
v2 协议，版本必须一致。

## 它做什么

- 从 `/dev/input/event*`（evdev）或 `/dev/hidraw*`（原始 HID）读遥控器通道。
- 按 `mapping.toml` 把通道绑到功能：横滚 / 俯仰 / 偏航 / 油门，加解锁、模式、回平、翻正四个开关。
- 归一化：轴 -1..1、油门 0..1、开关 true/false、多档拨杆输出档位序号；
  带反向、死区、指数、微调、速率。
- 打包成 32 字节小包发到 `127.0.0.1:47800`，收回游戏侧 26 字节回执，算出 RTT 和游戏侧帧率。
- 附一个 tkinter 界面：左边看实时通道值，右边改映射，标定也在里面完成。

程序里不写死任何「第几路是油门」。通道到功能的对应全在配置文件里，换遥控器只改配置。

## 环境要求

- Linux（读设备走 evdev / hidraw，没有 Windows 或 macOS 路径）。
- Python 3.11 暂不需要第三方依赖。
- tkinter：Debian/Ubuntu 装 `python3-tk`，Arch 请安装 `tk`，Fedora 请装 `python3-tkinter`。
- evdev（可选但推荐）：`python3-evdev` 或 `pip install evdev`。
- 设备权限：udev 规则带 uaccess 标签的设备，无需root。
  普通 `/dev/input/event*` 归 `input` 组，不要 sudo 跑本程序。

## 快速开始

```bash
# 1. 检测并链接遥控器。
python3 tools/fpv_monitor.py --list

# 2. 标定：设定功能与通道，保存以写入 ~/.config/endfield-fpv/mapping.toml
python3 tools/fpv_learn.py

# 3. 检查行程：提示出现后把每根摇杆推至极限以校准
python3 tools/fpv_range.py

# 4. 打开界面，确认实时值在动，勾选发送到游戏以开启发送端
python3 tools/ZML_FPVfreecam-HelperGUI.py
```

运行 `python3 tools/fake_game.py --bars`以使用我们构建的虚拟接收端进行验证
它会假装成游戏端收包、回执。

## 配置

配置文件 `~/.config/endfield-fpv/mapping.toml`，缺失时按设备现有通道生成一份占位内容。
每次保存前会把旧文件复制成 `mapping.toml.bak`。

```toml
[device]
match = "BETAFPV"        # 按设备名匹配（evdev / hidraw 通用）
backend = "evdev"        # evdev | hidraw
path = "/dev/input/event31"   # 直接指定节点，若填写则忽略 match

[hidraw]                 # 仅有 hidraw 后端会用到
offset = 0
width = 16
endian = "little"
count = 8

[funcs.roll]
channel = 0              # 通道号：设备里的第几路，未必等于摇杆的物理位置
kind = "axis"            # axis 轴 / range 行程（油门）/ switch 开关 / select 多档（拨杆）
min = 0
max = 2047
center = 1023
invert = false
deadband = 0.01
expo = 0.0
trim = 0.0
rate = 1.0
learned = true
```

roll 横滚、pitch 俯仰、yaw 偏航、throttle 油门、arm 解锁、mode 模式、buzzer 回平、turtle 翻正。
模式、回平、翻正这三个开关需要在游戏端有对应通道才会生效。

## 协议（v2）

收发端均只收发长二进制小包，不做文本解析，都通过 `127.0.0.1`，校验用 crc32。

状态包，辅助程序 → 游戏，32 字节，每帧一包（默认 250 Hz）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| magic | 4B | `EFPV` |
| ver / type | u8 / u8 | 2 / 1 |
| seq | u16 | 序号，回执里带回来 |
| t_ms | u32 | 单调时钟毫秒，回执原样抄回，用来算 RTT |
| roll / pitch / yaw | int16 ×3 | 杆量 -10000..10000，即 -100%..100%，正为右滚 / 推杆向前 / 右偏航 |
| throttle | u16 | 0..1000 |
| switches | u16 | bit0 解锁、bit1 模式、bit2 回平、bit3 翻正 |
| buttons | u16 | 按位，暂未使用 |
| aux | u16 | 低 2 位＝模式档位（0 角度 / 1 半自稳 / 2 手动），需 `ST_MODE_SEL` 才算数 |
| flags | u16 | 见下 |
| crc32 | u32 | 前 28 字节的 crc32 |

flags 位：`ST_VALID`(1) 映射齐了可信、`ST_ARMED`(2) 解锁、`ST_HOLD`(4) 急停交还相机、
`ST_CALIB`(8) 校准中别接管、`ST_MODE_SEL`(16) aux 里带档位。

回执包，游戏 → 辅助程序，26 字节，每 500 ms 一包：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| magic / ver / type | 4B / u8 / u8 | `EFPV` / 2 / 2 |
| ack_seq | u16 | 最后一个收到的状态包序号 |
| echo_t | u32 | 抄回该包的 t_ms |
| flags | u16 | `SG_RUNNING`(1) 接收端在跑、`SG_TAKEOVER`(2) 相机已被接管、`SG_WANT_RESET`(4) 要求回中 |
| lost | u16 | 游戏侧数出来的缺号 |
| game_ms | u32 | 游戏侧时钟 |
| fps | u16 | 游戏帧率 |
| crc32 | u32 | 前 22 字节的 crc32 |


## 代码结构

```
fpvhelper/channels.py   输入层与归一化：evdev / hidraw / serial(CRSF，预留) 后端 + Mapping
fpvhelper/config.py     mapping.toml 读写（读用 tomllib，写自己来）
fpvhelper/ipc.py        协议打包与解包
fpvhelper/link.py       UDP 收发、序号、RTT 与丢包统计
tools/                  六个脚本，见 tools/README.md
```

evdev 后端首选：内核将 HID 归一化成 `ABS_X..ABS_THROTTLE` 和 `BTN_*`，
OpenTX / EdgeTX 的 USB 摇杆模式都属这一类，换遥控器通常只改 `[device] match`。
hidraw 后端是自己拆原始报告，用来读 evdev 看不见的通道（超过 8 路的、藏在厂商报文里的），
靠 `[hidraw]` 的 offset / width / endian / count 描述布局。

## 排障

- `fpv_monitor.py --list` 里没有设备：先看 `ls /dev/input/event*`，再把用户加进 `input` 组；
  hidraw 设备确认 `/sys/class/hidraw/hidrawN/device/uevent` 里的 HID_NAME 与 `match` 对得上。
- 通道数值在动但游戏里没反应：跑 `fake_game.py --bars` 看包有没有发出去、字段对不对，
  再看模组侧那项设置里的端口与启用开关。
- 游戏里杆量到头了但摇杆还没到极限：行程记得过窄，重跑一次 `fpv_learn.py` 或 `fpv_range.py`，
  标定时一定要把杆推到底。
- 底栏提示需要重新标定：映射里的行程与当前设备不符（比如换了另一台遥控器），重标一次。
- 界面底栏的回中检测只检查带弹簧回中的轴，偏离超过量程 6% 会报出具体通道，那是硬件漂移不是程序问题。
- 无图形环境（ssh 里）用 `--selftest`，它只验证输入设备可用，不开窗。

## 相关

- 模组：[ZML-FPVfreecam](https://github.com/snowinging/ZML-FPVfreecam)（自由镜头 + 接管，C++/Lua）
- 工具脚本说明：[tools/README.md](tools/README.md)
