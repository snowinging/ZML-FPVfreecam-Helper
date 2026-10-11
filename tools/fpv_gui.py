#!/usr/bin/env python3
"""遥控器配置台：左边实时看通道，右边改映射，学习直接在里面学。

  ./fpv_gui.py                    用 ~/.config/endfield-fpv/mapping.toml
  ./fpv_gui.py --config /tmp/x.toml
  ./fpv_gui.py --selftest         不开窗口，只验证输入层能连上（无显示环境时用）
  ./fpv_gui.py --send --port 47800   起动就开始往游戏发状态包（不用手点勾选框）

界面只干三件事：看设备报什么、把通道绑到功能上、存成 TOML。没别的。
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

DEFAULT_RATES = {'roll': 400.0, 'pitch': 400.0, 'yaw': 500.0}

BG = '#14161a'
PANEL = '#1b1e24'
FG = '#d7dce2'
DIM = '#7d8794'
ACCENT = '#c8a06a'
LOW = '#7fa0c0'
LINE = '#2a2f37'
MONO = ('DejaVu Sans Mono', 9)

LEARNABLE = ('roll', 'pitch', 'yaw', 'throttle', 'arm', 'mode', 'buzzer', 'turtle')


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

        tk.Label(top, text='发给游戏', bg=BG, fg=DIM, font=MONO).pack(side='left', padx=(22, 6))
        self.send_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, variable=self.send_var, command=self._toggle_send).pack(side='left')
        tk.Label(top, text='端口', bg=BG, fg=DIM, font=MONO).pack(side='left', padx=(10, 4))
        self.port_var = tk.StringVar(value=str(self.link_port))
        port_entry = tk.Entry(top, textvariable=self.port_var, width=7, bg=PANEL, fg=FG,
                              insertbackground=FG, relief='flat', font=MONO)
        port_entry.pack(side='left')
        port_entry.bind('<Return>', lambda e: self._toggle_send(restart=True))
        self.arm_bypass = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text='免 arm 开关', variable=self.arm_bypass).pack(side='left', padx=(12, 0))
        self.selftest_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text='自检信号', variable=self.selftest_var).pack(side='left', padx=(10, 0))
        self.link_label = tk.Label(top, text='', bg=BG, fg=DIM, font=('DejaVu Sans Mono', 8))
        self.link_label.pack(side='right', padx=(0, 14))
        self.hz_label = tk.Label(top, text='--', bg=BG, fg=DIM, font=MONO)
        self.hz_label.pack(side='right')

        # 左：通道
        left = tk.Frame(root, bg=BG)
        left.grid(row=1, column=0, sticky='nsw', padx=(14, 8), pady=4)
        tk.Label(left, text='通道（设备报什么就是什么）', bg=BG, fg=DIM, font=MONO).pack(anchor='w')
        self.chan_box = tk.Frame(left, bg=BG)
        self.chan_box.pack(anchor='w', pady=(8, 0))

        # 左下方：两个摇杆框
        tk.Label(left, text='摇杆（取映射里的通道）', bg=BG, fg=DIM, font=MONO).pack(
            anchor='w', pady=(14, 0))
        gim_row = tk.Frame(left, bg=BG)
        gim_row.pack(anchor='w', pady=(8, 0))
        self.gim = {}
        for side, title in (('left', '左'), ('right', '右')):
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
        tk.Label(right, text='映射（存到 mapping.toml）', bg=BG, fg=DIM, font=MONO).pack(anchor='w')
        self.map_box = tk.Frame(right, bg=BG)
        self.map_box.pack(anchor='w', pady=(8, 0))
        head = tk.Frame(self.map_box, bg=BG)
        head.pack(anchor='w')
        for txt, w in (('功能', 9), ('通道', 10), ('类型', 7), ('反向', 5), ('expo', 6),
                       ('死区', 6), ('最小 -', 8), ('中点', 7), ('最大 +', 8), ('', 6)):
            tk.Label(head, text=txt, bg=BG, fg=DIM, font=MONO, width=w, anchor='w').pack(side='left')
        for name in LEARNABLE:
            self._add_row(name)
        tk.Label(right, text='最小-=反方向推到底的原始值，最大+=正方向推到底的；推反了就勾反向。'
                             '中点=杆回中的值（油门这种不回中的用不上，select 类型在这儿填分界值，逗号隔开）。三栏都能直接改\n'
                             '游戏里：arm 拨上来才接管；mode 拨上去=纯速率、拨下来=自稳（松杆回平）\n'
                             'buzzer 按住=强制回平；turtle 按住=翻正（收平并切油门）',
                 bg=BG, fg=DIM, font=('DejaVu Sans Mono', 8), justify='left').pack(anchor='w', pady=(8, 0))

        # 底栏
        bottom = tk.Frame(root, bg=BG)
        # 就绪检查：一行说清"现在卡在哪"，游戏会不会接管、为什么不接管
        ready = tk.Frame(holder, bg=BG)
        ready.grid(row=1, column=0, sticky='ew', pady=(8, 0))
        self.lamp = {}
        for key in ('设备', '映射', '解锁', '发送', '游戏', '接管'):
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
        ttk.Button(bottom, text='设中点', width=7,
                   command=self.set_center).pack(side='left', padx=6)
        ttk.Button(bottom, text='自动行程', width=9,
                   command=lambda: self.start_sweep(self.sel)).pack(side='left')
        ttk.Button(bottom, text='全部轴自动行程', width=15,
                   command=lambda: self.start_sweep(None)).pack(side='left', padx=6)
        ttk.Button(bottom, text='归中自检', width=9,
                   command=self.check_centers).pack(side='left')
        self.status = tk.Label(bottom, text='', bg=BG, fg=FG, font=MONO, anchor='w')
        self.status.pack(side='left', padx=12, fill='x', expand=True)
        if self.sel:
            self.sel_label.config(text=f'选中 {self.sel}')

    def _add_row(self, name):
        row = tk.Frame(self.map_box, bg=BG)
        row.pack(anchor='w', pady=1)
        spec = self.mapping.funcs.get(name, {})

        mark = tk.Frame(row, bg=LINE, width=3, height=20)
        mark.pack(side='left', padx=(0, 6))
        tk.Label(row, text=name, bg=BG, fg=FG, font=MONO, width=9, anchor='w').pack(side='left')

        cvar = tk.StringVar(value=self._chan_label(spec.get('channel')))
        cbox = ttk.Combobox(row, textvariable=cvar, width=9, state='readonly')
        cbox['values'] = ['--'] + [f'ch{c.index}' for c in self.channels] or ['--']
        cbox.pack(side='left')
        cbox.bind('<<ComboboxSelected>>', lambda e, n=name: self._set_field(n, 'channel'))

        kvar = tk.StringVar(value=spec.get('kind', 'axis' if name not in ch.SWITCH_FUNCS else 'switch'))
        kbox = ttk.Combobox(row, textvariable=kvar, width=6, state='readonly')
        kbox['values'] = ['axis', 'range', 'switch', 'select']
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

        self.rows[name] = {'chan': cvar, 'chan_w': cbox, 'kind': kvar, 'invert': ivar,
                           'expo': fields['expo'], 'deadband': fields['deadband'],
                           'min': fields['min'], 'center': fields['center'], 'max': fields['max'],
                           'min_w': fields['min_w'], 'center_w': fields['center_w'],
                           'max_w': fields['max_w'], 'mark': mark, 'row': row,
                           'sign': sign, 'sign_last': None}
        for widget in (row, mark):
            widget.bind('<Button-1>', lambda e, n=name: self._select(n))
        if self.sel is None:
            self.sel = name
            mark.config(bg=ACCENT)
        self._refresh_range(name)

    def _select(self, name):
        if name not in self.rows:
            return
        self.sel = name
        if hasattr(self, 'sel_label'):
            self.sel_label.config(text=f'选中 {name}')
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
                stale.append(f'{name}(ch{index})')
        if stale:
            notes.append(f'{len(stale)} 路映射和设备对不上，建议重标：' + '、'.join(stale[:3])
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

    def rates(self):
        got = self.mapping.data.get('rates') or {}
        return {k: float(got.get(k, v)) for k, v in DEFAULT_RATES.items()}

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
            self.set_status('端口得是数字')
            return
        self.link = UdpLink(self.link_port)
        self.link.open()
        self.mapping.data.setdefault('link', {})['port'] = self.link_port
        self.set_status(f'开始往 127.0.0.1:{self.link_port} 发状态包（游戏侧 bind 这个端口）'
                        + ('；重新开了' if restart else ''))

    def _send_tick(self, values):
        if not self.link:
            return
        self.last_send_at = time.monotonic()
        rate = self.rates()
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
        sw = 0
        for bit, name in enumerate(('arm', 'mode', 'buzzer', 'turtle')):
            if values.get(name):
                sw |= 1 << bit
        self.link.send_state(rate['roll'] * (values.get('roll') or 0.0),
                             rate['pitch'] * (values.get('pitch') or 0.0),
                             rate['yaw'] * (values.get('yaw') or 0.0),
                             ipc.THR_MAX * (values.get('throttle') or 0.0),
                             switches=sw, flags=flags)
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
            spec['kind'] = r['kind'].get()
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
                    self.set_status(f'{name} 的 {key} 不是数字，没改')
            txt = r['center'].get().strip()
            if not txt:
                continue
            if spec.get('kind') == 'select' or ',' in txt:
                try:
                    spec['bands'] = [int(float(x)) for x in txt.split(',') if x.strip()]
                except ValueError:
                    self.set_status(f'{name} 的分界值要一串数字用逗号隔开')
            else:
                try:
                    spec['center'] = int(float(txt))
                except ValueError:
                    r['center'].set(str(spec.get('center', '')))
                    self.set_status(f'{name} 的中点不是数字，没改')

    def _refresh_range(self, name):
        spec = self.mapping.funcs.get(name) or {}
        r = self.rows[name]
        if spec.get('channel') is None:
            for k in ('min', 'center', 'max'):
                r[k].set('')
            return
        r['chan'].set(f'ch{spec["channel"]}')
        r['kind'].set(spec.get('kind', 'axis'))
        r['invert'].set(bool(spec.get('invert')))
        r['min'].set('' if spec.get('min') is None else str(spec['min']))
        r['max'].set('' if spec.get('max') is None else str(spec['max']))
        if spec.get('kind') == 'select':
            r['center'].set(','.join(str(int(b)) for b in spec.get('bands', [])))
        else:
            r['center'].set('' if spec.get('center') is None else str(int(spec['center'])))
        self._paint_marks()

    def check_centers(self):
        """只看弹簧回中的轴。油门静止在底部、开关不动在中间，都算正常，不报警。"""
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
            self.set_status('还没映射任何回中轴，右边先把 roll/pitch/yaw 绑上通道')
        elif bad:
            self.set_status('没归中：' + '、'.join(bad) +
                            '  —— 要么修遥控器的摇杆校准，要么在映射里改那一路的 center')
        else:
            self.set_status(f'{checked} 个回中轴都在中点附近，正常（其余 {skipped} 路是油门/开关，本来就不居中）')

    def save(self):
        self._read_entries()
        cfg.save(self.mapping, self.config_path)
        self.set_status(f'已写入 {self.config_path}')

    def reload(self):
        self.mapping, _ = cfg.load(self.config_path)
        for name in LEARNABLE:
            self._refresh_range(name)
        self.set_status('已从磁盘重载')

    def _ready_tick(self):
        """一行说清"现在到哪一步了"，以及游戏为什么不接管。"""
        funcs = self.mapping.funcs
        stick = ('roll', 'pitch', 'yaw', 'throttle')
        missing = [n for n in stick if not (funcs.get(n) or {}).get('learned')]
        values = getattr(self, 'last_values', None) or {}
        armed = bool(values.get('arm')) or self.arm_bypass.get() or self.selftest_var.get()
        link = self.link

        def put(key, text, level):
            self.lamp[key].config(text=text, fg={'ok': ACCENT, 'warn': LOW,
                                                 'dim': DIM, 'mid': FG}[level])

        put('设备', '设备 ' + ('已连' if self.src else '没连'), 'ok' if self.src else 'warn')
        put('映射', '映射 齐' if not missing else '映射 缺' + '/'.join(missing),
            'ok' if not missing else 'warn')
        put('解锁', '解锁 ' + ('开' if armed else '关'), 'ok' if armed else 'warn')
        sending = link is not None and self.link is link
        hz = getattr(self, 'send_hz', 0.0) if sending else 0.0
        put('发送', f'发送 {hz:.0f}Hz' if sending else '发送 关',
            'ok' if hz > 5 else ('warn' if sending else 'dim'))
        if link is None:
            put('游戏', '游戏 没发', 'warn')
            put('接管', '接管 --', 'dim')
        else:
            running = bool(link.flags & ipc.SG_RUNNING) and link.online()
            taken = bool(link.flags & ipc.SG_TAKEOVER) and link.online()
            put('游戏', '游戏 ' + ('在跑' if running else '没回话'), 'ok' if running else 'warn')
            put('接管', '接管 ' + ('中' if taken else '没接管'), 'ok' if taken else 'mid')

        why = []
        if not self.src:
            why.append('先把遥控器连上')
        if missing:
            why.append('映射没标全，游戏一定不会接管：用右边的「校准」逐路认一遍')
        if not armed and not missing:
            why.append('arm 开关没推上来（没标过就勾「免 arm 开关」）')
        if link is not None and not (link.flags & ipc.SG_RUNNING and link.online()):
            why.append('游戏侧没回话：模组得是 0.1.18 那份、FreeCam.dll 要一起换、端口两边一样、'
                       '自由镜头要按 Ctrl+F6 先开')
        elif link is not None and not (link.flags & ipc.SG_TAKEOVER and link.online()):
            why.append('接收端在跑但没接管相机：先按 Ctrl+F6 开自由镜头，再看上面那几项')
        if self.selftest_var.get():
            why.append('自检信号开着，遥控器不参与')
        self.lamp_why.config(text='；'.join(why))

    def set_status(self, text):
        self.status.config(text=text)

    # ---------------------------------------------------------------- 学习与校准

    def set_center(self):
        """把当前这一路的位置直接当中点。杆放平了再点。"""
        name = self.sel
        spec = self.mapping.funcs.get(name) or {}
        index = spec.get('channel')
        if index is None:
            self.set_status(f'{name} 还没绑通道，先选通道或点「学」')
            return
        v = int(self.last.get(index, 0))
        spec['center'] = v
        spec['learned'] = True
        self._refresh_range(name)
        self.set_status(f'{name} 中点 = ch{index} 现在的 {v}  记得保存')

    ZERO_HOLD = 3.0        # 零位检测：轴不动这么久就算停住了

    def start_sweep(self, target=None):
        """自动行程：先零位检测（杆都不动 3 秒 → 当下就是中心），再推满行程取 min/max。"""
        if not self.src:
            return
        if target:
            who = [target]
            if (self.mapping.funcs.get(target) or {}).get('kind') == 'switch':
                self.set_status(f'{target} 是开关，用那行的「校准」逐位认；行程标定只管轴和油门')
                return
        else:
            who = [n for n in LEARNABLE
                   if self.mapping.funcs.get(n, {}).get('kind', 'axis') in ('axis', 'range')]
        bound = [n for n in who if (self.mapping.funcs.get(n) or {}).get('channel') is not None]
        if not bound:
            self.set_status(f'{target or "所有轴"}还没绑通道，先用那行的「校准」认一遍')
            return
        who = bound
        self.learn = {'mode': 'sweep', 'name': target or '全部轴', 'targets': who,
                      'stage': 'zero', 'zero': {}, 'still': {}, 'last': {}, 'acc': {},
                      'sweep_dur': 6.0 if target else 9.0, 't': time.monotonic()}
        self.set_status(f'零位检测（{target or "全部轴"}）：把杆都松手放回中位，别碰，静止 {self.ZERO_HOLD:.0f} 秒就算中心')

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
                self.set_status(f'零位检测：等不到数据，检查一下设备还连着没')
                return
            quiet = min(now - t for t in L['still'].values())
            if quiet < self.ZERO_HOLD:
                self.set_status(f'零位检测 {L["name"]}：松开杆别动，静止 {quiet:.1f} / {self.ZERO_HOLD:.0f} 秒'
                                f'（现在的位置就当中点）')
                return
            for index in L['still']:
                L['zero'][index] = self.last[index]
            L['stage'] = 'sweep'
            L['t'] = now
            zeros = '、'.join(f'ch{i}={v}' for i, v in sorted(L['zero'].items()))
            self.set_status(f'中心记下了：{zeros}。现在推满行程，'
                            f'{L["sweep_dur"]:.0f} 秒内把每个杆都推到两头、画几个圈')
            return
        for i, v in self.last.items():
            a = L['acc'].setdefault(i, [v, v])
            a[0], a[1] = min(a[0], v), max(a[1], v)
        left = L['sweep_dur'] - (now - L['t'])
        if left > 0:
            self.set_status(f'行程采样 {L["name"]}：推满每个方向，剩 {left:.1f}s')
            return
        self.finish_sweep(L)

    def start_learn(self, name):
        if not self.src:
            return
        self._select(name)
        kind = 'switch' if name in ch.SWITCH_FUNCS else ('range' if name in ch.STICK_FUNCS else 'axis')
        first = '最大' if kind == 'range' else ('关位' if kind == 'switch' else '正方向')
        second = '最小' if kind == 'range' else ('开位' if kind == 'switch' else '反方向')
        self.learn = {'mode': 'learn', 'name': name, 'kind': kind, 'stage': 0, 'steps': [
            (first, 2.5, {}), (second, 2.5, {})], 't': time.monotonic(), 'acc': {}}
        self.set_status(f'校准 {name}：现在把它推到「{first}」，保持不动（2.5 秒）')

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
            self.set_status(f'校准 {L["name"]}：{label} 采样中 {left:.1f}s')
            return
        L['steps'][L['stage']] = (label, dur, dict(L['acc']))
        L['acc'] = {}
        L['stage'] += 1
        if L['stage'] < len(L['steps']):
            nxt = L['steps'][L['stage']][0]
            L['t'] = time.monotonic()
            self.set_status(f'校准 {L["name"]}：换到「{nxt}」，保持不动（{L["steps"][L["stage"]][1]:.0f} 秒）')
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
                skipped.append(f'{name}(没动)')
                continue
            spec['min'], spec['max'] = lo, hi
            note = ''
            if spec.get('kind', 'axis') == 'axis' and index in zero:
                spec['center'] = int(round(zero[index]))
                note = f' 中点 {spec["center"]}'
            spec['learned'] = True
            self._refresh_range(name)
            done.append(f'{name} ch{index} {lo}..{hi}{note}')
        self.learn = None
        head = '标好了 ' + '；'.join(done) if done else '一个都没动'
        tail = ('；跳过的：' + '、'.join(skipped)) if skipped else ''
        self.set_status(head + tail + '  记得保存')

    def finish_learn(self, L):
        a = L['steps'][0][2]
        b = L['steps'][1][2]
        sp_a = {i: s[1] - s[0] for i, s in a.items()}
        sp_b = {i: s[1] - s[0] for i, s in b.items()}
        pick = max(sp_a, key=lambda i: max(sp_a[i], sp_b.get(i, 0)))
        d = max(sp_a[pick], sp_b.get(pick, 0))
        min_move = max(4, int(max((c.max - c.min for c in self.channels), default=2047) * 0.1))
        if d < min_move:
            self.set_status(f'{L["name"]} 没校准成：没看出你在动（最大变化 {d}）')
            self.learn = None
            return
        mean_a, mean_b = _mean(a[pick]), _mean(b[pick])
        lo = min(a[pick][0], b[pick][0])
        hi = max(a[pick][1], b[pick][1])
        spec = self.mapping.funcs.setdefault(L['name'], {})
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
        self.set_status(f'{L["name"]} = ch{pick}  {lo}..{hi}  '
                        f'{"反向" if spec.get("invert") else "正向"}  记得点保存')
        self.learn = None


    def apply_cli(self, port=None, wiggle=False, arm_bypass=False):
        """命令行起动时直接把「发给游戏」打开——不用在别人桌面上点勾选框。"""
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
    print(f'2 秒收到 {n} 帧，约 {n / 2:.0f} Hz')
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=cfg.DEFAULT_PATH)
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--send', action='store_true', help='起动就把状态包发给游戏（游戏侧 bind 这个端口）')
    ap.add_argument('--port', type=int, default=None, help='配合 --send，默认用配置里的端口')
    ap.add_argument('--wiggle', action='store_true', help='发自检信号：不看遥控器，让相机自己慢慢摇')
    ap.add_argument('--arm-bypass', action='store_true', help='没标过 arm 开关也算已解锁')
    args = ap.parse_args()
    if args.selftest:
        return selftest(args.config)
    root = tk.Tk()
    app = App(root, args.config)
    if args.send or args.port or args.wiggle:
        app.apply_cli(args.port, wiggle=args.wiggle, arm_bypass=args.arm_bypass)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
