#!/usr/bin/env python3
"""学习模式：让程序自己问出"第几路是油门"，然后写进 mapping.toml。

  ./fpv_learn.py                先侦查动过哪几路，再逐个功能学
  ./fpv_learn.py --funcs roll,pitch,--no-discover
  ./fpv_learn.py --dump         只看当前映射，不改

学完通道号和人命名的对应关系就固定下来了，除非换遥控器，不用再学。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpvhelper import channels as ch  # noqa: E402
from fpvhelper import config as cfg  # noqa: E402

CLEAR, RESET, BOLD = '\033[H\033[J', '\033[0m', '\033[1m'


def sample(src, seconds, label=''):
    """采一段时间，返回每通道的 (min, max, mean, n)。"""
    acc = {c.index: [c.value, c.value, 0.0, 0] for c in src.channels}
    t0, last = time.monotonic(), None
    while time.monotonic() - t0 < seconds:
        f = src.poll(0.1)
        if not f:
            continue
        last = f
        for i, v in f.values.items():
            a = acc[i]
            a[0], a[1], a[2], a[3] = min(a[0], v), max(a[1], v), a[2] + v, a[3] + 1
        left = seconds - (time.monotonic() - t0)
        sys.stdout.write(f'\r  {label} 采集中 {left:4.1f}s  ')
        sys.stdout.flush()
    print()
    return {i: (a[0], a[1], a[2] / a[3] if a[3] else a[0], a[3]) for i, a in acc.items()}


def spread(stats):
    return {i: s[1] - s[0] for i, s in stats.items()}


def ask(text):
    print()
    print(f'{BOLD}{text}{RESET}')
    try:
        return input('  准备好就回车 (q 跳过这一步) > ').strip().lower()
    except EOFError:
        return 'q'


def pick_channel(stats, exclude=()):
    """谁在这次采样里动得最多就是它。"""
    sp = {i: v for i, v in spread(stats).items() if i not in exclude}
    if not sp:
        return None, 0
    best = max(sp, key=lambda i: sp[i])
    return best, sp[best]


def learn(args):
    mapping, path = cfg.load(args.config)
    device = mapping.data.setdefault('device', {})
    if args.match:
        device['match'] = args.match
    src = ch.open_source(dict(device, hidraw=mapping.data.get('hidraw', {})))
    print(src.describe())
    print(f'\nmapping: {path}')

    if args.dump:
        print(mapping.summary())
        return 0

    min_move = max(4, int(next((c.max - c.min for c in src.channels), 2047) * args.threshold))

    if not args.no_discover:
        ask(f'侦查：把所有摇杆、拨杆、旋钮、开关都慢慢过一遍，大约 {args.discover:.0f} 秒')
        stats = sample(src, args.discover, '侦查')
        live = {i: d for i, d in spread(stats).items() if d > min_move}
        print(f'  动过的通道：{sorted(live)}   没动过的：{sorted(set(spread(stats)) - set(live))}')
        if not live:
            print('  一个都没动过，检查遥控器是不是开着、线插着')
            return 1
        if not ask('继续逐个功能学？'):
            pass

    for name in args.funcs.split(','):
        name = name.strip()
        if not name:
            continue
        if name in ch.SWITCH_FUNCS:
            ok = learn_switch(src, mapping, name, min_move)
        else:
            ok = learn_stick(src, mapping, name, min_move)
        if not ok:
            print(f'  {name} 跳过')

    cfg.save(mapping, args.config)
    print(f'\n写回 {args.config}')
    print(mapping.summary())
    return 0


def learn_stick(src, mapping, name, min_move):
    kind = 'range' if name in ch.STICK_FUNCS else 'axis'
    if ask(f'学「{name}」：先把它推到 {("最大" if kind == "range" else "正方向")} 到底，别松手继续按回车') == 'q':
        return False
    a = sample(src, 2.0, '前半程')
    if ask(f'再把它推到 {("最小" if kind == "range" else "反方向")} 到底，别松手') == 'q':
        return False
    b = sample(src, 2.0, '后半程')
    ch_a, d_a = pick_channel(a, exclude=mapping.learned_channels() - {mapping.funcs.get(name, {}).get('channel')})
    ch_b, d_b = pick_channel(b, exclude=mapping.learned_channels() - {mapping.funcs.get(name, {}).get('channel')})
    if ch_a is None or d_a < min_move:
        print(f'  没看出你在推杆（最大变化 {d_a}）')
        return False
    if ch_b is not None and d_b > d_a and ch_b != ch_a:
        print(f'  两次动的是不同通道（第一次 ch{ch_a}，第二次 ch{ch_b}），重来一遍吧')
        return False
    lo = min(a[ch_a][0], b[ch_a][0])
    hi = max(a[ch_a][1], b[ch_a][1])
    mean_a, mean_b = a[ch_a][2], b[ch_a][2]
    forward_is_higher = mean_a > mean_b
    spec = mapping.funcs.setdefault(name, {})
    spec.update({'channel': ch_a, 'kind': kind, 'min': lo, 'max': hi,
                 'center': int(round((mean_a + mean_b) / 2)),
                 'invert': False if forward_is_higher else (kind == 'axis'),
                 'deadband': 0.01, 'expo': spec.get('expo', 0.0), 'learned': True})
    print(f'  学到 {name} = ch{ch_a}  {lo}~{hi} 中点 {spec["center"]} '
          f'{"反向" if spec["invert"] else "正向"}')
    return True


def learn_switch(src, mapping, name, min_move):
    positions = []
    for label in ('关位/最上一个档', '开位/下一档', '再下一档(没有就回车跳过)'):
        if ask(f'学「{name}」的{label}，拨到那儿别动') == 'q':
            break
        s = sample(src, 1.5, label)
        c, d = pick_channel(s)
        if c is None:
            break
        positions.append((c, s[c][2]))
        if len(positions) >= 2 and '跳过' in label:
            break
    if not positions:
        return False
    target = positions[0][0]
    positions = [(c, v) for c, v in positions if c == target]
    if len(positions) == 1:
        print('  只学到一个档，开关至少要有两个位置')
        return False
    vals = [v for _, v in positions]
    lo, hi = min(vals), max(vals)
    spec = mapping.funcs.setdefault(name, {})
    if len(positions) == 2:
        spec.update({'channel': target, 'kind': 'switch', 'min': int(lo), 'max': int(hi),
                     'invert': vals[1] < vals[0], 'learned': True})
        print(f'  学到 {name} = ch{target} 阈值 {int((lo + hi) / 2)} '
              f'{"反向" if spec["invert"] else "正向"}')
    else:
        step = (hi - lo) / (len(positions) - 1)
        bands = [lo + step * i for i in range(1, len(positions))]
        spec.update({'channel': target, 'kind': 'select', 'min': int(lo), 'max': int(hi),
                     'bands': [int(b) for b in bands], 'invert': False, 'learned': True})
        print(f'  学到 {name} = ch{target} {len(positions)} 档，分界 {spec["bands"]}')
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=cfg.DEFAULT_PATH)
    ap.add_argument('--funcs', default='roll,pitch,yaw,throttle,arm')
    ap.add_argument('--match', default='')
    ap.add_argument('--discover', type=float, default=10.0)
    ap.add_argument('--no-discover', action='store_true')
    ap.add_argument('--threshold', type=float, default=0.15, help='认成一个通道动过的最小行程比例')
    ap.add_argument('--dump', action='store_true')
    args = ap.parse_args()
    sys.exit(learn(args))


if __name__ == '__main__':
    main()
