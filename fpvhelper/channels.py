"""输入层 + 软映射层。

原则：程序里不写死"第几路是油门"。设备只提供一串通道，通道叫什么、怎么用，
全在 mapping.toml 里配。换遥控器只改配置，不改代码。

后端：
  evdev  —— Linux 输入子系统，内核已经把 HID 归一化成 ABS_X..ABS_THROTTLE + 按键。
            OpenTX/EdgeTX 的 USB 摇杆模式都是这套，跨遥控器最稳。
  hidraw —— 原始 HID 报告，按 (偏移, 宽度, 字节序, 通道数) 解。给 evdev 看不到的通道用，
            换手柄时可能要改这几个数，但改的是配置不是代码。
  serial —— CRSF/ELRS 串口，16 通道全量，先留接口。
"""
from __future__ import annotations

import glob
import math
import os
import select
import time

try:
    import evdev
    from evdev import ecodes as e
except ImportError:  # evdev 缺失不影响 hidraw 后端
    evdev = None


# ---------------------------------------------------------------- 数据结构

class Channel:
    """一路物理通道。label 只是给人看的，程序逻辑不依赖它。"""
    __slots__ = ('index', 'label', 'min', 'max', 'value')

    def __init__(self, index, label, lo, hi, value=0):
        self.index, self.label = index, label
        self.min, self.max = lo, hi
        self.value = value

    def __repr__(self):
        return f'<ch{self.index} {self.label} {self.min}..{self.max}={self.value}>'


class Frame:
    """一次采样：各通道原始值 + 按键位图。"""
    __slots__ = ('t', 'values', 'buttons')

    def __init__(self, t, values, buttons):
        self.t, self.values, self.buttons = t, values, buttons


# ---------------------------------------------------------------- evdev 后端

_PREFERRED_AXES = ('ABS_X', 'ABS_Y', 'ABS_Z', 'ABS_RX', 'ABS_RY', 'ABS_RZ',
                   'ABS_THROTTLE', 'ABS_RUDDER', 'ABS_WHEEL', 'ABS_GAS', 'ABS_BRAKE', 'ABS_MISC')


class EvdevSource:
    name = 'evdev'

    def __init__(self, match='', path=None):
        self.path = path or self._find(match)
        self.dev = evdev.InputDevice(self.path)
        self._caps = self.dev.capabilities(absinfo=True)
        self.keys = sorted(self._caps.get(e.EV_KEY, []))
        self.channels = self._build_channels()

    @staticmethod
    def _find(match):
        cands = []
        for p in evdev.list_devices():
            try:
                d = evdev.InputDevice(p)
            except OSError:
                continue
            if e.EV_ABS not in d.capabilities():
                continue
            cands.append((match.lower() in d.name.lower() if match else False, d.name, p))
        if not cands:
            raise RuntimeError('没找到任何带轴的输入设备')
        cands.sort(key=lambda c: (not c[0], c[1]))
        return cands[0][2]

    def _build_channels(self):
        absinfo = {code: info for code, info in self._caps.get(e.EV_ABS, [])}
        order = []
        for want in _PREFERRED_AXES:
            code = e.ecodes.get(want)
            if code in absinfo:
                order.append(code)
        for code in sorted(absinfo):
            if code not in order:
                order.append(code)
        chans = []
        for i, code in enumerate(order):
            info = absinfo[code]
            name = e.ABS.get(code, f'ABS_{code}')
            chans.append(Channel(i, name, info.min, info.max, info.value))
        return chans

    def poll(self, timeout=0.2):
        """读到完整的 SYN_REPORT 就返回一帧，超时返回 None。"""
        r, _, _ = select.select([self.dev.fd], [], [], timeout)
        if not r:
            return None
        buttons = None
        try:
            for ev in self.dev.read():
                if ev.type == e.EV_ABS:
                    for c in self.channels:
                        if e.ABS.get(ev.code) == c.label:
                            c.value = ev.value
                elif ev.type == e.EV_KEY and ev.value in (0, 1):
                    buttons = self._buttons()
                elif ev.type == e.EV_SYN and ev.code == e.SYN_REPORT:
                    if buttons is None:
                        buttons = self._buttons()
                    return Frame(time.monotonic(),
                                 {c.index: c.value for c in self.channels}, buttons)
        except OSError:
            return None
        return None

    def _buttons(self):
        try:
            active = set(self.dev.active_keys(verbose=False))
        except OSError:
            return 0
        bits = 0
        for i, k in enumerate(self.keys):
            if k in active:
                bits |= 1 << i
        return bits

    def pressable(self):
        return [_name(k) for k in self.keys]

    def describe(self):
        lines = [f'evdev 设备 {self.path}  {self.dev.name}']
        for c in self.channels:
            lines.append(f'  ch{c.index} {c.label:<14} {c.min}..{c.max} 当前 {c.value}')
        lines.append(f'  按键 {len(self.keys)} 个: ' + ', '.join(self.pressable()[:12]))
        return '\n'.join(lines)


# ---------------------------------------------------------------- hidraw 后端

class HidrawSource:
    """原始 HID 报告。通道布局由 min/max/offset/width/endian/count 描述，全在配置里。"""
    name = 'hidraw'

    def __init__(self, match='', path=None, offset=1, width=2, endian='little', count=8):
        self.path = path or self._find(match)
        self.offset, self.width, self.endian, self.count = offset, width, endian, count
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        self.channels = [Channel(i, f'raw{i}', 0, 2 ** (8 * width) - 1, 0) for i in range(count)]
        self._u16 = (lambda b, o: b[o] | (b[o + 1] << 8)) if endian == 'little' else \
                    (lambda b, o: (b[o] << 8) | b[o + 1])

    @staticmethod
    def _find(match):
        for p in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
            try:
                uevent = open(os.path.join(p, 'device', 'uevent')).read()
            except OSError:
                continue
            name = ''
            for line in uevent.splitlines():
                if line.startswith('HID_NAME='):
                    name = line.split('=', 1)[1]
            if match and match.lower() in name.lower():
                return '/dev/' + os.path.basename(p)
        raise RuntimeError(f'未找到匹配 "{match}" 的 hidraw 设备，可用 fpv_monitor.py --list 查看')

    def poll(self, timeout=0.2):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        try:
            data = os.read(self.fd, 64)
        except BlockingIOError:
            return None
        if len(data) < self.offset + self.count * (self.width // 2) * 2:
            return None
        vals = {}
        for i in range(self.count):
            off = self.offset + i * self.width
            raw = self._u16(data, off) if self.width == 2 else data[off]
            vals[i] = raw
            self.channels[i].value = raw
        buttons = int.from_bytes(data[-2:], 'little') if self.width == 2 else 0
        return Frame(time.monotonic(), vals, buttons)

    def describe(self):
        return (f'hidraw 设备 {self.path}\n' + '\n'.join(
            f'  ch{c.index} {c.label} {c.min}..{c.max} 当前 {c.value}' for c in self.channels))


# ---------------------------------------------------------------- 串口后端（预留）

class SerialSource:
    """CRSF/ELRS 串口：16 通道 11 位打包，帧头 0xC8，长度 24。等真要用再实现。"""
    name = 'serial'

    def __init__(self, *a, **kw):
        raise NotImplementedError('CRSF 串口后端还没写：需要 pyserial，帧格式 0xC8 + 22 字节 11 位通道')


def open_source(cfg):
    """按配置开一个输入源。cfg 是 mapping.toml 里的 [device]/[hidraw] 段。"""
    backend = cfg.get('backend', 'evdev')
    if backend == 'hidraw':
        h = cfg.get('hidraw', {})
        return HidrawSource(cfg.get('match', ''), h.get('path'), h.get('offset', 1),
                            h.get('width', 2), h.get('endian', 'little'), h.get('count', 8))
    if backend == 'serial':
        return SerialSource()
    return EvdevSource(cfg.get('match', ''), cfg.get('path'))


def list_devices():
    out = []
    if evdev:
        for p in evdev.list_devices():
            try:
                d = evdev.InputDevice(p)
            except OSError:
                continue
            if e.EV_ABS in d.capabilities():
                out.append((p, d.name, [e.ABS[c] for c, _ in d.capabilities(absinfo=True)[e.EV_ABS]]))
    for p in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        try:
            name = [l.split('=', 1)[1] for l in open(os.path.join(p, 'device', 'uevent'))
                    if l.startswith('HID_NAME=')][0].strip()
        except (OSError, IndexError):
            continue
        out.append(('/dev/' + os.path.basename(p), name + '  [hidraw]', []))
    return out


def _name(code):
    """ecodes 里有别名时会返回元组，这里统一拿第一个名字。"""
    if code in e.KEY:
        n = e.KEY[code]
    elif code in e.BTN:
        n = e.BTN[code]
    else:
        return f'code{code}'
    return n[0] if isinstance(n, tuple) else n


# ---------------------------------------------------------------- 软映射

AXIS_FUNCS = ('roll', 'pitch', 'yaw')
STICK_FUNCS = ('throttle',)
SWITCH_FUNCS = ('arm', 'mode', 'buzzer', 'turtle')
ALL_FUNCS = AXIS_FUNCS + STICK_FUNCS + SWITCH_FUNCS

# 功能名的中文标签。键名一律保持英文（mapping.toml 里写的就是这些键），
# 只有界面与日志显示中文：roll 横滚 / pitch 俯仰 / yaw 偏航 / throttle 油门，
# arm 解锁 / mode 模式 / buzzer 回平 / turtle 翻正。
FUNC_CN = {'roll': '横滚', 'pitch': '俯仰', 'yaw': '偏航', 'throttle': '油门',
           'arm': '解锁', 'mode': '模式', 'buzzer': '回平', 'turtle': '翻正'}


class Mapping:
    """通道号 -> 功能 的软映射，外加每路的归一化参数。"""

    def __init__(self, data=None):
        self.data = data or {'device': {'match': '', 'backend': 'evdev'}, 'funcs': {}}

    @property
    def funcs(self):
        return self.data.setdefault('funcs', {})

    def channel_meta(self, channels, index):
        for c in channels:
            if c.index == index:
                return c
        return None

    def apply(self, frame, channels):
        """把一帧原始值变成 {功能名: 值}。轴 -1..1，油门 0..1，开关 True/False 或档位序号。"""
        out = {}
        for name, spec in self.funcs.items():
            if spec.get('channel') is None:
                continue
            raw = frame.values.get(spec['channel'])
            if raw is None:
                continue
            meta = self.channel_meta(channels, spec['channel'])
            lo = spec.get('min', meta.min if meta else 0)
            hi = spec.get('max', meta.max if meta else 1)
            kind = spec.get('kind', 'axis')
            if kind == 'switch':
                mid = (lo + hi) / 2
                val = (raw > mid) if not spec.get('invert') else (raw < mid)
            elif kind == 'select':
                bands = spec.get('bands') or [lo + (hi - lo) * i / 3 for i in range(4)]
                val = 0
                for i, edge in enumerate(bands):
                    if raw >= edge:
                        val = i
                if spec.get('invert'):
                    val = len(bands) - 1 - val
                # 档位序号 -> 值。带语义的多档开关（模式）在这里换成模式号：
                # map = [0, 1, 2] 表示从最小档到最大档依次是 角度模式 / 半自稳 / 手动模式
                order = spec.get('map') or []
                if 0 <= val < len(order):
                    val = order[val]
            elif kind == 'range':
                span = (hi - lo) or 1
                val = (raw - lo) / span
                val = 1 - val if spec.get('invert') else val
                val = _deadband_scale(_curve(val, spec.get('expo', 0)), spec.get('deadband', 0), 0.0, 1.0)
            else:
                center = spec.get('center', (lo + hi) / 2)
                span = (hi - center) if raw >= center else (center - lo)
                val = 0.0 if span <= 0 else (raw - center) / span
                val = max(-1.0, min(1.0, val * spec.get('rate', 1.0) + spec.get('trim', 0.0)))
                val = _deadband_scale(_curve(val, spec.get('expo', 0)), spec.get('deadband', 0), -1.0, 1.0)
                if spec.get('invert'):
                    val = -val
            out[name] = val
        return out

    def learned_channels(self):
        return {s.get('channel') for s in self.funcs.values() if s.get('channel') is not None}

    def summary(self):
        lines = []
        for name in ALL_FUNCS:
            s = self.funcs.get(name)
            if not s or s.get('channel') is None:
                lines.append(f'  {FUNC_CN.get(name, name):<4} {name:<9} 未映射')
                continue
            extra = f"反={'是' if s.get('invert') else '否'}"
            if s.get('kind') == 'switch':
                extra += ' 开关'
            if s.get('kind') == 'select':
                extra += f" 档位x{len(s.get('bands') or [0, 0, 0, 0])}"
                if s.get('map'):
                    extra += ' 顺序' + '/'.join(str(int(m)) for m in s['map'])
            lines.append(f"  {FUNC_CN.get(name, name):<4} {name:<9} ch{s['channel']:<3} "
                         f"{s.get('kind', 'axis'):<7} "
                         f"{s.get('min')}..{s.get('max')} {extra}"
                         f"{'' if s.get('learned', True) else '  (未标定，请先跑 fpv_learn.py)'}")
        return '\n'.join(lines)


def _curve(x, expo):
    if not expo:
        return x
    return (1 - expo) * x + expo * x ** 3


def _deadband_scale(x, db, lo, hi):
    if db <= 0:
        return x
    if lo < 0:                                # 双向轴
        return 0.0 if abs(x) < db else math.copysign((abs(x) - db) / (1 - db), x)
    return 0.0 if x < db else (x - db) / (1 - db)


def empty_mapping(channels, backend='evdev', match=''):
    """按设备现有通道生成一份占位映射：通道号先按顺序填，等 fpv_learn.py 学一遍再定。"""
    data = {'device': {'match': match, 'backend': backend}, 'funcs': {}}
    order = [c.index for c in channels]
    idx = 0
    for name in AXIS_FUNCS + STICK_FUNCS:
        if idx < len(order):
            ch = order[idx]
            meta = channels[ch]
            data['funcs'][name] = {'channel': ch, 'kind': 'range' if name == 'throttle' else 'axis',
                                   'min': meta.min, 'max': meta.max, 'center': (meta.min + meta.max) // 2,
                                   'invert': False, 'deadband': 0.01, 'expo': 0.0, 'learned': False}
            idx += 1
    for name in SWITCH_FUNCS:
        if idx < len(order):
            meta = channels[order[idx]]
            data['funcs'][name] = {'channel': order[idx], 'kind': 'switch',
                                   'min': meta.min, 'max': meta.max, 'invert': False, 'learned': False}
            idx += 1
    return Mapping(data)
