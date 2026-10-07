# -*- coding: utf-8 -*-
"""uiparse.py — Android 无障碍树解析器（RPA 采集框架的解析层）

职责：给定一屏的 uiautomator XML，把它解析成结构化的文本条目，
并做去重与清洗。与具体 App 无关，靠可配置的规则识别「条目 / 昵称 / 时间 / 规格行」。

解析思路（双模式，互为补充）：
  * 严格模式：以「时间行」为锚点，在其上下有限范围内找昵称与正文，字段完整。
  * 宽松模式：不看锚点，直接收集中文占比达标、长度合适的文本，产量更高。
  两者按正文前若干字合并去重，严格记录为公司宽松记录补齐缺失字段。

用法:
    python uiparse.py --probe --out ./capture   # 只解析当前一屏，验证环境
    python uiparse.py --out ./capture           # 自动滚动 + 解析 + 出报告

输出:
    <out>/comments_raw.json     原始（含噪声，便于复盘）
    <out>/comments_final.csv    清洗后（utf-8-sig，Excel 直接打开）
    <out>/comments_final.json
    <out>/comments_report.html  可浏览报告
"""
import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

# Git Bash / MSYS 下必须关闭路径转换，否则 /sdcard/... 会被改成 C:/...
ENV = dict(os.environ, MSYS_NO_PATHCONV='1')


def find_adb(user_adb=None):
    """找 adb：优先 --adb / 环境变量 ADB，其次 PATH，最后常见模拟器安装位置。"""
    for p in (user_adb, os.environ.get('ADB')):
        if p and os.path.exists(p):
            return p
    p = shutil.which('adb')
    if p:
        return p
    for p in (r'C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe',
              r'C:\Program Files\BlueStacks_nxt\HD-Adb.exe',
              os.path.expanduser('~/AppData/Local/Android/Sdk/platform-tools/adb.exe')):
        if os.path.exists(p):
            return p
    return 'adb'

# 时间行锚点：N天前 / N个月前 / 2026-09-21
# 注意：时间行可能带空格（实测 "7 个月前"、"10 个月前"），必须容忍空白，
# 否则严格模式一条都匹配不到，用户名/时间全丢。
RE_TIME = re.compile(r'^(?:\d+\s*(?:秒|分钟|小时|天|周|个月|年)\s*前|\d{4}-\d{1,2}-\d{1,2})$')

# 昵称通常是脱敏形式「首字**尾字」（如 椰**z / 4**4 / 🥷**0）。
# 拿它当"这是不是昵称"的判据，可一把排掉 UI 文案（物流/保障/已选）和统计数字（5151、(32.2万+)），
# 这些都在实测里冒充过用户名，可按自家 App 的昵称格式调整。
RE_NICK = re.compile(r'^[\w一-鿿\U0001F000-\U0001FAFF]{1,4}\*\*[\w一-鿿\U0001F000-\U0001FAFF]{0,3}$')

# 正文中文占比下限。实测：商品标题一类的噪声约 0.36，
# 而最低的真实正文约 0.69，中间缝隙很大 -> 取 0.45 可精确剔掉标题类噪声、零误伤。
ZH_MIN = 0.45

# 严格模式下判断"昵称/正文"时要跳过的界面固定文案
UI_FIXED = ('商家回复', '浏览', '有用', '选款式', '评价', '详情', '推荐', '旗舰店', '客服',
            '¥', '抢购', '顺丰', '运费险', '好评率', '销量', '更多视频', '7天无理由',
            '回头客', '商品', '展开', '收起', '预计', '刚刚')

# 宽松模式：正文候选必须不含这些（界面文案/规格行）
BAD_WORDS = ('顺丰包邮', '运费险', '7天无理由', '好评率', '销量', '旗舰店', '抢购', '客服',
             '浏览', '有用', '选款式', '更多视频', '回头客', '新增好评', '展开', '收起',
             '商品评价', '规格', '请选择', '加入购物车', '立即购买', '优惠券', '店铺', '保障',
             '已折叠', '帮助不大', '未填写评价内容')

# 最终清洗：改用规则而非写死商品词（原先的"浙江宁波发货/充气泵类型"绑死上一个商品，
# 换个商品就失效）。注意：绝不能用"发货/退货"这类宽泛词——会误伤真实评论
# （"发货迅速物流也快…"、"我要求退货"都是真实数据）。
RE_NOISE = re.compile(
    r'^\s*发货\s'                                                # 物流条："发货 预计广东深圳发货 ｜ 免运费"
    r'|预计[^，。！？]{0,8}(?:发货|送达|送到|抵达)'              # "预计后天送达"
    r'|\d+\s*天无理由'                                          # "7天无理由"
    r'|(?:爆款榜|热销榜|好评榜|回购榜|金榜)[·・]?第\s*\d+\s*名'    # 榜单徽章："…爆款榜·第6名"
    r'|送至|达人秀|买家秀'                                      # "送至 贵州省毕节市" / "达人秀·买家秀(250)"
    r'|^[\(\（]?[\d\.]+万?\+?[\)\）]?$'                          # 纯统计数字："(32.2万+)"、"5151"
    r'|^\s*\d+(?:\.\d+)?\s*万?\+?\s*人(?:感兴趣|加购|已购|购买|收藏)\s*$'   # "75.5万人感兴趣"、"130万+人加购"
    r'|入会领|满\d+\s*减\d+'                                     # 促销条："入会领·满89减5"
    r'|^\s*\d+(?:\.\d+)?\s*万?\+?\s*(?:人|达人)?推荐\s*$'          # "900+达人推荐"、"12.8万人推荐"
    r'|为你挑选|新增好评|大件运费险|未填写评价内容|已折叠|帮助不大'
)

# SKU/规格行特征：
#  - 含规格分隔符 ◆/◇，或 "*2片装" 这类数量规格（列表页常见，如
#    "【高清-离子防爆膜】…◆…*2片装/iPhone14 Plus"——又长又带标点，老规则会漏）
#  - 短、无句读、含类目规格词（如"双人床PVC+220V气泵"）
RE_SKU_SPEC = re.compile(r'[◆◇]|\*\s*\d+\s*[片只个条件套]\s*装')
SKU_HINT = ('PVC', 'TPU', '锂电泵', '气泵', '无泵', '双人', '单人', '三人', '优享',
            '夏季版', '四季版', '豪华版', '标准版')
RE_PUNCT = re.compile(r'[，。！？、；：,.!?~～]')


def is_sku(t):
    """判定 SKU/规格行而非评论正文（命中任一即算）。"""
    if RE_SKU_SPEC.search(t):
        return True
    # 规格串：含 【…】 且有分隔符。实测两种版式：
    #   "【高清-离子防爆膜】…◆…*2片装/iPhone14 Plus"
    #   "防窥膜-保护隐私【无尘秒贴舱】…/【超值装】买二送一 到手三…"
    if len(t) <= 80 and '【' in t and '】' in t and any(
            c in t for c in ('/', '／', '｜', '|', '◆', '◇', '*')):
        return True
    # 规格串（无【】版）：多个 / 分段的机型/规格串且含品类词、无句读，
    # 如 "某某适用于A18ProMax/17Pro/16防爆抗指纹手机膜15/14高清钢化膜"
    if (t.count('/') >= 2 and len(t) <= 80 and not RE_PUNCT.search(t)
            and any(k in t for k in ('膜', '适用', '装', '款', '版'))):
        return True
    return len(t) <= 32 and not RE_PUNCT.search(t) and any(k in t for k in SKU_HINT)


class Crawler(object):
    def __init__(self, adb, out_dir, max_screens=400, sleep=2.6,
                 swipe_from=0.74, swipe_to=0.29, min_len=10, expand=True):
        self.adb = adb
        self.out_dir = out_dir
        self.max_screens = max_screens
        self.sleep = sleep
        self.swipe_from = swipe_from
        self.swipe_to = swipe_to
        self.min_len = min_len
        self.expand = expand
        os.makedirs(out_dir, exist_ok=True)
        self.w, self.h = self.screen_size()
        print(f'屏幕尺寸: {self.w}x{self.h}')

    def sh(self, args, timeout=60):
        return subprocess.run(args, capture_output=True, env=ENV, timeout=timeout)

    def screen_size(self):
        """以实际截图为准：wm size 报的是物理分辨率，可能和当前方向相反（横/竖屏）。"""
        try:
            import struct
            r = self.sh([self.adb, 'exec-out', 'screencap', '-p'], timeout=30)
            d = r.stdout
            i = d.find(b'\x89PNG')
            if i >= 0 and len(d) >= i + 24:
                w, h = struct.unpack('>II', d[i + 16:i + 24])
                print('screencap: %dx%d' % (w, h))
                return w, h
        except Exception:
            pass
        r = self.sh([self.adb, 'shell', 'wm', 'size'])
        m = re.search(r'(\d+)x(\d+)', r.stdout.decode('utf-8', 'ignore'))
        return (int(m.group(1)), int(m.group(2))) if m else (900, 1600)

    # ---------- 采集 ----------
    def dump_ui(self, tmp=None):
        tmp = tmp or os.path.join(self.out_dir, '_ui.xml')
        self.sh([self.adb, 'shell', 'uiautomator', 'dump', '/sdcard/ui.xml'])
        r = self.sh([self.adb, 'shell', 'cat', '/sdcard/ui.xml'])
        if not r.stdout:
            return None
        with open(tmp, 'wb') as f:
            f.write(r.stdout)
        try:
            return ET.parse(tmp).getroot()
        except Exception:
            return None

    def swipe_up(self):
        x = self.w // 2
        self.sh([self.adb, 'shell', 'input', 'swipe', str(x),
                 str(int(self.h * self.swipe_from)), str(x), str(int(self.h * self.swipe_to)), '600'])

    def tap_folded(self, root):
        """点击"已折叠 N 条对你帮助不大的评价"展开隐藏评论（这些不展开永远抓不到）。"""
        for n in root.iter('node'):
            t = (n.get('text') or '')
            if '已折叠' in t and '评价' in t:
                m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', n.get('bounds', ''))
                if not m:
                    continue
                x = (int(m.group(1)) + int(m.group(3))) // 2
                y = (int(m.group(2)) + int(m.group(4))) // 2
                self.sh([self.adb, 'shell', 'input', 'tap', str(x), str(y)])
                time.sleep(2.5)
                return True
        return False

    def find_top_button(self, root):
        """定位页面自带的「回到顶部」按钮，返回中心坐标或 None。

        实测（1080x2400）：class=android.widget.ImageButton、clickable、约 176x176、
        固定在右下角（中心≈(992,1909)）；**无 text/content-desc**、resource-id 被混淆，
        所以只能靠 类名 + 尺寸 + 右下角位置 认。列表已在顶部时该按钮不显示。
        """
        for n in root.iter('node'):
            if not n.get('class', '').endswith('ImageButton') or n.get('clickable') != 'true':
                continue
            m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', n.get('bounds', ''))
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            bw, bh = x2 - x1, y2 - y1
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            if bw <= self.w * 0.35 and bh <= self.h * 0.35 and cx > self.w * 0.7 and cy > self.h * 0.5:
                return cx, cy
        return None

    def back_to_top(self, tries=3):
        """点「回到顶部」按钮回顶。

        比连发 25 次下滑快得多，也不会把手机 adbd 刷爆（真机上滑动密集时 adbd 会掉线）。
        按钮不存在 = 已在顶部，直接返回。
        """
        for attempt in range(tries):
            root = self.dump_ui()
            if root is None:
                return
            pos = self.find_top_button(root)
            if pos is None:
                print('已在列表顶部' if attempt == 0 else '已回到列表顶部')
                return
            self.sh([self.adb, 'shell', 'input', 'tap', str(pos[0]), str(pos[1])])
            time.sleep(2.5)
        print('点「回到顶部」%d 次后按钮仍未消失，继续抓取' % tries)

    # ---------- 解析 ----------
    @staticmethod
    def y_center(node):
        m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', '[0,0][0,0]'))
        return (int(m.group(2)) + int(m.group(4))) // 2 if m else 0

    @staticmethod
    def signature(root):
        """整屏指纹：所有 (bounds,text) 排序后 md5。连续两次相同 = 到底。"""
        parts = []
        for n in root.iter('node'):
            t = (n.get('text') or '').strip()
            if t:
                parts.append('%s|%s' % (n.get('bounds'), t))
        return hashlib.md5('\n'.join(sorted(parts)).encode('utf-8')).hexdigest()

    @staticmethod
    def zh_ratio(t):
        """中文字符占比。页面噪声（"(32.2万+)"、商品标题串）占比很低。"""
        return sum(1 for c in t if '一' <= c <= '鿿') / len(t) if t else 0

    def parse_strict(self, root):
        """以时间行为锚点，在**同一条评论的纵向范围**内配 昵称/正文/SKU。

        范围必须收紧：真机 UI 树里混着相邻卡片与屏幕外节点，旧版"上方最近短文本=昵称"
        会把邻卡昵称挂过来（实测把 椰**z 的评论挂成了 W**f，并且 椰**z/狗**x 整个丢失）。
        """
        nodes = [(self.y_center(n), (n.get('text') or '').strip())
                 for n in root.iter('node') if (n.get('text') or '').strip()]
        nodes.sort()
        items = []
        for i, (y_t, t) in enumerate(nodes):
            if not RE_TIME.match(t):
                continue
            # 昵称：时间行上方 <=140px 内、最近的**脱敏昵称**；其他一切（UI 文案/数字）都不算
            user = ''
            for j in range(i - 1, -1, -1):
                y_u, u = nodes[j]
                if y_t - y_u > 140:
                    break
                if RE_NICK.match(u):
                    user = u
                    break
            # 正文范围：本卡时间行 -> **下一个时间锚点**，且最多向下 600px
            # （注意是下一个"时间锚点"而不是紧邻的下一节点——时间行下方紧跟的是昵称）
            limit = y_t + 600
            for j in range(i + 1, len(nodes)):
                if RE_TIME.match(nodes[j][1]):
                    limit = min(limit, nodes[j][0])
                    break
            sku, content = '', ''
            for k in range(i + 1, len(nodes)):
                y_k, u = nodes[k]
                if y_k > limit:
                    break
                if u.startswith('商家回复'):
                    break
                if not sku and '|' in u and len(u) < 40:
                    sku = u
                    continue
                if (len(u) >= 6 and not u.startswith(('浏览', 'i 有用', '有用'))
                        and not is_sku(u) and not RE_NOISE.search(u)
                        and not any(w in u for w in BAD_WORDS)
                        and self.zh_ratio(u) >= ZH_MIN):
                    content = u
                    break
            if content:
                items.append({'user': user, 'time': t, 'sku': sku, 'content': content})
        return items

    def parse_loose(self, root):
        """不看锚点，直接收集中文长文本作为正文候选（产量 3-5 条/屏）。"""
        out = []
        for n in root.iter('node'):
            t = (n.get('text') or '').strip()
            if not (self.min_len <= len(t) <= 400):
                continue
            if t.startswith('商家回复') or t.startswith('i ') or '|' in t:
                continue
            han = sum(1 for c in t if '\u4e00' <= c <= '\u9fff')
            if han < len(t) * ZH_MIN:
                continue
            if (any(w in t for w in BAD_WORDS) or RE_TIME.match(t)
                    or is_sku(t) or RE_NOISE.search(t)):
                continue
            out.append({'user': '', 'time': '', 'sku': '', 'content': t})
        return out

    # ---------- 主流程 ----------
    def crawl(self, do_top=True):
        store = {}
        raw_path = os.path.join(self.out_dir, 'comments_raw.json')
        if os.path.exists(raw_path):                      # 续跑：载入历史
            for it in json.load(open(raw_path, encoding='utf-8')):
                if it.get('content'):
                    store[it['content'][:40]] = it
            print('已载入历史 %d 条' % len(store))

        if do_top:
            self.back_to_top()
            print('已回到列表顶部')

        screen, last_sig, same = 0, None, 0
        while screen < self.max_screens and same < 2:
            root = self.dump_ui()
            screen += 1
            if root is None:
                print('  [%d] dump 失败，重试' % screen)
                time.sleep(2)
                continue
            sig = self.signature(root)
            if sig == last_sig:
                same += 1
                print('  [%d] 整屏签名相同 (%d/2) —— 页面未滚动' % (screen, same))
                if same >= 2:
                    print('判定：已滑动到底（两次抓取完全一致），停止')
                    break
            else:
                same, last_sig = 0, sig
            if self.expand and self.tap_folded(root):   # 展开折叠区后重新取屏
                root = self.dump_ui() or root
                print('  [%d] 已展开折叠评价' % screen)
            strict = self.parse_strict(root)
            loose = self.parse_loose(root)
            new = sum(self.add(store, it) for it in strict + loose)
            print('  [%d] 严格 %d / 宽松 %d，新增 %d，累计 %d'
                  % (screen, len(strict), len(loose), new, len(store)))
            self.swipe_up()
            time.sleep(self.sleep)

        rows = list(store.values())
        json.dump(rows, open(raw_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        print('抓取完成：%d 条（原始），共 %d 屏' % (len(rows), screen))
        return rows

    @staticmethod
    def add(store, it):
        key = it['content'][:40]
        if key in store:
            for k in ('user', 'time', 'sku'):
                if it.get(k) and not store[key].get(k):
                    store[key][k] = it[k]
            return 0
        store[key] = it
        return 1

    # ---------- 清洗与输出 ----------
    @staticmethod
    def clean(rows):
        clean_rows, dropped = [], []
        for r in rows:
            c = (r['content'] or '').strip()
            if c.startswith('i '):
                dropped.append((c[:40], '详情页摘要'))
                continue
            if 'ㆍ' in c or RE_NOISE.search(c):
                dropped.append((c[:40], '页面噪声'))
                continue
            if len(c) < 6:
                dropped.append((c[:40], '过短'))
                continue
            if is_sku(c):
                dropped.append((c[:40], '规格行'))
                continue
            if Crawler.zh_ratio(c) < ZH_MIN:
                dropped.append((c[:40], '中文占比过低'))   # 商品标题/规格串
                continue
            r['content'] = c
            clean_rows.append(r)
        print('清洗: %d -> %d（删除 %d）' % (len(rows), len(clean_rows), len(dropped)))
        for c, why in dropped[:10]:
            print('  [-%s] %s' % (why, c))
        return clean_rows

    @staticmethod
    def write_csv(rows, path):
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['user', 'time', 'sku', 'content'],
                               extrasaction='ignore')
            w.writeheader()
            w.writerows(rows)

    @staticmethod
    def write_html(rows, path, title='列表数据采集报告'):
        def esc(s):
            return html.escape(s or '')
        cards = []
        for i, r in enumerate(rows, 1):
            meta = ' · '.join(x for x in (r.get('user'), r.get('time'), r.get('sku')) if x)
            c = r['content']
            m = re.match(r'^用户\s*(.*?)追评[：:]\s*(.*)$', c, re.S)
            badge = ''
            if m:
                badge = '<span class="badge">追评（%s）</span>' % esc(m.group(1))
                c = m.group(2)
            cards.append('<div class="card"><div class="head"><span class="idx">#%d</span>%s'
                         '<span class="meta">%s</span></div><div class="body">%s</div></div>'
                         % (i, badge, esc(meta), esc(c)))
        page = (
            '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>%s</title>'
            '<style>body{font-family:"Microsoft YaHei",sans-serif;max-width:900px;margin:0 auto;'
            'padding:24px;background:#f6f7f9;color:#222}h1{font-size:22px}'
            '.stats{display:flex;gap:12px;margin:16px 0}.stat{background:#fff;border-radius:10px;'
            'padding:14px 20px;box-shadow:0 1px 3px rgba(0,0,0,.08)}.stat b{font-size:26px;'
            'display:block;color:#e0483e}.card{background:#fff;border-radius:10px;padding:12px 16px;'
            'margin:10px 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}.head{display:flex;gap:10px;'
            'align-items:center;margin-bottom:6px}.idx{color:#999;font-size:12px}'
            '.meta{color:#888;font-size:13px}.badge{background:#fff3e0;color:#e67e22;font-size:12px;'
            'padding:1px 8px;border-radius:10px}.body{font-size:15px;line-height:1.6;'
            'white-space:pre-wrap}.note{color:#666;font-size:13px;background:#eef4ff;padding:10px 14px;'
            'border-radius:8px}</style></head><body><h1>%s</h1>'
            '<div class="note">共 %d 条 · 其中 %d 条带用户名/时间 · 方法：模拟器 uiautomator 自动滚动抓取</div>'
            '<div class="stats"><div class="stat"><b>%d</b>评论正文</div>'
            '<div class="stat"><b>%d</b>带用户名</div>'
            '<div class="stat"><b>%d</b>含追评</div></div>%s</body></html>'
            % (esc(title), esc(title), len(rows), sum(1 for r in rows if r.get('user')),
               len(rows), sum(1 for r in rows if r.get('user')),
               sum(1 for r in rows if '追评' in r['content']), ''.join(cards)))
        open(path, 'w', encoding='utf-8').write(page)


def main():
    ap = argparse.ArgumentParser(description='Android 无障碍树采集（RPA / uiautomator 自动滚动）')
    ap.add_argument('--adb', help='adb 可执行文件路径（默认用 PATH 里的 adb，或环境变量 ADB）')
    ap.add_argument('--out', default='capture', help='输出目录，默认 ./capture')
    ap.add_argument('--max-screens', type=int, default=400, help='最大滑动屏数')
    ap.add_argument('--sleep', type=float, default=2.6, help='每屏间隔秒')
    ap.add_argument('--swipe-from', type=float, default=0.74, help='滑动起点（屏高比例）')
    ap.add_argument('--swipe-to', type=float, default=0.29, help='滑动终点（屏高比例）')
    ap.add_argument('--min-len', type=int, default=10, help='条目正文最小长度')
    ap.add_argument('--no-top', action='store_true', help='不回到顶部（从当前位置续跑）')
    ap.add_argument('--no-expand', action='store_true', help='不自动展开页面上的折叠区')
    ap.add_argument('--probe', action='store_true', help='只解析当前一屏，验证环境')
    args = ap.parse_args()

    adb = find_adb(args.adb)
    out = os.path.abspath(args.out)
    c = Crawler(adb, out, args.max_screens, args.sleep,
                args.swipe_from, args.swipe_to, args.min_len,
                expand=not args.no_expand)

    # 连通性检查
    r = c.sh([adb, 'devices'])
    if 'device' not in r.stdout.decode('utf-8', 'ignore'):
        print('未检测到设备。请先用数据线或无线调试连上手机/模拟器，并停在目标列表页（adb:', adb, ')')
        return 2

    if args.probe:
        root = c.dump_ui()
        if root is None:
            print('uiautomator dump 失败：确认模拟器已解锁且停留在评价页')
            return 3
        s, l = c.parse_strict(root), c.parse_loose(root)
        print('环境 OK：严格 %d 条 / 宽松 %d 条' % (len(s), len(l)))
        for it in (s + l)[:5]:
            print('  -', it['user'], '|', it['time'], '|', it['content'][:40])
        return 0

    rows = c.crawl(do_top=not args.no_top)
    final = c.clean(rows)
    json.dump(final, open(os.path.join(out, 'comments_final.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)
    c.write_csv(final, os.path.join(out, 'comments_final.csv'))
    c.write_html(final, os.path.join(out, 'comments_report.html'))
    print('输出目录:', out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
