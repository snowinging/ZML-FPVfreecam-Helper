#!/usr/bin/env python3
"""遥控器配置工具：左侧显示通道实时值，右侧编辑映射，校准可在此完成。

  ./fpv_gui.py                      使用 ~/.config/endfield-fpv/mapping.toml
  ./fpv_gui.py --config /tmp/x.toml
  ./fpv_gui.py --selftest           不打开窗口，仅验证输入设备是否可用（无图形环境时使用）
  ./fpv_gui.py --send --port 47800  启动后立即向游戏发送状态包，无需手动勾选

界面只做三件事：显示设备上报的通道值、把通道绑定到功能、保存为 TOML 配置。
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpvhelper import channels as ch  # noqa: E402
from fpvhelper import config as cfg  # noqa: E402
from fpvhelper import ipc  # noqa: E402
from fpvhelper.link import UdpLink  # noqa: E402



BG = '#14161a'
PANEL = '#1b1e24'
FG = '#d7dce2'
DIM = '#7d8794'
ACCENT = '#c8a06a'
LOW = '#7fa0c0'
LINE = '#2a2f37'
MONO = ('DejaVu Sans Mono', 9)

LEARNABLE = ('roll', 'pitch', 'yaw', 'throttle', 'arm', 'mode', 'buzzer', 'turtle')


def cn(name):
    """功能名的中文显示名；mapping.toml 里的键名仍为英文。"""
    return ch.FUNC_CN.get(name, name) if name else name


# 类型下拉：写进 mapping.toml 的还是前面那个英文键
KINDS = (('axis', '轴'), ('range', '行程'), ('switch', '开关'), ('select', '多档'))


def kind_label(key):
    for k, t in KINDS:
        if k == key:
            return f'{k} {t}'
    return key or 'axis 轴'


def kind_key(label):
    return (label or '').split(' ')[0] or 'axis'


def _mean(acc):
    """acc 是 [min, max, 累加和, 次数]。"""
    return acc[2] / acc[3] if acc[3] else acc[0]


class Bar(tk.Canvas):
    """一条通道的标尺：底色带 = 这次看到的活动范围，竖线 = 静止位，亮块 = 当前位置。"""

    def __init__(self, master, width=250, height=14):
        super().__init__(master, width=width, height=height, bg=PANEL, highlightthickness=0)
        self.w, self.h = width, height
        self.band = self.create_rectangle(0, 3, 0, height - 3, fill=LINE, width=0)
        self.fill = self.create_rectangle(0, 3, 0, height - 3, fill=ACCENT, width=0)
        self.rest = self.create_line(0, 1, 0, height - 1, fill=DIM)
        self.create_line(0, height - 1, width, height - 1, fill=LINE)

    def set(self, raw, lo, hi, rest=None, seen=None):
        span = (hi - lo) or 1

        def pos(v):
            return max(0.0, min(self.w, (v - lo) / span * self.w))

        if seen:
            self.coords(self.band, min(pos(seen[0]), pos(seen[1])), 3,
                        max(pos(seen[0]), pos(seen[1])), self.h - 3)
        r = pos(rest if rest is not None else (lo + hi) / 2)
        x = pos(raw)
        self.coords(self.fill, min(r, x), 3, max(r, x), self.h - 3)
        self.coords(self.rest, r, 0, r, self.h)


class Gimbal(tk.Canvas):
    """一个摇杆框：横一路纵一路，中间那个点就是杆现在的位置。"""

    def __init__(self, master, size=150):
        super().__init__(master, width=size, height=size, bg=PANEL, highlightthickness=0)
        self.s = size
        self.c = size / 2
        self.r = size * 0.40                       # 满舵半径，边留一点
        self.create_rectangle(0, 0, size - 1, size - 1, outline=LINE)
        self.create_line(self.c, 0, self.c, size, fill=LINE, dash=(3, 3))
        self.create_line(0, self.c, size, self.c, fill=LINE, dash=(3, 3))
        self.trail = self.create_line(self.c, self.c, self.c, self.c, fill=DIM)
        self.dot = self.create_oval(0, 0, 0, 0, fill=ACCENT, outline='')
        font = ('DejaVu Sans Mono', 11, 'bold')
        pad = 5
        self.edge = {
            'l': self.create_text(pad, self.c, text='', fill=LOW, font=font, anchor='w'),
            'r': self.create_text(size - pad, self.c, text='', fill=ACCENT, font=font, anchor='e'),
            't': self.create_text(self.c, pad - 2, text='', fill=ACCENT, font=font, anchor='n'),
            'b': self.create_text(self.c, size - pad + 2, text='', fill=LOW, font=font, anchor='s'),
        }
        self.label_state = None

    def signs(self, h, v):
        """h/v：+1 = 正方向在右/上，-1 = 反了（勾了反向），0 = 这条还没绑通道。"""
        if self.label_state == (h, v):
            return
        self.label_state = (h, v)
        txt = {'l': '+' if h < 0 else ('-' if h > 0 else ''),
               'r': '+' if h > 0 else ('-' if h < 0 else ''),
               't': '+' if v > 0 else ('-' if v < 0 else ''),
               'b': '+' if v < 0 else ('-' if v > 0 else '')}
        for key, item in self.edge.items():
            self.itemconfig(item, text=txt[key], fill=ACCENT if txt[key] == '+' else LOW)

    def set(self, x, y):
        if x is None or y is None:
            self.coords(self.dot, 0, 0, 0, 0)
            self.coords(self.trail, self.c, self.c, self.c, self.c)
            return
        px, py = self.c + x * self.r, self.c - y * self.r
        self.coords(self.dot, px - 4, py - 4, px + 4, py + 4)
        self.coords(self.trail, self.c, self.c, px, py)


class App:
    def __init__(self, root, config_path):
        self.root = root
        self.config_path = config_path
        self.mapping, _ = cfg.load(config_path)
        self.src = None
        self.channels = []
        self.frames = 0
        self.hz_t0 = time.monotonic()
        self.hz = 0.0
        self.last = {}
        self.seen = {}
        self.learn = None
        self.sel = None
        self.rows = {}
        self.link = None
        self.link_port = int((self.mapping.data.get('link') or {}).get('port', 47800))

        root.title('Endfield FPV 遥控器')
        root.configure(bg=BG)
        self._style()
        self._build()
        self.connect(self.mapping.data.get('device', {}).get('match', ''))
        self.tick()
        self.root.after(4, self.tick_fast)

    # ---------------------------------------------------------------- 界面

    def _style(self):
        s = ttk.Style(self.root)
        try:
            s.theme_use('clam')
        except tk.TclError:
            pass
        s.configure('.', background=BG, foreground=FG, fieldbackground=PANEL,
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, focuscolor=ACCENT)
        s.configure('TCombobox', fieldbackground=PANEL, background=PANEL, arrowcolor=FG)
        s.configure('TEntry', fieldbackground=PANEL, insertcolor=FG)
        s.configure('TCheckbutton', background=PANEL)
        s.configure('TSeparator', background=LINE)
        s.map('TCombobox', fieldbackground=[('readonly', PANEL)])
        s.map('TCheckbutton', background=[('active', PANEL)])

    def _build(self):
        root = self.root
        for i in (0, 2):
            root.columnconfigure(i, weight=1)
        root.rowconfigure(1, weight=1)

        # 顶栏
        holder = tk.Frame(root, bg=BG)
        holder.grid(row=0, column=0, columnspan=3, sticky='ew', padx=14, pady=(12, 6))
        top = tk.Frame(holder, bg=BG)
        top.grid(row=0, column=0, sticky='ew')
        tk.Label(top, text='设备', bg=BG, fg=DIM, font=MONO).pack(side='left')
        self.dev_var = tk.StringVar()
        self.dev_box = ttk.Combobox(top, textvariable=self.dev_var, width=42, state='readonly')
        self.dev_box['values'] = [f'{p}  {n}' for p, n, _ in ch.list_devices()]
        self.dev_box.pack(side='left', padx=(8, 6))
        ttk.Button(top, text='连接', width=6, command=self._connect_from_box).pack(side='left')

        tk.Label(top, text='发送到游戏', bg=BG, fg=DIM, font=MONO).pack(side='left', padx=(22, 6))
        self.send_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, variable=self.send_var, command=self._toggle_send).pack(side='left')
        tk.Label(top, text='端口', bg=BG, fg=DIM, font=MONO).pack(side='left', padx=(10, 4))
        self.port_var = tk.StringVar(value=str(self.link_port))
        port_entry = tk.Entry(top, textvariable=self.port_var, width=7, bg=PANEL, fg=FG,
                              insertbackground=FG, relief='flat', font=MONO)
        port_entry.pack(side='left')
        port_entry.bind('<Return>', lambda e: self._toggle_send(restart=True))
        self.arm_bypass = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text='忽略解锁开关', variable=self.arm_bypass).pack(side='left', padx=(12, 0))
        self.selftest_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text='自检模式', variable=self.selftest_var).pack(side='left', padx=(10, 0))
        self.link_label = tk.Label(top, text='', bg=BG, fg=DIM, font=('DejaVu Sans Mono', 8))
        self.link_label.pack(side='right', padx=(0, 14))
        self.hz_label = tk.Label(top, text='--', bg=BG, fg=DIM, font=MONO)
        self.hz_label.pack(side='right')

        # 左：通道
        left = tk.Frame(root, bg=BG)
        left.grid(row=1, column=0, sticky='nsw', padx=(14, 8), pady=4)
        tk.Label(left, text='通道（设备原始值）', bg=BG, fg=DIM, font=MONO).pack(anchor='w')
        self.chan_box = tk.Frame(left, bg=BG)
        self.chan_box.pack(anchor='w', pady=(8, 0))

        # 左下方：两个摇杆框
        tk.Label(left, text='摇杆（按映射取值）', bg=BG, fg=DIM, font=MONO).pack(
            anchor='w', pady=(14, 0))
        gim_row = tk.Frame(left, bg=BG)
        gim_row.pack(anchor='w', pady=(8, 0))
        self.gim = {}
        for side, title in (('left', '左摇杆'), ('right', '右摇杆')):
            cell = tk.Frame(gim_row, bg=BG)
            cell.pack(side='left', padx=(0, 18))
            tk.Label(cell, text=title, bg=BG, fg=DIM, font=MONO, anchor='w').pack(anchor='w')
            canvas = Gimbal(cell)
            canvas.pack()
            cap = tk.Label(cell, text='', bg=BG, fg=DIM, font=('DejaVu Sans Mono', 8), anchor='w')
            cap.pack(anchor='w', pady=(4, 0))
            self.gim[side] = (canvas, cap)

        tk.Frame(root, bg=LINE, width=1).grid(row=1, column=1, sticky='ns', pady=4)

        # 右：映射
        right = tk.Frame(root, bg=BG)
        right.grid(row=1, column=2, sticky='nsew', padx=(10, 14), pady=4)
        tk.Label(right, text='映射（保存至 mapping.toml）', bg=BG, fg=DIM, font=MONO).pack(anchor='w')
        self.map_box = tk.Frame(right, bg=BG)
        self.map_box.pack(anchor='w', pady=(8, 0))
        head = tk.Frame(self.map_box, bg=BG)
        head.pack(anchor='w')
        for txt, w in (('功能', 14), ('通道', 10), ('类型', 7), ('反向', 5), ('指数', 6),
                       ('死区', 6), ('最小 -', 8), ('中位', 7), ('最大 +', 8), ('档位顺序', 8)):
            tk.Label(head, text=txt, bg=BG, fg=DIM, font=MONO, width=w, anchor='w').pack(side='left')
        for name in LEARNABLE:
            self._add_row(name)
        tk.Label(right, text='功能名：横滚 roll / 俯仰 pitch / 偏航 yaw / 油门 throttle；'
                             '解锁 arm / 模式 mode / 回平 buzzer / 翻正 turtle（mapping.toml 里写的是英文键）\n'
                             '最小-：反方向推到底时的原始值；最大+：正方向推到底时的原始值；方向相反时勾选反向。'
                             '中位：摇杆回中时的值（不回中的轴无需填写；select 类型在此填写分界值，逗号分隔）。各项均可直接修改\n'
                             '三档拨杆（比如模式开关）：该行类型选「select 多档」，点「校准」逐档标定；'
                             '「档位顺序」按档位序号从小到大填各行对应的值，模式开关就是 0 角度模式 / 1 半自稳 / 2 手动模式，'
                             '反过来拨就填 2,1,0\n'
                             '游戏内：解锁开关拨到高位后取得相机控制权；模式开关拨到高位为手动模式（纯角速度，随杆），拨到低位为角度模式（松杆自动回平）\n'
                             '回平键按住时忽略杆位、按松杆自稳回平（横滚与俯仰拉平，偏航仍随杆）；'
                             '翻正键按住时收平姿态、冻结偏航、油门切至悬停',
                 bg=BG, fg=DIM, font=('DejaVu Sans Mono', 8), justify='left').pack(anchor='w', pady=(8, 0))

        # 底栏
        bottom = tk.Frame(root, bg=BG)
        # 就绪检查：一行说清"现在卡在哪"，游戏会不会接管、为什么不接管
        ready = tk.Frame(holder, bg=BG)
        ready.grid(row=1, column=0, sticky='ew', pady=(8, 0))
        self.lamp = {}
        for key in ('设备', '映射', '解锁', '发送', '游戏', '控制权'):
            w = tk.Label(ready, text=key + ' --', bg=BG, fg=DIM, font=('DejaVu Sans Mono', 8))
            w.pack(side='left', padx=(0, 12))
            self.lamp[key] = w
        self.lamp_why = tk.Label(ready, text='', bg=BG, fg=LOW, font=('DejaVu Sans Mono', 8))
        self.lamp_why.pack(side='left')

        bottom.grid(row=2, column=0, columnspan=3, sticky='ew', padx=14, pady=(6, 12))
        ttk.Button(bottom, text='保存', width=7, command=self.save).pack(side='left')
        ttk.Button(bottom, text='重载', width=7, command=self.reload).pack(side='left', padx=6)
        tk.Frame(bottom, bg=LINE, width=1, height=22).pack(side='left', padx=8)
        self.sel_label = tk.Label(bottom, text='选中 -', bg=BG, fg=ACCENT, font=MONO, width=12)
        self.sel_label.pack(side='left')
        ttk.Button(bottom, text='校准', width=6,
                   command=lambda: self.start_learn(self.sel)).pack(side='left')
        ttk.Button(bottom, text='设为中位', width=8,
                   command=self.set_center).pack(side='left', padx=6)
        ttk.Button(bottom, text='自动标定行程', width=12,
                   command=lambda: self.start_sweep(self.sel)).pack(side='left')
        ttk.Button(bottom, text='全部轴自动标定', width=14,
                   command=lambda: self.start_sweep(None)).pack(side='left', padx=6)
        ttk.Button(bottom, text='回中检测', width=9,
                   command=self.check_centers).pack(side='left')
        self.status = tk.Label(bottom, text='', bg=BG, fg=FG, font=MONO, anchor='w')
        self.status.pack(side='left', padx=12, fill='x', expand=True)
        if self.sel:
            self.sel_label.config(text=f'选中 {cn(self.sel)}')

    def _add_row(self, name):
        row = tk.Frame(self.map_box, bg=BG)
        row.pack(anchor='w', pady=1)
        spec = self.mapping.funcs.get(name, {})

        mark = tk.Frame(row, bg=LINE, width=3, height=20)
        mark.pack(side='left', padx=(0, 6))
        tk.Label(row, text=f'{cn(name)} {name}', bg=BG, fg=FG, font=MONO,
                 width=14, anchor='w').pack(side='left')

        cvar = tk.StringVar(value=self._chan_label(spec.get('channel')))
        cbox = ttk.Combobox(row, textvariable=cvar, width=9, state='readonly')
        cbox['values'] = ['--'] + [f'ch{c.index}' for c in self.channels] or ['--']
        cbox.pack(side='left')
        cbox.bind('<<ComboboxSelected>>', lambda e, n=name: self._set_field(n, 'channel'))

        default_kind = spec.get('kind', 'axis' if name not in ch.SWITCH_FUNCS else 'switch')
        kvar = tk.StringVar(value=kind_label(default_kind))
        kbox = ttk.Combobox(row, textvariable=kvar, width=10, state='readonly')
        kbox['values'] = [kind_label(k) for k, _ in KINDS]
        kbox.pack(side='left', padx=4)
        kbox.bind('<<ComboboxSelected>>', lambda e, n=name: self._set_field(n, 'kind'))

        ivar = tk.BooleanVar(value=bool(spec.get('invert')))
        ttk.Checkbutton(row, variable=ivar,
                        command=lambda n=name: self._set_field(n, 'invert')).pack(side='left')

        tones = {'min': LOW, 'center': FG, 'max': ACCENT}
        fields = {}
        for key, var in (('expo', tk.StringVar(value=str(spec.get('expo', 0.0)))),
                         ('deadband', tk.StringVar(value=str(spec.get('deadband', 0.01)))),
                         ('min', tk.StringVar()), ('center', tk.StringVar()),
                         ('max', tk.StringVar())):
            entry = tk.Entry(row, textvariable=var, width=6, bg=PANEL, fg=tones.get(key, FG),
                             insertbackground=FG, relief='flat', font=MONO)
            entry.pack(side='left', padx=2)
            entry.bind('<FocusIn>', lambda e, n=name: self._select(n))
            fields[key] = var
            fields[key + '_w'] = entry

        sign = tk.Label(row, text='', bg=BG, fg=DIM, width=2, font=MONO)
        sign.pack(side='left')
        ttk.Button(row, text='校准', width=5,
                   command=lambda: (self._select(name), self.start_learn(name))).pack(side='left')

        # 档位顺序：只对「多档」类型的行显示（其他行 pack_forget 掉）
        mvar = tk.StringVar()
        mentry = tk.Entry(row, textvariable=mvar, width=8, bg=PANEL, fg=FG,
                          insertbackground=FG, relief='flat', font=MONO)
        mentry.bind('<FocusOut>', lambda e, n=name: self._read_map(n))
        mentry.bind('<FocusIn>', lambda e, n=name: self._select(n))

        self.rows[name] = {'chan': cvar, 'chan_w': cbox, 'kind': kvar, 'invert': ivar,
                           'expo': fields['expo'], 'deadband': fields['deadband'],
                           'min': fields['min'], 'center': fields['center'], 'max': fields['max'],
                           'min_w': fields['min_w'], 'center_w': fields['center_w'],
                           'max_w': fields['max_w'], 'mark': mark, 'row': row,
                           'sign': sign, 'sign_last': None, 'map': mvar, 'map_w': mentry}
        for widget in (row, mark):
            widget.bind('<Button-1>', lambda e, n=name: self._select(n))
        if self.sel is None:
            self.sel = name
            mark.config(bg=ACCENT)
        self._refresh_kind(name)
        self._refresh_range(name)

    def _select(self, name):
        if name not in self.rows:
            return
        self.sel = name
        if hasattr(self, 'sel_label'):
            self.sel_label.config(text=f'选中 {cn(name)}')
        self._paint_marks()

    def _paint_marks(self):
        for n, r in self.rows.items():
            learned = self.mapping.funcs.get(n, {}).get('learned') is not False
            r['mark'].config(bg=ACCENT if n == self.sel else (BG if learned else LINE))

    # ---------------------------------------------------------------- 设备

    def connect(self, match):
        if self.src:
            try:
                self.src.dev.close()
            except Exception:
                pass
            self.src = None
        try:
            self.src = ch.open_source({'match': match or '',
                                       'backend': self.mapping.data.get('device', {}).get('backend', 'evdev'),
                                       'hidraw': self.mapping.data.get('hidraw', {})})
        except Exception as exc:
            self.set_status(f'连不上：{exc}')
            return
        self.channels = self.src.channels
        self.last = {c.index: c.value for c in self.channels}
        self.rest = dict(self.last)          # 开界面时的位置，遥控器放平就是静止位
        self.seen = {c.index: (c.value, c.value) for c in self.channels}
        for w in self.chan_box.winfo_children():
            w.destroy()
        self.bars, self.vals = {}, {}
        for c in self.channels:
            line = tk.Frame(self.chan_box, bg=BG)
            line.pack(anchor='w', pady=1)
            tk.Label(line, text=f'ch{c.index}', bg=BG, fg=DIM, font=MONO, width=5,
                     anchor='w').pack(side='left')
            tk.Label(line, text=c.label, bg=BG, fg=FG, font=MONO, width=14,
                     anchor='w').pack(side='left')
            bar = Bar(line)
            bar.pack(side='left')
            val = tk.Label(line, text='', bg=BG, fg=DIM, font=MONO, width=17, anchor='w')
            val.pack(side='left', padx=6)
            self.bars[c.index], self.vals[c.index] = bar, val
        for name, r in self.rows.items():
            r['chan_w']['values'] = ['--'] + [f'ch{c.index}' for c in self.channels]
            if r['chan'].get() not in r['chan_w']['values']:
                r['chan'].set('--')
        real = getattr(getattr(self.src, 'dev', None), 'name', None) or self.src.name
        self.dev_var.set(f'{getattr(self.src, "path", "")}  {real}')
        notes = []
        if match and match.lower() not in real.lower():
            notes.append(f'配置里写的 {match} 没找到，先连了 {real}')
        stale = []
        for name, spec in self.mapping.funcs.items():
            index = spec.get('channel')
            meta = self.mapping.channel_meta(self.channels, index) if index is not None else None
            lo, hi = spec.get('min'), spec.get('max')
            if not meta or lo is None or hi is None:
                continue
            if lo < meta.min - 1 or hi > meta.max + 1 or hi - lo < (meta.max - meta.min) * 0.3:
                stale.append(f'{cn(name)}(ch{index})')
        if stale:
            notes.append(f'{len(stale)} 路映射与设备不一致，建议重新标定：' + '、'.join(stale[:3])
                         + ('…' if len(stale) > 3 else ''))
        self.set_status(f'{self.src.name}  {getattr(self.src, "path", "")}  {len(self.channels)} 路  '
                        f'{len(getattr(self.src, "keys", []))} 键'
                        + ('　|　' + '；'.join(notes) if notes else ''))
        self.mapping.data.setdefault('device', {})['match'] = match or ''

    def gimbal_sides(self):
        """哪个框取哪两路。默认按常规布局，配置里有 [gimbal] 就用配置的。"""
        cfg_sides = self.mapping.data.get('gimbal') or {'left': ['yaw', 'throttle'],
                                                       'right': ['roll', 'pitch']}
        return {k: tuple(v) for k, v in cfg_sides.items()}

    @staticmethod
    def _cap(name, value):
        short = {'throttle': 'thr', 'arm': 'arm'}
        return f'{short.get(name, name)} {("--" if value is None else format(value, "+.2f"))}'

    def _connect_from_box(self):
        txt = self.dev_var.get()
        if not txt:
            return
        path, name = txt.split('  ', 1)
        if path.startswith('/dev/hidraw'):
            self.mapping.data['device']['backend'] = 'hidraw'
            self.mapping.data['hidraw'] = self.mapping.data.get('hidraw', {}) or {'offset': 1, 'width': 2,
                                                                                 'endian': 'little', 'count': 8}
            self.mapping.data['device']['path'] = path
            self.connect('')
        else:
            self.mapping.data['device']['backend'] = 'evdev'
            self.mapping.data['device']['path'] = None
            self.connect(name.split('  ')[0])

    def _gimbal_signs(self, side):
        out = []
        for name in self.gimbal_sides()[side]:
            spec = self.mapping.funcs.get(name) or {}
            if spec.get('channel') is None:
                out.append(0)
            else:
                out.append(-1 if spec.get('invert') else 1)
        return out

    # ---------------------------------------------------------------- 发给游戏

    def _toggle_send(self, restart=False):
        if self.link:
            self.link.close()
            self.link = None
        if not self.send_var.get():
            self.link_label.config(text='')
            return
        try:
            self.link_port = int(self.port_var.get())
        except ValueError:
            self.send_var.set(False)
            self.set_status('端口必须为数字')
            return
        self.link = UdpLink(self.link_port)
        self.link.open()
        self.mapping.data.setdefault('link', {})['port'] = self.link_port
        self.set_status(f'开始向 127.0.0.1:{self.link_port} 发送状态包（游戏端绑定该端口）'
                        + ('；已重新启动' if restart else ''))

    def _send_tick(self, values):
        if not self.link:
            return
        self.last_send_at = time.monotonic()
        flags = 0
        stick = ('roll', 'pitch', 'yaw', 'throttle')
        if all((self.mapping.funcs.get(n) or {}).get('learned') for n in stick):
            flags |= ipc.ST_VALID
        if self.learn:
            flags |= ipc.ST_CALIB
        if values.get('arm') or self.arm_bypass.get():
            flags |= ipc.ST_ARMED
        if self.selftest_var.get():
            # 自检信号：不看遥控器，直接让相机慢慢摇。
            # 游戏里相机跟着动，就说明从这一头到自由镜头整条路都通了。
            t = time.monotonic()
            values = {'roll': .3 * math.sin(t * .9), 'pitch': .22 * math.sin(t * .55 + 1.0),
                      'yaw': 0.0, 'throttle': 0.0, 'arm': True,
                      'mode': False, 'buzzer': False, 'turtle': False}
            flags |= ipc.ST_VALID | ipc.ST_ARMED
        sw, aux = 0, 0
        for bit, name in enumerate(('arm', 'mode', 'buzzer', 'turtle')):
            v = values.get(name)
            if isinstance(v, bool):
                if v:
                    sw |= 1 << bit
            elif isinstance(v, int) and name == 'mode':
                # 模式开关是多档：档位号直接进 aux，游戏端以它为准
                aux = max(0, min(3, v))
                flags |= ipc.ST_MODE_SEL
        self.link.send_state(ipc.STICK_MAX * (values.get('roll') or 0.0),
                             ipc.STICK_MAX * (values.get('pitch') or 0.0),
                             ipc.STICK_MAX * (values.get('yaw') or 0.0),
                             ipc.THR_MAX * (values.get('throttle') or 0.0),
                             switches=sw, aux=aux, flags=flags)
        self.sent = getattr(self, 'sent', 0) + 1
        self.link.poll()

    # ---------------------------------------------------------------- 每帧

    def poll_input(self):
        """读掉积压的通道帧、更新行程、算映射、把状态包发出去。

        这条比界面刷新快得多（tick_fast 里跑）：界面 30 Hz 够看，遥控 30 Hz 不够飞。
        """
        if not self.src:
            return
        gain = 0
        while gain < 40:
            f = self.src.poll(0)
            if not f:
                break
            gain += 1
            self.frames += 1
            self.last = f.values
            for i, v in f.values.items():
                lo, hi = self.seen.get(i, (v, v))
                self.seen[i] = (min(lo, v), max(hi, v))
            self.last_buttons = f.buttons
        if not self.last:
            return
        values = self.mapping.apply(ch.Frame(time.monotonic(), self.last,
                                            getattr(self, 'last_buttons', 0)), self.channels)
        self.last_values = values
        self._send_tick(values)

    def tick_fast(self):
        try:
            self.poll_input()
        except Exception as err:                      # 输入循环出错不能把界面搞死
            self.set_status(f'输入循环出错：{err}')
        self.root.after(4, self.tick_fast)

    def tick(self):
        if self.src:
            for c in self.channels:
                v = self.last.get(c.index, c.value)
                self.bars[c.index].set(v, c.min, c.max, self.rest.get(c.index), self.seen[c.index])
                lo, hi = self.seen[c.index]
                self.vals[c.index].config(text=f'{v:>5}  {lo}~{hi}')

            for name, r in self.rows.items():
                spec = self.mapping.funcs.get(name) or {}
                index = spec.get('channel')
                mark = ''
                if index is not None:
                    meta = self.mapping.channel_meta(self.channels, index)
                    value = self.last.get(index)
                    center = spec.get('center')
                    if meta and value is not None and center is not None:
                        eps = (meta.max - meta.min) * 0.02
                        mark = '+' if value > center + eps else ('-' if value < center - eps else '')
                if r['sign_last'] != mark:
                    r['sign'].config(text=mark,
                                     fg=ACCENT if mark == '+' else (LOW if mark == '-' else DIM))
                    r['sign_last'] = mark

            values = getattr(self, 'last_values', None) or self.mapping.apply(
                ch.Frame(time.monotonic(), self.last, getattr(self, 'last_buttons', 0)), self.channels)
            if time.monotonic() - getattr(self, 'last_send_at', 0.0) > .05:   # 快速路没跑时兜底
                self._send_tick(values)
            for side, (hname, vname) in self.gimbal_sides().items():
                xv, yv = values.get(hname), values.get(vname)
                canvas, cap = self.gim[side]
                canvas.set(xv, None if yv is None else yv * 2 - 1 if vname in ch.STICK_FUNCS else yv)
                canvas.signs(*self._gimbal_signs(side))
                cap.config(text=self._cap(hname, xv) + chr(10) + self._cap(vname, yv))

        now = time.monotonic()
        if now - self.hz_t0 >= 1.0:
            span = now - self.hz_t0
            self.hz = self.frames / span
            self.send_hz = getattr(self, 'sent', 0) / span      # 真正发出去的包率
            self.frames, self.sent, self.hz_t0 = 0, 0, now
            # 摇杆不动时内核会把没变化的轴过滤掉，一帧都不来，这是正常的
            self.hz_label.config(text='静止' if self.hz < 0.5 else f'包率 {self.hz:.0f} Hz')
            if self.link:
                self.link_label.config(text=self.link.summary(),
                                       fg=FG if self.link.online() else DIM)
            self._ready_tick()

        if self.learn:
            self._learn_tick()

        self.root.after(33, self.tick)

    # ---------------------------------------------------------------- 映射操作

    def _chan_label(self, index):
        return f'ch{index}' if index is not None else '--'

    def _chan_index(self, label):
        return None if label in ('', '--') else int(label[2:])

    def _set_field(self, name, field):
        r = self.rows[name]
        spec = self.mapping.funcs.setdefault(name, {})
        if field == 'channel':
            spec['channel'] = self._chan_index(r['chan'].get())
            meta = self.mapping.channel_meta(self.channels, spec['channel'])
            if meta:
                spec.setdefault('min', meta.min)
                spec.setdefault('max', meta.max)
                spec.setdefault('center', (meta.min + meta.max) // 2)
        elif field == 'kind':
            spec['kind'] = kind_key(r['kind'].get())
            self._refresh_kind(name)
        elif field == 'invert':
            spec['invert'] = bool(r['invert'].get())
        self._refresh_range(name)

    def _read_entries(self):
        for name, r in self.rows.items():
            spec = self.mapping.funcs.get(name)
            if not spec:
                continue
            for key in ('expo', 'deadband', 'min', 'max'):
                txt = r[key].get().strip()
                if not txt:
                    continue
                try:
                    spec[key] = float(txt) if '.' in txt else int(txt)
                except ValueError:
                    r[key].set(str(spec.get(key, '')))
                    self.set_status(f'{cn(name)}的 {key} 不是数字，未修改')
            txt = r['center'].get().strip()
            if not txt:
                continue
            if spec.get('kind') == 'select' or ',' in txt:
                try:
                    spec['bands'] = [int(float(x)) for x in txt.split(',') if x.strip()]
                except ValueError:
                    self.set_status(f'{cn(name)} 的分界值需为数字，并以逗号分隔')
            else:
                try:
                    spec['center'] = int(float(txt))
                except ValueError:
                    r['center'].set(str(spec.get('center', '')))
                    self.set_status(f'{cn(name)} 的中位不是数字，未修改')
            self._read_map(name)

    def _refresh_kind(self, name):
        """多档类型才需要填档位顺序：档位序号从小到大对应的值。"""
        r = self.rows[name]
        spec = self.mapping.funcs.get(name) or {}
        if spec.get('kind') == 'select':
            n = len(spec.get('bands') or []) or 3
            r['map'].set(','.join(str(int(m)) for m in spec.get('map') or range(n)))
            r['map_w'].pack(side='left', padx=6)
        else:
            r['map_w'].pack_forget()

    def _read_map(self, name):
        spec = self.mapping.funcs.get(name)
        if not spec or spec.get('kind') != 'select':
            return
        txt = self.rows[name]['map'].get().strip().replace(' ', ',')
        if not txt:
            spec.pop('map', None)
            return
        try:
            spec['map'] = [int(float(x)) for x in txt.split(',') if x.strip()]
        except ValueError:
            self._refresh_kind(name)
            self.set_status(f'{cn(name)} 的档位顺序需为数字，并以逗号分隔')

    def _refresh_range(self, name):
        spec = self.mapping.funcs.get(name) or {}
        r = self.rows[name]
        if spec.get('channel') is None:
            for k in ('min', 'center', 'max'):
                r[k].set('')
            return
        r['chan'].set(f'ch{spec["channel"]}')
        r['kind'].set(kind_label(spec.get('kind', 'axis')))
        r['invert'].set(bool(spec.get('invert')))
        r['min'].set('' if spec.get('min') is None else str(spec['min']))
        r['max'].set('' if spec.get('max') is None else str(spec['max']))
        if spec.get('kind') == 'select':
            r['center'].set(','.join(str(int(b)) for b in spec.get('bands', [])))
        else:
            r['center'].set('' if spec.get('center') is None else str(int(spec['center'])))
        self._refresh_kind(name)
        self._paint_marks()

    def check_centers(self):
        """仅检查带弹簧回中的轴。油门静止于底部、开关静止于中间，均视为正常，不报警。"""
        axis_chans = {spec.get('channel') for spec in self.mapping.funcs.values()
                      if spec.get('kind', 'axis') == 'axis'}
        bad, skipped, checked = [], 0, 0
        for c in self.channels:
            if c.index not in axis_chans:
                skipped += 1
                continue
            checked += 1
            v = self.last.get(c.index, c.value)
            span = (c.max - c.min) or 1
            off = (v - (c.min + c.max) / 2) / span
            if abs(off) > 0.06:
                bad.append(f'ch{c.index} 偏 {off * 100:+.0f}%')
        if not checked:
            self.set_status('尚未映射任何回中轴，请先在右侧将横滚、俯仰、偏航绑定到通道')
        elif bad:
            self.set_status('未回中：' + '、'.join(bad) +
                            '  —— 请校准遥控器摇杆，或在映射中修改该路的中心值')
        else:
            self.set_status(f'{checked} 路回中轴均在中位附近，正常（其余 {skipped} 路为油门或开关，不回中属正常）')

    def save(self):
        self._read_entries()
        cfg.save(self.mapping, self.config_path)
        self.set_status(f'已保存到 {self.config_path}')

    def reload(self):
        self.mapping, _ = cfg.load(self.config_path)
        for name in LEARNABLE:
            self._refresh_range(name)
        self.set_status('已从磁盘重新载入')

    def _ready_tick(self):
        """用一行显示当前所处的步骤，以及游戏端未接管相机的原因。"""
        funcs = self.mapping.funcs
        stick = ('roll', 'pitch', 'yaw', 'throttle')
        missing = [n for n in stick if not (funcs.get(n) or {}).get('learned')]
        values = getattr(self, 'last_values', None) or {}
        armed = bool(values.get('arm')) or self.arm_bypass.get() or self.selftest_var.get()
        link = self.link

        def put(key, text, level):
            self.lamp[key].config(text=text, fg={'ok': ACCENT, 'warn': LOW,
                                                 'dim': DIM, 'mid': FG}[level])

        put('设备', '设备 ' + ('已连接' if self.src else '未连接'), 'ok' if self.src else 'warn')
        put('映射', '映射 完整' if not missing else '映射 缺少 ' + '、'.join(cn(n) for n in missing),
            'ok' if not missing else 'warn')
        put('解锁', '解锁 ' + ('已开' if armed else '未开'), 'ok' if armed else 'warn')
        sending = link is not None and self.link is link
        hz = getattr(self, 'send_hz', 0.0) if sending else 0.0
        put('发送', f'发送 {hz:.0f} Hz' if sending else '发送 关闭',
            'ok' if hz > 5 else ('warn' if sending else 'dim'))
        if link is None:
            put('游戏', '游戏 未发送', 'warn')
            put('控制权', '控制权 --', 'dim')
        else:
            running = bool(link.flags & ipc.SG_RUNNING) and link.online()
            taken = bool(link.flags & ipc.SG_TAKEOVER) and link.online()
            put('游戏', '游戏 ' + ('已响应' if running else '无响应'), 'ok' if running else 'warn')
            put('控制权', '控制权 ' + ('已取得' if taken else '未取得'), 'ok' if taken else 'mid')

        why = []
        if not self.src:
            why.append('请先连接遥控器')
        if missing:
            why.append('映射不完整，游戏端不会接管相机：请在右侧用「校准」逐项完成')
        if not armed and not missing:
            why.append('arm 开关未拨到高位（未标定时可勾选「忽略解锁开关」）')
        if link is not None and not (link.flags & ipc.SG_RUNNING and link.online()):
            why.append('游戏端无响应：请确认模组版本不低于 0.1.19、FreeCam.dll 已同步更新、端口一致，并先按 Ctrl+F6；'
                       '游戏内按 Ctrl+F9 可输出自检日志')
        elif link is not None and not (link.flags & ipc.SG_TAKEOVER and link.online()):
            why.append('游戏端已响应但未接管相机：请先按 Ctrl+F6 开启自由镜头，并检查以上各项')
        if self.selftest_var.get():
            why.append('自检模式已开启，遥控器输入不参与')
        self.lamp_why.config(text='；'.join(why))

    def set_status(self, text):
        self.status.config(text=text)

    # ---------------------------------------------------------------- 学习与校准

    def set_center(self):
        """将当前这一路的位置直接设为中位。请先将摇杆置于中位再点击。"""
        name = self.sel
        spec = self.mapping.funcs.get(name) or {}
        index = spec.get('channel')
        if index is None:
            self.set_status(f'{cn(name)}尚未绑定通道，请先选择通道或点击「校准」')
            return
        v = int(self.last.get(index, 0))
        spec['center'] = v
        spec['learned'] = True
        self._refresh_range(name)
        self.set_status(f'{cn(name)}中位 = ch{index} 当前值 {v}（请记得保存）')

    ZERO_HOLD = 3.0        # 零位检测：轴不动这么久就算停住了

    def start_sweep(self, target=None):
        """自动标定行程：先做零位检测（各摇杆静止 3 秒即取当前位置为中位），再推满行程取 min/max。"""
        if not self.src:
            return
        if target:
            who = [target]
            if (self.mapping.funcs.get(target) or {}).get('kind') in ('switch', 'select'):
                self.set_status(f'{cn(target)}为开关或多档类型，请使用该行的「校准」逐档标定；行程标定仅适用于轴与油门')
                return
        else:
            who = [n for n in LEARNABLE
                   if self.mapping.funcs.get(n, {}).get('kind', 'axis') in ('axis', 'range')]
        bound = [n for n in who if (self.mapping.funcs.get(n) or {}).get('channel') is not None]
        if not bound:
            self.set_status(f'{cn(target) if target else "所有轴"}尚未绑定通道，请先使用该行的「校准」')
            return
        who = bound
        self.learn = {'mode': 'sweep', 'name': target or '全部轴', 'targets': who,
                      'stage': 'zero', 'zero': {}, 'still': {}, 'last': {}, 'acc': {},
                      'sweep_dur': 6.0 if target else 9.0, 't': time.monotonic()}
        self.set_status(f'零位检测（{cn(target) if target else "全部轴"}）：请将所有摇杆置于中位并保持静止 {self.ZERO_HOLD:.0f} 秒，此时位置将作为中位')

    def _sweep_tick(self, L):
        now = time.monotonic()
        if L['stage'] == 'zero':
            for name in L['targets']:
                index = (self.mapping.funcs.get(name) or {}).get('channel')
                if index is None:
                    continue
                meta = self.mapping.channel_meta(self.channels, index)
                value = self.last.get(index)
                if meta is None or value is None:
                    continue
                eps = max(4.0, (meta.max - meta.min) * 0.012)
                prev = L['last'].get(index)
                if prev is None or abs(value - prev) > eps:
                    L['still'][index] = now
                L['last'][index] = value
            if not L['still']:
                self.set_status(f'零位检测：未收到数据，请检查设备连接是否正常')
                return
            quiet = min(now - t for t in L['still'].values())
            if quiet < self.ZERO_HOLD:
                self.set_status(f'零位检测 {cn(L["name"])}：请松开摇杆保持静止，已静止 {quiet:.1f} / {self.ZERO_HOLD:.0f} 秒'
                                f'（当前位置将作为中位）')
                return
            for index in L['still']:
                L['zero'][index] = self.last[index]
            L['stage'] = 'sweep'
            L['t'] = now
            zeros = '、'.join(f'ch{i}={v}' for i, v in sorted(L['zero'].items()))
            self.set_status(f'中位已记录：{zeros}。请开始推满行程，'
                            f'{L["sweep_dur"]:.0f} 秒内将每个摇杆推到两端，并画几个整圈')
            return
        for i, v in self.last.items():
            a = L['acc'].setdefault(i, [v, v])
            a[0], a[1] = min(a[0], v), max(a[1], v)
        left = L['sweep_dur'] - (now - L['t'])
        if left > 0:
            self.set_status(f'行程采样 {cn(L["name"])}：请将摇杆推到各方向极限，剩余 {left:.1f} 秒')
            return
        self.finish_sweep(L)

    def start_learn(self, name):
        if not self.src:
            return
        self._select(name)
        spec = self.mapping.funcs.get(name) or {}
        kind = spec.get('kind') or ('switch' if name in ch.SWITCH_FUNCS
                                    else ('range' if name in ch.STICK_FUNCS else 'axis'))
        if kind == 'select':
            n = max(2, min(8, len(spec.get('bands') or []) or 3))
            self.learn = {'mode': 'learn', 'name': name, 'kind': 'select', 'stage': 0,
                          'steps': [(f'档位 {i + 1}', 2.0, {}) for i in range(n)],
                          't': time.monotonic(), 'acc': {}}
            self.set_status(f'{cn(name)}（{n} 档）逐档标定：请把拨杆拨到「档位 1」并保持不动（2.0 秒）；'
                            '逐档走完后，档位序号按原始值从小到大排，顺序对不对看「档位顺序」那栏')
            return
        first = '最大' if kind == 'range' else ('关位' if kind == 'switch' else '正方向')
        second = '最小' if kind == 'range' else ('开位' if kind == 'switch' else '反方向')
        self.learn = {'mode': 'learn', 'name': name, 'kind': kind, 'stage': 0, 'steps': [
            (first, 2.5, {}), (second, 2.5, {})], 't': time.monotonic(), 'acc': {}}
        self.set_status(f'校准{cn(name)}：请将它推到「{first}」并保持不动（2.5 秒）')

    def _learn_tick(self):
        L = self.learn
        if L['mode'] == 'sweep':
            self._sweep_tick(L)
            return
        label, dur, _ = L['steps'][L['stage']]
        for i, v in self.last.items():
            a = L['acc'].setdefault(i, [v, v, 0.0, 0])
            a[0], a[1], a[2], a[3] = min(a[0], v), max(a[1], v), a[2] + v, a[3] + 1
        left = dur - (time.monotonic() - L['t'])
        if left > 0:
            self.set_status(f'校准 {cn(L["name"])}：{label} 采样中，剩余 {left:.1f} 秒')
            return
        L['steps'][L['stage']] = (label, dur, dict(L['acc']))
        L['acc'] = {}
        L['stage'] += 1
        if L['stage'] < len(L['steps']):
            nxt = L['steps'][L['stage']][0]
            L['t'] = time.monotonic()
            self.set_status(f'校准 {cn(L["name"])}：请切换到「{nxt}」并保持不动'
                            f'（{L["steps"][L["stage"]][1]:.0f} 秒）')
            return
        self.finish_learn(L)

    def finish_sweep(self, L):
        sweep, zero = L['acc'], L['zero']
        done, skipped = [], []
        for name in L['targets']:
            spec = self.mapping.funcs.setdefault(name, {})
            index = spec.get('channel')
            if index is None or index not in sweep:
                skipped.append(name)
                continue
            lo, hi = int(sweep[index][0]), int(sweep[index][1])
            if hi - lo < 4:
                skipped.append(f'{cn(name)}(未动作)')
                continue
            spec['min'], spec['max'] = lo, hi
            note = ''
            if spec.get('kind', 'axis') == 'axis' and index in zero:
                spec['center'] = int(round(zero[index]))
                note = f' 中位 {spec["center"]}'
            spec['learned'] = True
            self._refresh_range(name)
            done.append(f'{cn(name)} ch{index} {lo}..{hi}{note}')
        self.learn = None
        head = '已标定 ' + '；'.join(done) if done else '未检测到任何动作'
        tail = ('；已跳过：' + '、'.join(skipped)) if skipped else ''
        self.set_status(head + tail + '（请记得保存）')

    def finish_learn(self, L):
        accs = [s[2] for s in L['steps']]
        lo_all = {i: min(s[i][0] for s in accs if i in s) for i in accs[0]}
        hi_all = {i: max(s[i][1] for s in accs if i in s) for i in accs[0]}
        pick = max(lo_all, key=lambda i: hi_all[i] - lo_all[i])
        d = hi_all[pick] - lo_all[pick]
        min_move = max(4, int(max((c.max - c.min for c in self.channels), default=2047) * 0.1))
        if d < min_move:
            self.set_status(f'{cn(L["name"])} 校准失败：未检测到动作（最大变化量 {d}）')
            self.learn = None
            return
        means = [_mean(s[pick]) if pick in s else None for s in accs]
        mean_a = means[0]
        mean_b = means[1] if len(means) > 1 else means[0]
        lo, hi = lo_all[pick], hi_all[pick]
        spec = self.mapping.funcs.setdefault(L['name'], {})
        if L['kind'] == 'select':
            # 档位序号按原始值从小到大：分界值取相邻两档的中点
            order = sorted(range(len(means)), key=lambda k: means[k])
            bands = [int(round(lo))]
            bands += [int(round((means[a] + means[b]) / 2))
                      for a, b in zip(order, order[1:])]
            spec.update({'channel': pick, 'kind': 'select', 'min': int(lo), 'max': int(hi),
                         'bands': bands, 'invert': False, 'learned': True})
            spec.setdefault('map', list(range(len(bands))))
            self._refresh_range(L['name'])
            self.set_status(f'{cn(L["name"])} = ch{pick}  {len(bands)} 档：'
                            + '、'.join(f'档位{i + 1}({int(means[k])})' for i, k in enumerate(order))
                            + '；档位序号按原始值从小到大排，顺序不对就改「档位顺序」（请记得保存）')
            self.learn = None
            return
        if L['kind'] == 'switch':
            spec.update({'channel': pick, 'kind': 'switch', 'min': lo, 'max': hi,
                         'invert': mean_b < mean_a, 'learned': True})
        else:
            spec.update({'channel': pick, 'kind': L['kind'], 'min': lo, 'max': hi,
                         'center': int(round((mean_a + mean_b) / 2)),
                         'invert': False if mean_a > mean_b else L['kind'] == 'axis',
                         'learned': True})
            spec.setdefault('expo', 0.0)
            spec.setdefault('deadband', 0.01)
        self._refresh_range(L['name'])
        self.set_status(f'{cn(L["name"])} = ch{pick}  {lo}..{hi}  '
                        f'{"反向" if spec.get("invert") else "正向"}（请记得保存）')
        self.learn = None


    def apply_cli(self, port=None, wiggle=False, arm_bypass=False):
        """命令行启动时直接开启「发送到游戏」，无需在界面中手动勾选。"""
        if port:
            self.port_var.set(str(port))
        if arm_bypass:
            self.arm_bypass.set(True)
        if wiggle:
            self.selftest_var.set(True)
        self.send_var.set(True)
        self._toggle_send()


def selftest(config_path):
    mapping, _ = cfg.load(config_path)
    src = ch.open_source(dict(mapping.data.get('device', {}), hidraw=mapping.data.get('hidraw', {})))
    print(src.describe())
    t0, n = time.monotonic(), 0
    while time.monotonic() - t0 < 2.0:
        if src.poll(0.2):
            n += 1
    print(f'2 秒内收到 {n} 帧，约 {n / 2:.0f} Hz')
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=cfg.DEFAULT_PATH)
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--send', action='store_true', help='启动后立即向游戏发送状态包（游戏端绑定该端口）')
    ap.add_argument('--port', type=int, default=None, help='配合 --send 使用，默认为配置中的端口')
    ap.add_argument('--wiggle', action='store_true', help='发送自检信号：不读取遥控器，令相机缓慢摆动')
    ap.add_argument('--arm-bypass', action='store_true', help='未标定 arm 开关时也视为已解锁')
    args = ap.parse_args()
    if args.selftest:
        return selftest(args.config)
    root = tk.Tk()
    try:
        _icon = tk.PhotoImage(file=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'fpvhelper', 'icon.png'))
        root.iconphoto(True, _icon)
        root._icon = _icon  # 保持引用，否则被垃圾回收后图标会变回默认
    except Exception:
        pass
    app = App(root, args.config)
    if args.send or args.port or args.wiggle:
        app.apply_cli(args.port, wiggle=args.wiggle, arm_bypass=args.arm_bypass)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
