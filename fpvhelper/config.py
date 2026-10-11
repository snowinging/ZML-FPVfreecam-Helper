"""mapping.toml 读写。读用标准库 tomllib，写自己来（tomllib 只读，不想为这个加依赖）。

结构：
  [device] match / backend / path
  [hidraw] offset / width / endian / count
  [funcs.<功能名>] channel / kind / min / max / center / invert / deadband / expo / trim / rate / learned
"""
from __future__ import annotations

import os
import tomllib

from .channels import Mapping

DEFAULT_DIR = os.path.expanduser('~/.config/endfield-fpv')
DEFAULT_PATH = os.path.join(DEFAULT_DIR, 'mapping.toml')


def load(path=DEFAULT_PATH):
    if not os.path.exists(path):
        return Mapping(), path
    with open(path, 'rb') as f:
        return Mapping(tomllib.load(f)), path


def save(mapping, path=DEFAULT_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = mapping.data
    lines = ['# Endfield FPV 遥控器软映射 —— 换遥控器只改这个文件，不用动代码',
             '# 通道号 chN 是设备里第 N 路，顺序不一定对应遥控器的物理位置，用 fpv_learn.py 学一遍最省事',
             '']
    head = {k: v for k, v in data.items() if k != 'funcs'}
    for section, body in head.items():
        if not isinstance(body, dict):
            continue
        lines.append(f'[{section}]')
        for k, v in body.items():
            lines.append(f'{k} = {_fmt(v)}')
        lines.append('')
    for name, spec in data.get('funcs', {}).items():
        lines.append(f'[funcs.{name}]')
        for k, v in spec.items():
            lines.append(f'{k} = {_fmt(v)}')
        lines.append('')
    with open(path, 'w') as f:
        f.write('\n'.join(lines).rstrip() + '\n')
    return path


def _fmt(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, str):
        return '"' + v.replace('"', '\\"') + '"'
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return '[' + ', '.join(_fmt(x) for x in v) + ']'
    return '"' + str(v) + '"'
