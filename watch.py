# -*- coding: utf-8 -*-
"""watch.py —— 实时打印采集过程中新增的条目

原理：采集脚本每屏都会把当前 UI 树写到 <out>/_ui.xml。本程序只监视这个文件的
变化，用**和采集脚本完全相同的解析规则**解析，把新出现的条目实时打到终端。

只读文件、不发任何 adb 命令，所以不会干扰正在跑的任务。

用法：
    python watch.py                # 默认监视 ./capture
    python watch.py capture_mkt    # 指定输出目录
"""
import os
import sys
import time
import xml.etree.ElementTree as ET

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')   # Windows 控制台默认 GBK，避免中文乱码

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import uiparse as cc  # noqa: E402


class Parser(cc.Crawler):
    """只借解析方法，不碰 adb：覆盖 __init__ 跳过设备探测。"""

    def __init__(self, min_len=10):
        self.min_len = min_len

    def sh(self, *a, **k):
        raise RuntimeError('watch_comments 不应执行 adb 命令')


def acceptable(c):
    """与 uiparse.Crawler.clean() 一致的过滤，保证终端看到的和最终数据一致。"""
    c = (c or '').strip()
    if not c or c.startswith('i ') or 'ㆍ' in c or cc.RE_NOISE.search(c):
        return False
    if len(c) < 6:
        return False
    return cc.Crawler.zh_ratio(c) >= cc.ZH_MIN


def main():
    out = 'capture'
    for a in sys.argv[1:]:
        if not a.startswith('-'):
            out = a
    ui_path = os.path.join(out, '_ui.xml')
    p = Parser()

    print('监视 %s' % ui_path, flush=True)
    print('（抓取脚本每抓一屏就会更新这个文件；Ctrl-C 退出）\n', flush=True)

    seen = set()
    last_mtime = None
    n = 0
    while True:
        try:
            if not os.path.exists(ui_path):
                time.sleep(1)
                continue
            mt = os.path.getmtime(ui_path)
            if mt == last_mtime:
                time.sleep(0.6)
                continue
            try:
                root = ET.parse(ui_path).getroot()   # 可能正被写入，失败就下轮重试
            except Exception:
                time.sleep(0.5)
                continue
            last_mtime = mt

            # 和抓取脚本一致的合并逻辑：严格先、宽松补，按 content[:40] 去重
            store = {}
            for it in p.parse_strict(root) + p.parse_loose(root):
                k = it['content'][:40]
                if k in store:
                    for f in ('user', 'time', 'sku'):
                        if it.get(f) and not store[k].get(f):
                            store[k][f] = it[f]
                else:
                    store[k] = it

            for k, rec in store.items():
                if k in seen or not acceptable(rec['content']):
                    continue
                seen.add(k)
                n += 1
                who = rec.get('user') or '-'
                when = rec.get('time') or '-'
                print('#%-4d %-9s %-9s %s' % (n, who, when, rec['content']), flush=True)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print('  (跳过一屏: %s)' % e, flush=True)
            time.sleep(1)

    print('\n已停止，共看到 %d 条新评论。' % n, flush=True)


if __name__ == '__main__':
    main()
