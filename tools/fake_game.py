#!/usr/bin/env python3
"""假游戏端：站在游戏的位置收状态包、回回执，用来验证协议和延迟，不用开游戏。

用法：
  ./tools/fake_game.py                    # 收到什么打什么，每 0.5 秒一行
  ./tools/fake_game.py --bars             # 顺便画四条摇杆位置的条
  ./tools/fake_game.py --seconds 10       # 跑 10 秒自己退出
  ./tools/fake_game.py --quiet            # 只统计，不打明细
  ./tools/fake_game.py --takeover         # 回执里报"相机已接管"
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fpvhelper import ipc  # noqa: E402

NAMES = ('roll', 'pitch', 'yaw', 'throttle')


def bars(state):
    out = []
    for name in NAMES:
        v = state[name]
        if name == 'throttle':
            cells = int(v / ipc.THR_MAX * 21)
        else:
            cells = int((v + 500) / 1000 * 21)
        cells = max(0, min(21, cells))
        out.append(f'{name:>8} [{("#" * cells).ljust(21)}] {v:>6}')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=47800)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--seconds', type=float, default=0)
    ap.add_argument('--bars', action='store_true')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--takeover', action='store_true')
    ap.add_argument('--status-hz', type=float, default=2.0, help='回执频率')
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.settimeout(0.05)
    print(f'假游戏端起来：{args.host}:{args.port}  ' + ipc.describe(), flush=True)

    t0 = time.monotonic()
    n = bad = lost = 0
    last_seq = None
    last_pkt = None
    peer = None
    gaps = 0
    win_n = 0
    win_t = time.monotonic()
    next_status = 0.0
    rate = 0.0
    look_want = (ipc.SG_RUNNING | (ipc.SG_TAKEOVER if args.takeover else 0))

    while True:
        now = time.monotonic()
        if args.seconds and now - t0 >= args.seconds:
            break
        if now >= next_status and peer:
            next_status = now + 1.0 / args.status_hz
            sock.sendto(ipc.pack_status(last_seq or 0, last_pkt['t_ms'] if last_pkt else 0,
                                        look_want, lost, int((now - t0) * 1000), round(rate)),
                        peer)
        try:
            data, addr = sock.recvfrom(512)
        except socket.timeout:
            continue
        except OSError as e:
            print('收包出错:', e, flush=True)
            break
        st = ipc.unpack_state(data)
        if st is None:
            bad += 1
            continue
        if peer != addr:
            peer = addr
            last_seq = None
            print(f'辅助程序接上了：{addr[0]}:{addr[1]}', flush=True)
        if last_seq is not None:
            gap = (st['seq'] - last_seq - 1) & 0xFFFF
            if 0 < gap < 1000:
                lost += gap
                gaps += 1
        last_seq = st['seq']
        last_pkt = st
        n += 1
        win_n += 1

        if not args.quiet and args.bars:
            print('\r' + ' | '.join(bars(st)), end='', flush=True)
        elif not args.quiet:
            print(f"\rseq {st['seq']:>5}  r {st['roll']:>6} p {st['pitch']:>6} "
                  f"y {st['yaw']:>6}  thr {st['throttle']:>4}  sw {st['switches']:04b}",
                  end='', flush=True)

        now = time.monotonic()
        if now - win_t >= 1.0:
            rate = win_n / (now - win_t)
            win_n, win_t = 0, now
            if not args.quiet:
                print()
            print(f'[{now - t0:5.1f}s] 收到 {n} 包 / {rate:.0f} Hz  缺号 {lost}（{gaps} 次断）'
                  f'  坏包 {bad}', flush=True)

    if not args.quiet:
        print()
    print(f'结束：{n} 包，平均 ' + (f'{n / max(1e-6, time.monotonic() - t0):.0f} Hz，' if n else '')
          + f'缺号 {lost}，坏包 {bad}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
