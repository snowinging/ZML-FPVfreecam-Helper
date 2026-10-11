"""把状态包发出去、把回执收回来，顺便数一数延迟和丢包。

UDP 走本机回环，不需要先连谁：游戏侧接收端 bind 固定端口，这边往那儿发，
回执包会从同一个 socket 回来，所以辅助程序自己不占固定端口。
"""
from __future__ import annotations

import socket
import time

from . import ipc


class UdpLink:
    def __init__(self, port=47800, host='127.0.0.1'):
        self.host = host
        self.port = port
        self.sock = None
        self.seq = 0
        self.t0 = time.monotonic()
        self.sent = 0
        self.recv = 0
        self.bad = 0
        self.lost = 0            # 已发出但回执还没确认的包数（回执 2 Hz，正常就是 1~2 个在途，不是丢包）
        self.last_ack = None
        self.game_lost = 0
        self.fps = 0
        self.flags = 0
        self.rtt = None          # 平滑过的往返延迟，毫秒
        self.last_seen = None    # 最后一次收到回执的时刻

    # ---------------------------------------------------------------- 开关

    def open(self, port=None):
        if port:
            self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        return self.sock.getsockname()

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock = None

    @property
    def open_(self):
        return self.sock is not None

    def now_ms(self):
        return int((time.monotonic() - self.t0) * 1000) & 0xFFFFFFFF

    # ---------------------------------------------------------------- 收发

    def send_state(self, roll, pitch, yaw, throttle, switches=0, buttons=0, aux=0, flags=0):
        if not self.sock:
            return False
        self.seq = (self.seq + 1) & 0xFFFF
        pkt = ipc.pack_state(self.seq, self.now_ms(), roll, pitch, yaw, throttle,
                             switches, buttons, aux, flags)
        try:
            self.sock.sendto(pkt, (self.host, self.port))
            self.sent += 1
            return True
        except OSError:
            return False

    def poll(self, max_packets=16):
        """把积压的回执全收掉，留最后一个。"""
        if not self.sock:
            return None
        last = None
        for _ in range(max_packets):
            try:
                data, _addr = self.sock.recvfrom(256)
            except BlockingIOError:
                break
            except OSError:
                break
            st = ipc.unpack_status(data)
            if not st:
                self.bad += 1
                continue
            self.recv += 1
            self.last_seen = time.monotonic()
            self.last_ack = st['ack_seq']
            self.flags = st['flags']
            self.game_lost = st['lost']
            self.fps = st['fps']
            gap = (self.seq - st['ack_seq']) & 0xFFFF
            if 0 < gap < 1000:
                self.lost = gap
            elif gap == 0:
                self.lost = 0
            dt = self.now_ms() - st['echo_t']
            if 0 <= dt < 10000:
                # 单次回环延迟抖动大，压一下
                self.rtt = dt if self.rtt is None else (self.rtt * 0.8 + dt * 0.2)
            last = st
        return last

    # ---------------------------------------------------------------- 读数

    def online(self, fresh=1.0):
        return self.last_seen is not None and (time.monotonic() - self.last_seen) < fresh

    def summary(self):
        if not self.sock:
            return '没开'
        if not self.online():
            return f'发 {self.sent}  没有回执（游戏侧没起？端口 {self.port}）'
        rtt = '?' if self.rtt is None else f'{self.rtt:.1f}'
        bits = []
        if self.flags & ipc.SG_RUNNING:
            bits.append('接收端在跑')
        if self.flags & ipc.SG_TAKEOVER:
            bits.append('相机已接管')
        if self.flags & ipc.SG_WANT_RESET:
            bits.append('游戏要求回中')
        tail = ('  ' + '、'.join(bits)) if bits else ''
        return (f'发 {self.sent}  收 {self.recv}  延迟 {rtt}ms  在途 {self.lost}'
                f'（游戏侧数出丢掉 {self.game_lost}）  游戏 {self.fps}fps{tail}')
