# -*- coding: utf-8 -*-
"""crawler.py —— Android 列表数据采集主程序（RPA / 无障碍树方案）

在 uiparse.py 的滚动采集之上外挂运行监控，回答一个问题：
    一直按正常速度滚下去，App 会不会开始拦我？如果会，是哪一步触发的？

监控四层：
  1) 每屏扫 UI 里的拦截/异常信号（验证码、操作频繁、网络异常、被踢登录）
  2) 全程抓 logcat，按可配置的关键词过滤安全/风控相关日志
  3) 命中异常时当场取证：截图 + UI dump
  4) 停止时定性：页面不变是"到底"还是"被拦"；逐屏时间线落盘

另外内置：
  * 断点续跑（按滑动次数做锚点，存 progress.json）
  * 启动时目标校验：页面变了自动切到新输出目录，避免两批数据混在一起

用法：
    python crawler.py --out ./capture --max-screens 2000 --sleep 2.6
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')   # Windows 终端默认 GBK，避免中文乱码

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import uiparse as cc  # noqa: E402

ADB_DEFAULT = os.environ.get('ADB', 'adb')

# UI 上的拦截/异常信号。刻意写具体，避免误伤用户内容（"验证"单字太宽，不用）
RISK_UI = re.compile(
    r'拖动滑块|滑块验证|完成验证|安全验证|图形验证|拼图验证|输入验证码'
    r'|操作[过太]于?频繁|请求(过于)?频繁|请稍后再?试'
    r'|账号(?:存在)?异常|账号被限制|限制登录|封禁'
    r'|网络异常|网络连接失败|加载失败'
    r'|登录已过期|重新登录|请先登录'
)

def same_product(a, b):
    """页面名宽松比对。列表页顶部显示的常是「当前选中规格」而非页面名，
    同一目标滚动时会变（如 "A款 蓝色" / "A款 红色"），所以不能精确比。
    判同：完全相同 / 前 3 字相同 / 互相包含 / 有 4 字以上公共子串。"""
    if not a or not b:
        return True                       # 读不到就不判，不误伤
    if a == b or a[:3] == b[:3] or a in b or b in a:
        return True
    return any(a[i:i + 4] in b for i in range(max(0, len(a) - 3)))


def scan_risk_ui(root, max_len=40):
    """扫描屏幕上的风控/异常提示，返回命中的文案或 None。

    只认短文案：实测有条目正文写了"账号被封永久禁言"，被误报成拦截信号过。
    拦截提示（验证码/操作频繁/网络异常）都很短，用户内容都很长。
    """
    for n in root.iter('node'):
        t = (n.get('text') or '').strip()
        if t and len(t) <= max_len and RISK_UI.search(t):
            return t
    return None


# logcat 里安全/风控相关的日志标签（按需替换成目标 App 的关键词）
RISK_LOG = re.compile(
    r'\brisk[_\d]|secsdk|riskstub|antispam|secverify|'
    r'captcha|verify_code|slardar',
    re.I,
)

# 列表页顶部栏的固定文案（按自己的 App 调整）
PAGE_TITLE_SKIP = ('评价', '选款式', '商品评价', '参数')

# 列表到底的文案
END_UI = re.compile(r'没有更多|已?展示全部|到底了|没有更多评论|暂无更多')


class Monitor(cc.Crawler):
    def __init__(self, *a, **kw):
        cc.Crawler.__init__(self, *a, **kw)
        self.timeline = []      # (screen, ts, total, new, risk_hit)
        self.risk_hits = []     # (screen, text)
        self.risk_logs = []     # 命中的 logcat 行
        self.max_comments = None   # 抓够这么多条就停（None=不限）
        self._last_addr = None     # 上次见到的无线设备地址（ip:port），掉线后用来重连
        self.fast_forward_on = True   # 续抓时按屏数断点快速追赶
        self.screen_base = 0          # 上轮累计屏数（断点锚点）
        self._logf = self._logp = None

    # ---------- logcat ----------
    def start_logcat(self):
        self.sh([self.adb, 'logcat', '-c'])
        self._logf = open(os.path.join(self.out_dir, 'logcat_full.txt'), 'w',
                          encoding='utf-8', errors='ignore')
        self._logp = subprocess.Popen(
            [self.adb, 'logcat', '-v', 'time'],
            stdout=self._logf, stderr=subprocess.DEVNULL, env=cc.ENV)

    def stop_logcat(self):
        for obj, closer in ((self._logp, 'terminate'), (self._logf, 'close')):
            try:
                getattr(obj, closer)()
            except Exception:
                pass
        p = os.path.join(self.out_dir, 'logcat_full.txt')
        if os.path.exists(p):
            with open(p, encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if RISK_LOG.search(line):
                        self.risk_logs.append(line.rstrip())

    # ---------- 连接状态 ----------
    def device_online(self):
        """返回 device/unauthorized/offline/none。顺手记住无线设备地址。"""
        out = self.sh([self.adb, 'devices']).stdout.decode('utf-8', 'ignore')
        m = re.search(r'^(\S+)\s+(device|unauthorized|offline)\b', out, re.M)
        if m and ':' in m.group(1):
            self._last_addr = m.group(1)
        return m.group(2) if m else 'none'

    def ensure_device(self, tries=3):
        """设备掉线时尝试自动恢复（重连无线地址 / 重启 adb 服务）。返回是否恢复。"""
        for i in range(tries):
            st = self.device_online()
            if st == 'device':
                return True
            if st == 'unauthorized':
                print('    ! 手机上有授权弹窗，需要手动点「确定」', flush=True)
                return False
            print('    尝试恢复 (%d/%d)，当前: %s' % (i + 1, tries, st), flush=True)
            if self._last_addr:                      # 无线设备：先 disconnect 再 connect
                self.sh([self.adb, 'disconnect', self._last_addr])
                self.sh([self.adb, 'connect', self._last_addr])
                time.sleep(2)
            if st == 'offline':
                self.sh([self.adb, 'reconnect', 'offline'])
                time.sleep(2)
            if self.device_online() != 'device':
                self.sh([self.adb, 'kill-server'])
                time.sleep(1)
                self.sh([self.adb, 'start-server'])
                time.sleep(3)
                if self._last_addr:
                    self.sh([self.adb, 'connect', self._last_addr])
                    time.sleep(2)
        return self.device_online() == 'device'

    def screen_asleep(self):
        """屏幕是否已熄灭 / 已锁屏。真机上这会让 input swipe 完全无效，
        抓取会"每屏一样"然后误判成页面到底 —— 实测踩过一次。"""
        p = self.sh([self.adb, 'shell', 'dumpsys', 'power']).stdout.decode('utf-8', 'ignore')
        w = re.search(r'mWakefulness=(\w+)', p)
        t = self.sh([self.adb, 'shell', 'dumpsys', 'trust']).stdout.decode('utf-8', 'ignore')
        return (w is not None and w.group(1) != 'Awake') or ('deviceLocked=1' in t)

    # ---------- 取证 ----------
    def foreground(self):
        out = self.sh([self.adb, 'shell', 'dumpsys', 'window']).stdout.decode('utf-8', 'ignore')
        m = re.search(r'mCurrentFocus=Window\{[^}]*\}', out)
        return m.group(0) if m else ''

    def snapshot(self, tag):
        png = subprocess.run([self.adb, 'exec-out', 'screencap', '-p'],
                             capture_output=True, env=cc.ENV, timeout=30).stdout
        with open(os.path.join(self.out_dir, 'shot_%s.png' % tag), 'wb') as f:
            f.write(png)
        xml = subprocess.run([self.adb, 'shell', 'cat', '/sdcard/ui.xml'],
                             capture_output=True, env=cc.ENV, timeout=30).stdout
        with open(os.path.join(self.out_dir, 'dump_%s.xml' % tag), 'wb') as f:
            f.write(xml)
        return self.foreground()

    def detect_product(self):
        """从列表页顶部读当前页面名（顶部栏下面那一行）。读不到返回 None。"""
        root = self.dump_ui()
        if root is None:
            return None
        cands = []
        for n in root.iter('node'):
            t = (n.get('text') or '').strip()
            m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', n.get('bounds', ''))
            if not t or not m:
                continue
            y = (int(m.group(2)) + int(m.group(4))) // 2
            if 120 < y < 210 and len(t) >= 2 and t not in PAGE_TITLE_SKIP:
                cands.append(t)
        return max(cands, key=len) if cands else None

    def check_product(self):
        """启动时校验目标：与本目录记录的不同就自动换到新目录，避免污染。

        返回最终使用的输出目录。
        """
        cur = self.detect_product()
        rec_path = os.path.join(self.out_dir, 'product.json')
        rec = None
        if os.path.exists(rec_path):
            try:
                rec = json.load(open(rec_path, encoding='utf-8')).get('product')
            except Exception:
                rec = None

        if not cur:
            print('! 读不到页面名（可能不在列表页），跳过校验', flush=True)
        elif not rec:
            if os.path.exists(os.path.join(self.out_dir, 'comments_raw.json')):
                print('! 本目录已有数据但没有页面记录，跳过校验（建议手工确认）', flush=True)
            else:
                json.dump({'product': cur}, open(rec_path, 'w', encoding='utf-8'), ensure_ascii=False)
                print('页面记录: %s' % cur, flush=True)
        elif not same_product(rec, cur):
            safe = re.sub(r'[^\w一-鿿]', '', cur)[:24] or 'new'
            new_dir = '%s__%s' % (self.out_dir, safe)
            print('!! 目标变了：本目录记的是「%s」，当前页面是「%s」' % (rec, cur), flush=True)
            print('!! 为避免污染，自动改用新目录: %s' % new_dir, flush=True)
            self.out_dir = new_dir
            os.makedirs(new_dir, exist_ok=True)
            json.dump({'product': cur}, open(os.path.join(new_dir, 'product.json'), 'w',
                                             encoding='utf-8'), ensure_ascii=False)
        else:
            print('页面核对通过: %s' % cur, flush=True)
        return self.out_dir

    def load_progress(self):
        """读累计屏数（上一轮滑到第几屏 = 断点锚点）。"""
        p = os.path.join(self.out_dir, 'progress.json')
        if os.path.exists(p):
            try:
                return int(json.load(open(p, encoding='utf-8')).get('screens', 0))
            except Exception:
                return 0
        return 0

    def save_progress(self, screens):
        p = os.path.join(self.out_dir, 'progress.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'screens': screens, 'updated': time.strftime('%Y-%m-%d %H:%M:%S')},
                      f, ensure_ascii=False)

    def swipe_ratio(self, y1, y2, dur=500):
        x = self.w // 2
        self.sh([self.adb, 'shell', 'input', 'swipe', str(x), str(int(self.h * y1)),
                 str(x), str(int(self.h * y2)), str(dur)])

    def fast_forward(self, store, margin=None):
        """回放到上一轮的**屏数断点**，再靠内容确认边界。

        锚点用屏数（每滑一次 = 1 屏，累计值存 progress.json），不看页面位置。
        续抓时：先大距离快滑抢回大部分屏数，剩 margin 屏改用常规步长逐屏内容确认，
        看到新评论才转入正常抓取——这样即使快滑的跨度估算不准也不会漏掉边界评论。

        余量 margin 跟着断点大小走（断点的 1/4，8~40 屏之间）：
        断点只有 20 屏时若还用固定 40 屏余量，就变成"快滑 0 屏 + 确认 40 屏"，反而更慢。
        """
        prev = self.screen_base
        if prev <= 0:
            print('无屏数断点（首次抓取），直接开始', flush=True)
            return
        if margin is None:
            margin = min(40, max(8, prev // 4))
        known = set(store.keys())
        target = max(0, prev - margin)
        print('快速追赶：断点在第 %d 屏 —— 先大距离快滑 %d 屏，再逐屏确认 %d 屏'
              % (prev, target, margin), flush=True)

        # 阶段1：**用与抓取时完全相同的滑动**回放 prev 屏。
        # 滑动一次的距离固定，所以"回放 N 次"就等于回到第 N 屏，不用猜跨度。
        # 真正省下的时间只有一个来源：回放时**不解析**——
        # 跳过每屏 2.5 秒的 uiautomator dump，只滑 + 等加载。
        x = self.w // 2
        y1, y2 = int(self.h * self.swipe_from), int(self.h * self.swipe_to)
        done = 0
        while done < target:
            self.sh([self.adb, 'shell', 'input', 'swipe',
                     str(x), str(y1), str(x), str(y2), '600'])     # 与抓取同一手势
            done += 1
            time.sleep(1.2)                                        # 留出内容加载时间
            if done % 30 == 0:                                     # 每 30 屏校验一次
                root = self.dump_ui()
                if root is None:
                    if not self.ensure_device():
                        break
                    continue
                items = self.parse_strict(root) + self.parse_loose(root)
                unknown = [it for it in items if it['content'][:40] not in known]
                if len(unknown) >= 2 or (items and len(unknown) / len(items) > 0.4):
                    print('  回放中撞到新内容（本屏 %d 条未知）→ 转逐屏确认' % len(unknown), flush=True)
                    break
                print('  回放中… %d/%d 屏' % (min(done, target), target), flush=True)

        # 阶段2：常规步长逐屏确认，撞到新内容即回退一小段
        for _ in range(margin * 2):
            root = self.dump_ui()
            if root is None:
                if not self.ensure_device():
                    break
                continue
            items = self.parse_strict(root) + self.parse_loose(root)
            unknown = [it for it in items if it['content'][:40] not in known]
            if len(unknown) >= 2 or (items and len(unknown) / len(items) > 0.4):
                print('  确认追上（本屏 %d 条未知）→ 回退 12 屏后正常抓取' % len(unknown), flush=True)
                for _ in range(12):
                    self.swipe_ratio(0.15, 0.85, 300)
                    time.sleep(0.25)
                return
            self.swipe_ratio(0.74, 0.29, 600)
            time.sleep(0.8)
        print('  逐屏确认跑完仍未遇新内容，直接转入正常抓取', flush=True)

    def fast_forward_burst(self, store, burst=4):
        """按**评论内容**快速追赶已抓过的区域，不依赖页面位置。

        已抓过的评论做"已知集合"。为了绕开"每屏一次 dump 要 2.5 秒"这个瓶颈，
        这里**盲滑 burst 屏再采一次**：采到的若还是已知内容就继续盲滑，
        一旦出现未知内容（判定阈值见下），回退 burst+2 屏再交给正常逐屏抓取，
        保证跳太快也不会漏掉边界上的评论。返回大约跳过的屏数。
        """
        known = set(store.keys())
        back = burst + 2
        print('快速追赶：已抓 %d 条做锚点，每次盲滑 %d 屏…' % (len(known), burst), flush=True)
        loops = 0
        while loops < 300:                          # 上限，防死循环（300*4=1200 屏）
            loops += 1
            for _ in range(burst):                  # 盲滑一段，不求内容
                self.swipe_ratio(0.88, 0.12, 280)
                time.sleep(0.30)
            root = self.dump_ui()                   # 再采一次，看追到哪了
            if root is None:
                if not self.ensure_device():
                    break
                continue
            if loops % 10 == 0:
                print('  追赶中… 已盲滑约 %d 屏' % (loops * burst), flush=True)
            items = self.parse_strict(root) + self.parse_loose(root)
            unknown = [it for it in items if it['content'][:40] not in known]
            # 单条未知多半是页面噪声混入（实测被 "900+达人推荐" 骗停过一次），
            # 要求 ≥2 条未知、或未知占比 >40%，才算真进入新区域。
            is_new = len(unknown) >= 2 or (items and len(unknown) / len(items) > 0.4)
            if is_new or not items:
                print('  追上进度（本屏 %d/%d 条未知）→ 回退 %d 屏后正常抓取'
                      % (len(unknown), len(items), back), flush=True)
                for _ in range(back):
                    self.swipe_ratio(0.15, 0.85, 300)
                    time.sleep(0.25)
                return loops * burst
        print('  追赶达到上限，转入正常抓取', flush=True)
        return loops * burst

    def save_raw(self, store):
        """增量落盘，保证中途被打断也能续抓。"""
        p = os.path.join(self.out_dir, 'comments_raw.json')
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(list(store.values()), f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)

    # ---------- 主循环 ----------
    def run(self, do_top=True):
        self.check_product()          # 目标校验要在读历史之前：换目标会改 out_dir
        store = {}
        raw_path = os.path.join(self.out_dir, 'comments_raw.json')
        if os.path.exists(raw_path):
            for it in json.load(open(raw_path, encoding='utf-8')):
                if it.get('content'):
                    store[it['content'][:40]] = it
            print('已载入历史 %d 条（本次续抓）' % len(store), flush=True)

        self.screen_base = self.load_progress()
        if self.screen_base:
            print('屏数断点: 第 %d 屏' % self.screen_base, flush=True)

        if self.screen_asleep():
            print('!! 警告：手机已熄屏或锁屏 —— 滑动会完全无效，抓取将毫无进展。请先唤醒解锁。', flush=True)

        if do_top:
            self.back_to_top()
            print('已回到列表顶部', flush=True)
        if store and self.fast_forward_on:
            self.fast_forward(store)
        print('开始监控抓取', flush=True)
        self.start_logcat()

        screen, last_sig, same, fails = 0, None, 0, 0
        stop_reason = 'max_screens'
        try:
            while screen < self.max_screens:
                root = self.dump_ui()
                screen += 1
                if root is None:
                    fails += 1
                    print('  [%d] dump 失败 (%d/5)' % (screen, fails), flush=True)
                    if fails >= 3:
                        if self.ensure_device():
                            print('  [%d] 设备已恢复，继续' % screen, flush=True)
                            fails = 0
                            continue
                        stop_reason = 'device_lost'
                        break
                    time.sleep(1.5)
                    continue
                fails = 0

                # 1) 风控 UI 扫描（每屏都扫）
                hit = scan_risk_ui(root) or ''
                if hit:
                    self.risk_hits.append((screen, hit))
                    print('  [%d] !! 风控/异常界面: %s' % (screen, hit[:70]), flush=True)
                    self.snapshot('risk_%d' % screen)

                # 2) 到底判定：连续两次整屏完全一致
                sig = self.signature(root)
                if sig == last_sig:
                    same += 1
                    if same >= 2:
                        stop_reason = 'page_stalled'
                        break
                else:
                    same, last_sig = 0, sig

                # 3) 解析累积
                if self.expand and self.tap_folded(root):
                    root = self.dump_ui() or root
                strict = self.parse_strict(root)
                loose = self.parse_loose(root)
                added = []
                for it in strict + loose:
                    key = it['content'][:40]
                    if self.add(store, it):
                        added.append(store[key])
                new = len(added)
                self.timeline.append((screen, round(time.time(), 1), len(store), new, hit))
                print('  [%d] 严格%d/宽松%d 新增%d 累计%d'
                      % (screen, len(strict), len(loose), new, len(store)), flush=True)
                for rec in added:                        # 实时打印每条新评论
                    print('      + [%-9s|%-9s] %s' % (
                        rec.get('user') or '-', rec.get('time') or '-',
                        rec['content'].replace('\n', ' ')[:70]), flush=True)

                if screen % 10 == 0:                    # 每 10 屏增量落盘，可断点续抓
                    self.save_raw(store)
                    self.save_progress(self.screen_base + screen)   # 屏数锚点

                if self.max_comments and len(store) >= self.max_comments:
                    stop_reason = 'target_reached'
                    break

                # 4) 翻页
                self.swipe_up()
                time.sleep(self.sleep)
        finally:
            fg = self.snapshot('stop')
            self.stop_logcat()

        # ---------- 停止原因定性 ----------
        stop_xml = ''
        p = os.path.join(self.out_dir, 'dump_stop.xml')
        if os.path.exists(p):
            stop_xml = open(p, encoding='utf-8', errors='ignore').read()

        if stop_reason == 'page_stalled':
            if self.screen_asleep():
                verdict = '页面停住 —— 但手机已熄屏/锁屏，滑动无效（不是风控）'
            elif RISK_UI.search(stop_xml):
                verdict = '页面停住，且停止时屏幕上仍有风控/异常文案 -> 疑似被拦'
            elif END_UI.search(stop_xml):
                verdict = '页面停住，且出现"没有更多" -> 评论到底（自然结束）'
            else:
                verdict = '页面停住，未见风控文案也未见"没有更多" -> 疑似静默限流/加载卡住'
        elif stop_reason == 'target_reached':
            verdict = '抓够目标条数后主动停止（非风控、非到底）'
        elif stop_reason == 'device_lost':
            verdict = '设备掉线（offline/unauthorized），抓取中断 -- 检查线与授权弹窗'
        else:
            verdict = '达到屏数上限，尚未到底'

        # ---------- 输出数据 ----------
        rows = list(store.values())
        json.dump(rows, open(raw_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        final = self.clean(rows)
        json.dump(final, open(os.path.join(self.out_dir, 'comments_final.json'), 'w',
                              encoding='utf-8'), ensure_ascii=False, indent=2)
        self.write_csv(final, os.path.join(self.out_dir, 'comments_final.csv'))
        self.write_html(final, os.path.join(self.out_dir, 'comments_report.html'))

        # ---------- 时间线 ----------
        with open(os.path.join(self.out_dir, 'timeline.csv'), 'w',
                  encoding='utf-8-sig', newline='') as f:
            import csv as _csv
            w = _csv.writer(f)
            w.writerow(['screen', 'unix_ts', 'total_comments', 'new_this_screen', 'risk_hit'])
            w.writerows(self.timeline)

        # ---------- 风控报告 ----------
        report = {
            '停止原因': stop_reason,
            '定性': verdict,
            '共滑屏数': screen,
            '停止时前台': fg,
            '最终评论数': len(final),
            '风控UI命中': self.risk_hits[:50],
            '风控日志命中数': len(self.risk_logs),
            '风控日志样例': self.risk_logs[:30],
        }
        json.dump(report, open(os.path.join(self.out_dir, 'risk_report.json'), 'w',
                               encoding='utf-8'), ensure_ascii=False, indent=2)

        print('\n===== 抓取结论 =====', flush=True)
        print('滑屏 %d 屏，最终 %d 条评论' % (screen, len(final)), flush=True)
        print('停止原因: %s' % stop_reason, flush=True)
        print('定性    : %s' % verdict, flush=True)
        print('停止前台: %s' % fg, flush=True)
        print('风控UI命中: %d 次' % len(self.risk_hits), flush=True)
        for s, t in self.risk_hits[:10]:
            print('   [第%d屏] %s' % (s, t[:60]), flush=True)
        print('风控日志命中: %d 行' % len(self.risk_logs), flush=True)
        for line in self.risk_logs[:8]:
            print('   ' + line[:110], flush=True)
        print('输出目录: %s' % self.out_dir, flush=True)
        return final


def main():
    ap = argparse.ArgumentParser(description='Android 列表数据采集（RPA / 无障碍树 + 运行监控）')
    ap.add_argument('--adb', default=None)
    ap.add_argument('--out', default='capture_phone')
    ap.add_argument('--max-screens', type=int, default=100000, help='安全上限，默认几乎不限')
    ap.add_argument('--max-comments', type=int, default=None, help='采够这么多条就停（不给=不限）')
    ap.add_argument('--sleep', type=float, default=2.6)
    ap.add_argument('--no-top', action='store_true', help='不从顶部开始，从当前位置续采（断点续采用）')
    ap.add_argument('--no-ff', action='store_true', help='禁用"回放追进度"（续采时默认开启）')
    args = ap.parse_args()

    adb = cc.find_adb(args.adb or ADB_DEFAULT)
    out = os.path.abspath(args.out)
    m = Monitor(adb, out, max_screens=args.max_screens, sleep=args.sleep)
    m.max_comments = args.max_comments
    m.fast_forward_on = not args.no_ff

    state = m.device_online()
    if state != 'device':
        print('设备状态异常: %s' % state, flush=True)
        if state == 'unauthorized':
            print('-> 手机上有「允许 USB 调试吗」弹窗，请点确定', flush=True)
        elif state == 'offline':
            print('-> 设备 offline，试 adb reconnect offline 或拔插数据线', flush=True)
        else:
            print('-> 未检测到设备', flush=True)
        return 2
    print('设备就绪: %s' % state, flush=True)

    m.run(do_top=not args.no_top)
    return 0


if __name__ == '__main__':
    sys.exit(main())
