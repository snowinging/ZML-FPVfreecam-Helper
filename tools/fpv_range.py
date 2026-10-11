#!/usr/bin/env python3
"""量程检查：采样一小段时间，查看各轴实际可达范围，并与设备声明的 min/max 比较。

  ./fpv_range.py           按提示将摇杆推到底
  ./fpv_range.py --sec 30  采样时长

标定映射时若未将摇杆推到底，量程会被记录得过窄：摇杆未到极限而游戏内已为满杆，
且整条曲线会被拉长。本脚本用于检查这一问题。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpvhelper import channels as ch  # noqa: E402
from fpvhelper import config as cfg  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=cfg.DEFAULT_PATH)
    ap.add_argument('--sec', type=float, default=25.0)
    args = ap.parse_args()

    mapping, path = cfg.load(args.config)
    src = ch.open_source(dict(mapping.data.get('device') or {}, hidraw=mapping.data.get('hidraw') or {}))
    print(src.describe())
    print(f'\n采样 {args.sec:.0f} 秒：请将每根回中杆推到四个角落各停留片刻，油门与旋钮走完整个行程。\n')

    lo = {c.index: c.value for c in src.channels}
    hi = dict(lo)
    t0 = time.monotonic()
    while time.monotonic() - t0 < args.sec:
        f = src.poll(0.1)
        if not f:
            continue
        for i, v in f.values.items():
            lo[i], hi[i] = min(lo[i], v), max(hi[i], v)
        sys.stdout.write(f'\r  还剩 {args.sec - (time.monotonic() - t0):4.1f}s ')
        sys.stdout.flush()
    print('\n')
    print(f'  {"通道":<14}{"声明量程":>18}{"实际走到":>18}{"覆盖":>8}')
    for c in src.channels:
        span = (c.max - c.min) or 1
        got = hi[c.index] - lo[c.index]
        print(f'  ch{c.index} {c.label:<10}{c.min:>8}..{c.max:<8}'
              f'{lo[c.index]:>8}..{hi[c.index]:<8}{got / span * 100:>7.0f}%')
    print('\n  （某一路覆盖率明显不足 100%，即为标定映射时未推到底）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
