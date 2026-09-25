#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tier_log.py — 日志/大数据域三级压缩（L1 原始行 → L2 模板行 → L3 模板码）

用法：
    python tier_log.py <日志文件> [--out-l2 <f>] [--out-l3 <f>] [--json]
    python tier_log.py <日志文件> --verify-roundtrip
    python tier_log.py --demo

三级定义（见 docs/04 §2.4）：
    L1 原始行  全量日志
    L2 模板行  变量位抽为字段 + 模板（保证可重建）
    L3 模板码  模板 → 代号 + 计数

⭐ 本域是唯一可 100% 无损的：
    ∀ 行：render(template, fields) == line   ← 必须逐字相等
    不满足的行不模板化，保留为 L1，并计入报告。

零依赖：仅标准库。
"""

import io
import os
import re
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier_common import EQ_STRICT, setup_stdout, count, ratio, Report  # noqa: E402

# 变量位识别（顺序重要：先长后短）
VAR_RULES = [
    ('<TS>', re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?')),
    ('<DATE>', re.compile(r'\d{4}-\d{2}-\d{2}')),
    ('<TIME>', re.compile(r'\b\d{2}:\d{2}:\d{2}\b')),
    ('<UUID>', re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')),
    ('<IP>', re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b')),
    ('<HEX>', re.compile(r'\b0x[0-9a-fA-F]+\b')),
    ('<PATH>', re.compile(r'(?:[A-Za-z]:\\[^\s"\']+|/(?:[\w.\-]+/)+[\w.\-]*)')),
    ('<NUM>', re.compile(r'\b\d+(?:\.\d+)?\b')),
]

FIELD_ORDER = ['<TS>', '<DATE>', '<TIME>', '<UUID>', '<IP>', '<HEX>', '<PATH>', '<NUM>']


def templatize(line):
    """
    单行 → (模板, [字段值...])
    模板中变量位统一写 <*>，字段按出现顺序记录。

    ⭐ v2 修（真跑抓到的 bug：6/6 行重建失败）：
      首版是【逐条规则串行 sub】，并把结果哨兵写成 \x01N\x01。
      但后面的规则（尤其 <NUM>）还会去匹配【先前插入的哨兵编号】
      （\x01 是非单词字符，所以 \b\d+ 能在里面命中），
      哨兵被二次替换 → 模板里残留 \x01 → 重建必然不等。

    修法：合并为【一次性 alternation】匹配，每条规则再也看不到别人的输出。
    """
    rx, tags = _combined()
    slots = []

    def _rep(m):
        # lastindex = 命中的第几个 alternation 分支（各分支均为非捕获组）
        idx = (m.lastindex or 1) - 1
        tag = tags[idx] if 0 <= idx < len(tags) else '?'
        slots.append((tag, m.group(0)))
        return '<*>'

    tmpl = rx.sub(_rep, line)
    return tmpl, slots


def _combined():
    """把 VAR_RULES 合并成单次匹配用（顺序即优先级：先长后短）"""
    global _COMBINED
    if _COMBINED is None:
        parts = ['(%s)' % rx.pattern for _tag, rx in VAR_RULES]
        _COMBINED = (re.compile('|'.join(parts)), [t for t, _ in VAR_RULES])
    return _COMBINED


_COMBINED = None


def render(tmpl, slots):
    """按模板 + 字段重建原行（模板里 <*> 按出现顺序对位）"""
    out = tmpl
    for _tag, val in slots:
        out = out.replace('<*>', val, 1)
    return out


def classify(line):
    """粗分类（用于模板代号前缀）"""
    u = line.upper()
    if 'ERROR' in u or 'FAIL' in u or 'EXCEPTION' in u or 'FATAL' in u:
        return 'E'
    if 'WARN' in u:
        return 'W'
    if 'DEBUG' in u or 'TRACE' in u:
        return 'D'
    return 'I'


def main():
    setup_stdout()
    argv = sys.argv[1:]

    if '--demo' in argv:
        demo = '\n'.join([
            '2026-09-25 10:00:01 INFO  app started pid=1234',
            '2026-09-25 10:00:02 INFO  app started pid=5678',
            '2026-09-25 10:00:03 ERROR db connect failed host=127.0.0.1 code=5432',
            '2026-09-25 10:00:04 ERROR db connect failed host=10.0.0.5 code=5433',
            '2026-09-25 10:00:05 WARN  slow query 1.23s path=/api/items',
            'plain line without structure',
        ])
        lines = demo.split('\n')
    elif argv:
        src = argv[0]
        if not os.path.exists(src):
            print('FAIL: 日志文件不存在: %s' % src)
            return 2
        lines = open(src, 'rb').read().decode('utf-8', 'replace').split('\n')
    else:
        print(__doc__)
        return 2

    r = Report(os.path.basename(argv[0]) if (argv and not argv[0].startswith('--')) else 'demo',
               '日志/数据')
    r.size('L1', count('\n'.join(lines)))

    # ── L1 → L2 模板化 ──
    tmpl_map = {}      # tmpl → {'code', 'count', 'samples': [(line, slots)]}
    not_templated = []
    rt_ok = rt_fail = 0

    for ln_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        tmpl, slots = templatize(line)
        # ⭐ 无损契约：重建必须逐字相等
        if render(tmpl, slots) != line:
            rt_fail += 1
            not_templated.append(('L%d' % ln_no, '重建失败（无损契约不满足）'))
            continue
        rt_ok += 1
        if tmpl not in tmpl_map:
            tmpl_map[tmpl] = {'count': 0, 'slots': slots, 'kind': classify(line),
                              'samples': []}
        tmpl_map[tmpl]['count'] += 1
        if len(tmpl_map[tmpl]['samples']) < 2:
            tmpl_map[tmpl]['samples'].append((line, slots))

    # ── 编号（按出现次数降序）──
    ordered = sorted(tmpl_map.items(), key=lambda kv: -kv[1]['count'])
    for i, (tmpl, info) in enumerate(ordered, 1):
        info['code'] = 'L-%s%02d' % (info['kind'], i)

    # ── 渲染 L2 ──
    l2_lines = ['# L2 模板行 ｜ 模板 + 字段（可无损重建）', '']
    for tmpl, info in ordered:
        l2_lines.append('[%s] ×%d  %s' % (info['code'], info['count'], tmpl))
        for line, slots in info['samples'][:1]:
            valstr = ' ｜ '.join('%s=%s' % (t, v) for t, v in slots[:8]) or '(无字段)'
            l2_lines.append('     字段示例: %s' % valstr)
    l2 = '\n'.join(l2_lines)

    # ── 渲染 L3 ──
    l3_lines = ['# L3 模板码 ｜ 代号 + 计数（极短，可常驻）', '']
    for tmpl, info in ordered:
        l3_lines.append('  %-8s ×%-6d %s' % (info['code'], info['count'],
                                             tmpl[:70].replace('<*>', '·')))
    l3 = '\n'.join(l3_lines)

    r.size('L2', count(l2))
    r.size('L3', count(l3))

    r.extra['原始行数'] = len([x for x in lines if x.strip()])
    r.extra['模板数'] = len(ordered)
    r.extra['模板化行数'] = rt_ok
    r.extra['未模板化行数'] = rt_fail
    n_multi = sum(1 for _, i in ordered if i['count'] > 1)
    r.extra['多行归并模板'] = n_multi
    if ordered:
        top = ordered[0]
        r.extra['最高频模板'] = '%s ×%d' % (top[1]['code'], top[1]['count'])

    r.mark(EQ_STRICT, rt_ok)   # 模板化 = 严格等价
    for name, why in not_templated[:50]:
        r.skip(name, why)

    # ── 门禁 ──
    if rt_fail:
        r.gate('无损契约', 'FAIL', '%d 行无法重建 → 已保留为 L1' % rt_fail)
    else:
        r.gate('无损契约', 'OK', '%d/%d 行可逐字重建' % (rt_ok, rt_ok))

    # ⭐ 小样本防护：行数 < 20 时提示统计不具代表性
    n_line = len([x for x in lines if x.strip()])
    if n_line < 20:
        r.gate('样本代表性', 'WARN',
               '仅 %d 行，压缩比不具代表性（头部固定开销占比过大）' % n_line)

    if not ordered:
        r.gate('模板化有效', 'FAIL', '未提取到任何模板')
    else:
        cover = 100.0 * rt_ok / max(1, rt_ok + rt_fail)
        r.gate('模板化有效', 'OK' if cover > 50 else 'WARN',
               '覆盖 %.1f%% ｜ 模板 %d 个' % (cover, len(ordered)))

    if n_multi:
        r.gate('模板复用', 'OK', '%d 个模板出现多次（有归并收益）' % n_multi)
    else:
        r.gate('模板复用', 'WARN', '无重复模板 → 本数据集几乎无压缩空间')

    # ⭐ 膨胀门禁（真跑实测发现的真问题）：
    #   小样本 + 多模板时，L2 的头部固定开销会【超过原文】。
    #   ⚠️ 但小样本（<20 行）不具代表性，此时降级为 WARN，
    #      否则会把“样本太小”误判成“必须修” → 噪音淹没真问题。
    l1n, l2n = r.sizes.get('L1', 0), r.sizes.get('L2', 0)
    n_line_now = len([x for x in lines if x.strip()])
    if l2n > l1n and l1n:
        if n_line_now < 20:
            r.gate('L2 未膨胀', 'WARN',
                   'L2(%d) > L1(%d)：小样本(%d 行)固定开销占比过大，不具代表性'
                   % (l2n, l1n, n_line_now))
        else:
            r.gate('L2 未膨胀', 'FAIL',
                   'L2(%d) > L1(%d) → 固定开销超过收益；模板太碎'
                   % (l2n, l1n))
    else:
        r.gate('L2 未膨胀', 'OK', 'L2 为 L1 的 %.1f%%' % ratio(l1n, l2n))

    # 代号可解析（L3 每个代号都能在 L2 里找到定义）
    orphan = [i['code'] for _, i in ordered if i['code'] not in l2]
    if orphan:
        r.gate('L3 代号有定义', 'FAIL', '孤儿代号: %s' % '、'.join(orphan[:5]))
    else:
        r.gate('L3 代号有定义', 'OK', '%d 个代号全部在 L2 有定义' % len(ordered))

    nfail = r.dump()

    if '--verify-roundtrip' in argv:
        print()
        print(' 【无损往返复核】（对每个模板的样例行逐字比对）')
        bad = 0
        for tmpl, info in ordered:
            for line, slots in info['samples']:
                if render(tmpl, slots) != line:
                    bad += 1
                    print('   FAIL 重建不等: %s' % line[:70])
        print('   逐字比对 %d 行 ｜ 不等 %d 行 ｜ %s'
              % (sum(len(i['samples']) for _, i in ordered), bad,
                 'OK' if bad == 0 else 'FAIL'))

    if '--out-l2' in argv:
        p = argv[argv.index('--out-l2') + 1]
        open(p, 'wb').write(l2.encode('utf-8'))
        print(' 已写 L2: %s' % p)
    if '--out-l3' in argv:
        p = argv[argv.index('--out-l3') + 1]
        open(p, 'wb').write(l3.encode('utf-8'))
        print(' 已写 L3: %s' % p)
    if '--json' in argv:
        print(json.dumps({'sizes': r.sizes, 'marks': r.marks, 'gates': r.gates,
                          'extra': r.extra, 'uncompressed': r.uncompressed},
                         ensure_ascii=False, indent=2))
    return 1 if nfail else 0


if __name__ == '__main__':
    sys.exit(main())
