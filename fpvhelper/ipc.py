"""辅助程序 <-> 游戏 的本机 UDP 协议。

两边都只认定长二进制小包，不做文本解析。跑在 127.0.0.1 上，丢包就地丢，
下一帧补齐——遥控数据是"最新状态"，重传没有意义。

包一共两种，靠 type 字段分：

  状态包 辅助程序 -> 游戏  32 字节  每帧一包（默认 250 Hz）
    magic 'EFPV' / ver / type=1 / seq u16 / t_ms u32
    roll pitch yaw  int16  度每秒（角速率指令，正 = 右滚 / 抬头 / 右偏航）
    throttle        u16    0..1000
    switches u16（arm / mode / buzzer / turtle 按位）/ buttons u16 / aux u16
    flags    u16（ST_* 位，见下）/ crc32 u32

  状态包 游戏 -> 辅助程序  26 字节  每 500 ms 一包（心跳 + 回执）
    magic 'EFPV' / ver / type=2 / ack_seq u16 / echo_t u32
    flags u16 / lost u16（游戏侧数出来的缺号）/ game_ms u32 / fps u16
    crc32 u32

echo_t 是把辅助程序包里的 t_ms 原样抄回来，辅助程序拿它算 RTT，
不需要两边时钟对齐。
"""
from __future__ import annotations

import struct
import zlib

MAGIC = b'EFPV'
VER = 1

TYPE_STATE = 1
TYPE_STATUS = 2

# ---- 状态包 flags（辅助程序 -> 游戏）
ST_VALID = 1 << 0      # 映射齐了，归一化可信
ST_ARMED = 1 << 1      # 解锁
ST_HOLD = 1 << 2       # 急停：游戏侧应当把相机交还
ST_CALIB = 1 << 3      # 正在校准，别接管相机

# ---- 回执包 flags（游戏 -> 辅助程序）
SG_RUNNING = 1 << 0    # 游戏侧接收端在跑
SG_TAKEOVER = 1 << 1   # 相机已被 FPV 接管
SG_WANT_RESET = 1 << 2 # 游戏侧要求辅助程序回中/重来

STATE = struct.Struct('<4sBBHIhhhHHHHHI')
STATUS = struct.Struct('<4sBBHIHHIHI')
STATE_SIZE = STATE.size      # 32
STATUS_SIZE = STATUS.size    # 26

RATE_MAX = 32767
THR_MAX = 1000


def _crc(payload: bytes) -> int:
    return zlib.crc32(payload) & 0xFFFFFFFF


def clamp_rate(v) -> int:
    return max(-RATE_MAX, min(RATE_MAX, int(round(v))))


def clamp_throttle(v) -> int:
    return max(0, min(THR_MAX, int(round(v))))


def pack_state(seq, t_ms, roll, pitch, yaw, throttle,
               switches=0, buttons=0, aux=0, flags=0) -> bytes:
    """三路角速率（度/秒）+ 油门（0..1000）+ 开关位。"""
    body = STATE.pack(MAGIC, VER, TYPE_STATE, seq & 0xFFFF, t_ms & 0xFFFFFFFF,
                      clamp_rate(roll), clamp_rate(pitch), clamp_rate(yaw),
                      clamp_throttle(throttle), switches & 0xFFFF,
                      buttons & 0xFFFF, aux & 0xFFFF, flags & 0xFFFF, 0)
    return body[:-4] + struct.pack('<I', _crc(body[:-4]))


def unpack_state(data: bytes):
    if len(data) != STATE_SIZE:
        return None
    magic, ver, typ, seq, t_ms, roll, pitch, yaw, thr, sw, btn, aux, flags, crc = STATE.unpack(data)
    if magic != MAGIC or typ != TYPE_STATE or ver != VER:
        return None
    if crc != _crc(data[:-4]):
        return None
    return {'seq': seq, 't_ms': t_ms, 'roll': roll, 'pitch': pitch, 'yaw': yaw,
            'throttle': thr, 'switches': sw, 'buttons': btn, 'aux': aux, 'flags': flags}


def pack_status(ack_seq, echo_t, flags, lost, game_ms, fps) -> bytes:
    body = STATUS.pack(MAGIC, VER, TYPE_STATUS, ack_seq & 0xFFFF, echo_t & 0xFFFFFFFF,
                       flags & 0xFFFF, min(0xFFFF, lost), game_ms & 0xFFFFFFFF,
                       min(0xFFFF, int(fps)), 0)
    return body[:-4] + struct.pack('<I', _crc(body[:-4]))


def unpack_status(data: bytes):
    if len(data) != STATUS_SIZE:
        return None
    magic, ver, typ, ack, echo_t, flags, lost, game_ms, fps, crc = STATUS.unpack(data)
    if magic != MAGIC or typ != TYPE_STATUS or ver != VER:
        return None
    if crc != _crc(data[:-4]):
        return None
    return {'ack_seq': ack, 'echo_t': echo_t, 'flags': flags, 'lost': lost,
            'game_ms': game_ms, 'fps': fps}


def describe() -> str:
    return (f'状态包 {STATE_SIZE}B 辅助->游戏，回执包 {STATUS_SIZE}B 游戏->辅助，'
            f'本机 UDP，magic {MAGIC.decode()} v{VER}')
