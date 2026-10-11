#!/usr/bin/env python3
"""遥控器 HID 探针：识别设备、采集原始报告、统计各字节与候选通道的活动范围。

用法:
  ./radio_probe.py --list                     列出候选 HID 设备
  ./radio_probe.py --seconds 120 --csv a.csv  采集 120 秒并保存原始报告
  ./radio_probe.py --bar                      实时打印 8 个通道的柱状图（标定时使用）
无需 root：设备的 udev 规则带有 uaccess 标签，当前登录用户已具备 ACL 权限。
"""
import argparse, os, select, sys, time, glob

CANDIDATE_NAMES = ('BETAFPV', 'OpenTX', 'Joystick', 'TX16', 'Radiomaster', 'Jumper', 'EdgeTX')


def find_hidraw():
    """按 HID_NAME 找遥控器，返回 /dev/hidrawN 列表（按匹配度排序）。"""
    hits = []
    for path in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        name = ''
        try:
            with open(os.path.join(path, 'device', 'uevent')) as f:
                for line in f:
                    if line.startswith('HID_NAME='):
                        name = line.strip().split('=', 1)[1]
        except OSError:
            continue
        node = '/dev/' + os.path.basename(path)
        score = sum(1 for k in CANDIDATE_NAMES if k.lower() in name.lower())
        hits.append((score, node, name))
    hits.sort(key=lambda h: (-h[0], h[1]))
    return hits


def open_node(node):
    return os.open(node, os.O_RDONLY | os.O_NONBLOCK)


def u16le(buf, off):
    return buf[off] | (buf[off + 1] << 8)


def s16le(buf, off):
    v = u16le(buf, off)
    return v - 65536 if v >= 32768 else v


def capture(node, seconds, csv_path=None):
    fd = open_node(node)
    print(f'# 设备 {node}，抓 {seconds} 秒原始报告（现在去动摇杆）', flush=True)
    csvf = open(csv_path, 'w') if csv_path else None
    if csvf:
        csvf.write('t,' + ','.join(f'b{i}' for i in range(64)) + '\n')
    per_byte = {}
    lengths = {}
    count = 0
    t0 = time.time()
    last_print = t0
    try:
        while time.time() - t0 < seconds:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                continue
            try:
                data = os.read(fd, 64)
            except BlockingIOError:
                continue
            if not data:
                continue
            count += 1
            lengths[len(data)] = lengths.get(len(data), 0) + 1
            for i, b in enumerate(data):
                lo, hi, n = per_byte.get(i, (255, 0, 0))
                per_byte[i] = (min(lo, b), max(hi, b), n + 1)
            if csvf:
                csvf.write(f'{time.time() - t0:.4f},' + ','.join(str(b) for b in data) + '\n')
            now = time.time()
            if now - last_print > 5:
                last_print = now
                top = sorted(per_byte.items(), key=lambda kv: kv[1][1] - kv[1][0], reverse=True)[:4]
                print(f'  [{now - t0:5.1f}s] {count} 包  活动最大的字节: ' +
                      ', '.join(f'b{i}({lo}-{hi})' for i, (lo, hi, n) in top), flush=True)
    finally:
        if csvf:
            csvf.close()
        os.close(fd)

    dur = time.time() - t0
    print(f'\n# 共 {count} 包 / {dur:.1f} 秒 = {count / dur:.1f} Hz')
    print(f'# 报告长度分布: {lengths}')
    print('\n# 各字节活动范围（仅列出有变化的）')
    for i in sorted(per_byte):
        lo, hi, n = per_byte[i]
        if hi > lo:
            print(f'  b{i:<3} {lo:>3}-{hi:>3}')
    print('\n# 候选通道：16 位小端，自偏移 1 起，每 2 字节为一路')
    for idx, off in enumerate(range(1, 17, 2)):
        vals = []
        if csv_path:
            with open(csv_path) as f:
                next(f)
                for line in f:
                    parts = line.strip().split(',')
                    if len(parts) > off + 1:
                        vals.append((int(parts[off + 1]), int(parts[off + 2])))
        if not vals:
            continue
        u = [a | (b << 8) for a, b in vals]
        s = [v - 65536 if v >= 32768 else v for v in u]
        print(f'  ch{idx}（偏移 {off}-{off + 1}）：无符号 {min(u)}..{max(u)}  有符号 {min(s)}..{max(s)}  '
              f'{sum(1 for v in u if v != u[0]) / len(u) * 100:.0f}% 样本有变化')
    print('\n# 提示：16 位通道中仅 4 路有变化，说明其余两路为其他位宽；')


def bar(node, seconds):
    fd = open_node(node)
    print('# 8 路柱状图（EOF / Ctrl-C 退出）')
    import struct
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                continue
            try:
                data = os.read(fd, 64)
            except BlockingIOError:
                continue
            if len(data) < 17:
                continue
            chans = [s16le(data, off) for off in range(1, 17, 2)]
            line = ' | '.join(f'{i}:{c:>6}' for i, c in enumerate(chans))
            print(line, flush=True)
    finally:
        os.close(fd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--node', help='直接指定 /dev/hidrawN')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--seconds', type=float, default=60)
    ap.add_argument('--csv')
    ap.add_argument('--bar', action='store_true')
    a = ap.parse_args()

    if a.list:
        for score, node, name in find_hidraw():
            print(f'{score}  {node}  {name}')
        return
    node = a.node
    if not node:
        hits = [h for h in find_hidraw() if h[0] > 0]
        if not hits:
            print('未找到遥控器，可用 --list 查看', file=sys.stderr)
            return 1
        node = hits[0][1]
        print(f'# 选中 {node}（{hits[0][2]}）')
    if a.bar:
        return bar(node, a.seconds)
    capture(node, a.seconds, a.csv)
    return 0


if __name__ == '__main__':
    sys.exit(main())
