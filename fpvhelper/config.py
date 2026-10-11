"""mapping.toml 读写。读用标准库 tomllib，写自己来（tomllib 只读，不想为这个加依赖）。

结构：
  [device] match / backend / path
  [hidraw] offset / width / endian / count
  [funcs.<功能名>] channel / kind / min / max / center / invert / deadband / expo / trim / rate / learned
                  多档开关（kind = select）另有两个键：bands（各档之间的分界值）/ map（档位序号
                  对应的值，模式开关用它排 角度模式 / 半自稳 / 手动模式 的先后）
"""
from __future__ import annotations

import os
import shutil
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
    if os.path.exists(path):
        try:
            shutil.copyfile(path, path + '.bak')
        except OSError as e:
            print(f'旧配置备份失败（不影响继续写入）：{e}')
    data = mapping.data
    lines = ['# Endfield FPV 遥控器映射：更换遥控器只需修改本文件，无需改动程序',
             '# 通道号 chN 为设备中的第 N 路，其顺序未必对应遥控器的物理位置，建议先用 fpv_learn.py 完整标定一次',
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
