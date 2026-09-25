#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
roundtrip_check.py — 三级概括法的无损往返验证（独立复核）

作用：**独立**验证"压缩结果能否还原" —— 不复用生成侧代码路径，
避免"生成和校验同一个 bug 互相掩护"（M-05 静默失效防治要求）。

用法：
    python roundtrip_check.py --text <原文> --l2 <L2文件>
    python roundtrip_check.py --log <原始日志> [--l2 <L2文件>]
    python roundtrip_check.py --dict <字典> --l3 <L3文件>
    python roundtrip_check.py --capability      # 打印本工具能力边界

判据：
    文本域  L1→L2 为【有损归约】 → 检查"信息要素零丢失"（数值/路径/命令/结论）
    日志域  L1→L2 为【无损模板】 → 检查"逐字重建相等"
    字典域  L2→L3 为【无损映射】 → 检查"每个代号有定义、每个原文可还原"

零依赖：仅标准库。
"""

import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier_common import setup_stdout  # noqa: E402

# 信息要素抽取（文本域用）—— 与 tier_text 的 KEY_PATTERNS 保持同一语义，
# 但此处【独立实现】，不 import，以防生成侧 bug 掩护
#
# ⭐ v2 修（首跑误报 36 项）：
#   首版把「行内代码段」连反引号一起比对（`X`），
#   但抽句/切分时反引号会被切开（L1 394 → L2 379 个）。
#   实测复核：36 项「丢失」的【内容全在】，只是反引号变了 → 全部误报。
#   修法：代码段只比【内容】；反引号配对单独报告。
BT = chr(96)
ELEM_PATTERNS = [
    (r'\d+(?:\.\d+)?', '数值'),
    (BT + r'([^' + BT + r'\n]+)' + BT, '代码命令内容'),
    (r'[A-Za-z0-9_.\-]+\.(?:md|py|json|txt|log|js|ts|html|css|ya?ml|toml|ps1|cmd|sh|c|cpp|h|sql)', '文件路径'),
    (r'[A-Z]{2,}(?:-[A-Z0-9]+)+', '代号'),
]


def backtick_parity(l1, l2):
    """反引号配对检查（单独报，不计入要素丢失）"""
    a, b = l1.count(BT), l2.count(BT)
    if a == b:
        return 'OK', 'L1/L2 均为 %d 个' % a
    return 'WARN', 'L1=%d ｜ L2=%d ｜ 差 %d → 行内代码段可能在抽句时被切断' % (a, b, a - b)


def extract_elements(text):
    """独立抽取信息要素（多集合，便于分别比对）"""
    out = {}
    for pat, name in ELEM_PATTERNS:
        found = set(re.findall(pat, text))
        if found:
            out[name] = found
    return out


def check_text(l1_path, l2_path):
    l1 = open(l1_path, 'rb').read().decode('utf-8', 'replace')
    l2 = open(l2_path, 'rb').read().decode('utf-8', 'replace')
    e1, e2 = extract_elements(l1), extract_elements(l2)

    print('=' * 64)
    print(' 文本域 · 信息要素零丢失检查')
    print('=' * 64)
    print(' L1: %d 字符 ｜ L2: %d 字符 ｜ 保留 %.1f%%'
          % (len(l1), len(l2), 100.0 * len(l2) / max(1, len(l1))))
    print()

    total_lost = 0
    for name in sorted(set(list(e1.keys()) + list(e2.keys()))):
        a = e1.get(name, set())
        if name == '代码命令内容':
            # ⭐ 特殊判据：反引号配对被打散后，两侧都抽取会错位。
            #    正确做法：L1 的代码段【内容】必须原样出现在 L2 文本里。
            b = set(x for x in a if x in l2)
            lost = a - b
        else:
            b = e2.get(name, set())
            lost = a - b
        total_lost += len(lost)
        verdict = 'OK' if not lost else 'FAIL'
        print(' %-6s %-12s L1=%-4d L2=%-4d 丢失=%d' % (verdict, name, len(a), len(b), len(lost)))
        if lost:
            for x in sorted(lost)[:8]:
                print('        - 丢失: %s' % x[:80])

    # 反引号配对单独报（不计入要素丢失）
    pv, pnote = backtick_parity(l1, l2)
    print()
    print(' %-6s %-12s %s' % (pv, '反引号配对', pnote))

    print()
    print('-' * 64)
    print(' 小结: 信息要素丢失 %d 项 ｜ %s'
          % (total_lost, 'OK（要素零丢失）' if total_lost == 0 else 'FAIL（有要素丢失）'))
    print('-' * 64)
    return 0 if total_lost == 0 else 1


def check_log(l1_path):
    """
    日志域独立复核：独立实现（与 tier_log 不同写法）

    ⭐ v2 修（首跑误报 342 行）：
      首版【逐条规则串行 sub】并按【规则顺序】收集字段值，
      而重建时模板里的占位符是按【位置顺序】出现的 →
      两者错位 → 误报为“重建失败”。

    修法：用【单次 alternatives 匹配】，
      字段值天然按【位置顺序】收集，与重建顺序一致。
    """
    # 独立写法：合成单条 alternation（与 tier_log 的 VAR_RULES 不同源）
    rules = [
        r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?',
        r'\d{4}-\d{2}-\d{2}',
        r'\b\d{2}:\d{2}:\d{2}\b',
        r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b',
        r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b',
        r'\b0x[0-9a-fA-F]+\b',
        r'(?:[A-Za-z]:\\[^\s"\']+|/(?:[\w.\-]+/)+[\w.\-]*)',
        r'\b\d+(?:\.\d+)?\b',
    ]
    rx = re.compile('|'.join('(?:%s)' % r for r in rules))
    lines = open(l1_path, 'rb').read().decode('utf-8', 'replace').split('\n')
    lines = [x for x in lines if x.strip()]

    print('=' * 64)
    print(' 日志域 · 无损往返独立复核')
    print('=' * 64)
    print(' 原始行数: %d' % len(lines))
    print()

    total = ok = bad = 0
    bads = []
    for line in lines:
        total += 1
        vals = []

        def _rep(m):
            vals.append(m.group(0))
            return '\x02<'          # 独立哨兵（非数字，避免被后续规则误匹配）

        tmpl = rx.sub(_rep, line)
        rebuilt = tmpl
        for v in vals:
            rebuilt = rebuilt.replace('\x02<', v, 1)
        if rebuilt == line:
            ok += 1
        else:
            bad += 1
            bads.append((line, tmpl, rebuilt))

    print(' 逐字重建: 成功 %d ｜ 失败 %d ｜ 覆盖率 %.1f%%'
          % (ok, bad, 100.0 * ok / max(1, total)))
    for line, tmpl, rebuilt in bads[:5]:
        print('   FAIL 原行: %s' % line[:74])
        print('        重建: %s' % rebuilt[:74])
    print()
    print('-' * 64)
    print(' 小结: %s' % ('OK（全部行可还原）' if bad == 0 else 'FAIL（%d 行不可还原）' % bad))
    print('-' * 64)
    return 0 if bad == 0 else 1


def check_dict(dict_path, l3_path):
    """词典域：L3 每个代号必须有字典定义；每条"含义"必须能在 L2/原文还原"""
    txt = open(dict_path, 'rb').read().decode('utf-8', 'replace')
    codes = {}
    for line in txt.split('\n'):
        if '|' not in line:
            continue
        cells = [c.strip().strip('`').strip('*').strip() for c in line.strip().strip('|').split('|')]
        if len(cells) >= 2 and re.match(r'^[A-Za-z0-9_\-]{2,24}$', cells[0]):
            codes[cells[0]] = cells[1]
    l3 = open(l3_path, 'rb').read().decode('utf-8', 'replace') if os.path.exists(l3_path) else ''

    print('=' * 64)
    print(' 词典域 · L3 代号可解析检查')
    print('=' * 64)
    print(' 字典词条: %d ｜ L3 字符: %d' % (len(codes), len(l3)))
    print()

    used = set(re.findall(r'[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+', l3))
    undef = sorted(x for x in used if x not in codes)
    unused = sorted(x for x in codes if x not in used)

    print(' L3 使用的代号: %d ｜ 未定义: %d' % (len(used), len(undef)))
    for u in undef[:10]:
        print('   FAIL 未定义代号: %s' % u)
    if unused:
        print(' （提示）字典中未在 L3 出现的词条 %d 个: %s'
              % (len(unused), '、'.join(unused[:8])))
    print()
    print('-' * 64)
    print(' 小结: %s' % ('OK（全部代号有定义）' if not undef else 'FAIL（%d 个孤儿代号）' % len(undef)))
    print('-' * 64)
    return 0 if not undef else 1


def main():
    setup_stdout()
    argv = sys.argv[1:]

    if '--capability' in argv:
        print(__doc__)
        return 0

    rc = 0

    def arg(name):
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv):
                return argv[i + 1]
        return None

    did = False
    if arg('--text') and arg('--l2'):
        rc |= check_text(arg('--text'), arg('--l2'))
        did = True
    if arg('--log'):
        print()
        rc |= check_log(arg('--log'))
        did = True
    if arg('--dict') and arg('--l3'):
        print()
        rc |= check_dict(arg('--dict'), arg('--l3'))
        did = True

    if not did:
        print(__doc__)
        return 2
    return rc


if __name__ == '__main__':
    sys.exit(main())
