#!/usr/bin/env python3
"""查看遥控器当前上报的通道值。不作任何假设：设备有多少路就显示多少路。

  ./fpv_monitor.py --list              列出可识别的设备
  ./fpv_monitor.py --seconds 5         实时柱状图，默认 10 秒
  ./fpv_monitor.py --dump              仅输出一次通道清单和原始值
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpvhelper import channels as ch  # noqa: E402
from fpvhelper import config as cfg  # noqa: E402

RESET, CLEAR = '\033[0m', '\033[H\033[J'


def bar(pos, lo, hi, width=40, mark=None):
    span = (hi - lo) or 1
    n = int(round((pos - lo) / span * (width - 1)))
    s = ['-'] * width
    if mark is not None:
        s[int(round((mark - lo) / span * (width - 1)))] = '|'
    s[max(0, min(width - 1, n))] = '#'
    return ''.join(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--backend', default='evdev', choices=('evdev', 'hidraw'))
    ap.add_argument('--match', default='')
    ap.add_argument('--path', default=None)
    ap.add_argument('--seconds', type=float, default=10.0)
    ap.add_argument('--dump', action='store_true')
    ap.add_argument('--config', default=cfg.DEFAULT_PATH)
    args = ap.parse_args()

    if args.list:
        for path, name, axes in ch.list_devices():
            print(f'{path:<18} {name}')
            if axes:
                print(f'{"":18} 轴: {", ".join(axes)}')
        return

    mapping, path = cfg.load(args.config)
    if os.path.exists(args.config):
        dev = mapping.data.get('device', {})
        args.backend = args.backend if args.backend != 'evdev' else dev.get('backend', args.backend)
        args.match = args.match or dev.get('match', '')

    src = ch.open_source(dict(mapping.data.get('device', {}), backend=args.backend,
                              match=args.match, hidraw=mapping.data.get('hidraw', {})))
    print(src.describe())
    if args.dump:
        return

    order = [c.index for c in src.channels]
    lo_hi = {c.index: (c.min, c.max) for c in src.channels}
    seen = {i: (src.channels[i].value, src.channels[i].value) for i in order}
    last = {}
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < args.seconds:
            f = src.poll(0.2)
            if f:
                last = f.values
            for i in order:
                v = last.get(i, src.channels[i].value)
                lo, hi = seen[i]
                seen[i] = (min(lo, v), max(hi, v))
            sys.stdout.write(CLEAR)
            print(f'mapping: {path}   后端 {src.name}   剩 {args.seconds - (time.monotonic() - t0):4.1f}s')
            print('  通道        当前值   本次最小~最大       位置')
            for c in src.channels:
                v = last.get(c.index, c.value)
                lo, hi = seen[c.index]
                print(f'  ch{c.index} {c.label:<13} {v:>6}  {lo:>5}~{hi:<5} {bar(v, c.min, c.max)}')
            if f:
                print(f'  按键位图 0x{f.buttons:04x}   包率 {f.t:.3f}')
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    print(CLEAR)
    print('本次活动范围：')
    for c in src.channels:
        lo, hi = seen[c.index]
        delta = hi - lo
        flag = '  <- 动过' if delta > (c.max - c.min) * 0.05 else ''
        print(f'  ch{c.index} {c.label:<13} {lo}~{hi}  幅度 {delta}{flag}')


if __name__ == '__main__':
    main()
