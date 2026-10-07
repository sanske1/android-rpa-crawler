# -*- coding: utf-8 -*-
"""view_data.py —— 查看某个抓取目录已抓到什么

优先读 comments_final.json（抓完后生成），没有就读 comments_raw.json（断点文件）。

用法：
    python view_data.py capture          # 概览 + 最新 10 条
    python view_data.py capture 30       # 最新 30 条
"""
import json
import os
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

d = sys.argv[1] if len(sys.argv) > 1 else 'capture'
n = int(sys.argv[2]) if len(sys.argv) > 2 else 10

path = None
for name in ('comments_final.json', 'comments_raw.json'):
    p = os.path.join(d, name)
    if os.path.exists(p):
        path = p
        break
if not path:
    print('没找到数据：%s 下既无 comments_final.json 也无 comments_raw.json' % d)
    sys.exit(1)

rows = json.load(open(path, encoding='utf-8'))
users = [r for r in rows if r.get('user')]
print('目录: %s' % d)
print('数据源: %s%s' % (os.path.basename(path),
      '（断点文件，抓完后会生成 comments_final.csv/json）' if 'raw' in path else ''))
print('总数: %d 条   带用户名+时间: %d 条 (%.0f%%)' % (len(rows), len(users), 100.0 * len(users) / len(rows) if rows else 0))
lens = [len(r['content']) for r in rows]
if lens:
    print('正文长度: 最短%d / 最长%d / 平均%.0f 字' % (min(lens), max(lens), sum(lens) / len(lens)))
print('\n最近 %d 条:' % n)
for r in rows[-n:]:
    print('  [%-9s|%-9s] %s' % (r.get('user') or '-', r.get('time') or '-',
                                r['content'].replace('\n', ' ')[:70]))
