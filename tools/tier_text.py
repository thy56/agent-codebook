#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tier_text.py — 文本域三级概括（L1 完整 → L2 精简 → L3 关键词码）

用法：
    python tier_text.py <输入文件> [--dict <字典文件>] [--report-json <输出>]
    python tier_text.py <输入文件> --out-l2 <文件> --out-l3 <文件>
    python tier_text.py --demo

三级定义（见 docs/04 §2.2）：
    L1  完整句     原文
    L2  精简句     删套话 + 删程度修饰 + 去重 + 抽关键句
    L3  关键词码   把已登记的关键概念替换为代号

损失标记（docs/04 §三）：
    删套话/去重      → ≈ 语义等价
    删程度修饰       → ⊃修饰
    抽关键句丢句     → ⊃举例 / ⊃推导

零依赖：仅标准库。
"""

import io
import os
import re
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier_common import (  # noqa: E402
    EQ_SEM, LOSS, DEGREE_WORDS, FILLER_PHRASES,
    setup_stdout, count, ratio, mask_protect, unmask,
    split_sentences, sentence_verdict, Report,
)


def l2_reduce(text):
    """
    L1 → L2：返回 (l2文本, 统计)

    顺序铁律（v2 修）：
      ① 先掩保护词（否则「仅/至少」这类限界词会被当修饰删掉）
      ② 先对【原句】判留/丢 —— 再删套话
         ⚠️ 首版 bug：先删套话会把「简单来说，」这类
            标识别词一起删掉，导致后面的丢弃规则失配、该丢的句子留下来。
    """
    st = {'filler': 0, 'degree': 0, 'dedup': 0,
          'drop_举例': 0, 'drop_推导': 0, 'drop_样式': 0, 'kept_sent': 0}

    # ① 掩保护词
    masked, table = mask_protect(text)

    # ② 按【原句】判留/丢，再对保留句做删减
    kept = []
    for s in split_sentences(masked):
        res = sentence_verdict(s)
        if res[0] == 'drop':
            st['drop_' + res[1]] = st.get('drop_' + res[1], 0) + 1
            continue
        s2 = s
        for ph in FILLER_PHRASES:
            if ph in s2:
                st['filler'] += s2.count(ph)
                s2 = s2.replace(ph, '')
        for w in DEGREE_WORDS:
            if w in s2:
                st['degree'] += s2.count(w)
                s2 = s2.replace(w, '')
        kept.append(s2)
        st['kept_sent'] += 1

    # ③ 还原保护词
    kept = [unmask(s, table) for s in kept]

    # ④ 去重（精确 + 去标点归一化）
    out, seen = [], set()
    for s in kept:
        norm = re.sub(r'[\s。！？；，,\.!?;:：、]', '', s)
        if norm and norm in seen:
            st['dedup'] += 1
            continue
        seen.add(norm)
        out.append(s)

    l2 = ''.join(out)
    l2 = re.sub(r'[ \t]{2,}', ' ', l2)
    l2 = re.sub(r'\n{3,}', '\n\n', l2).strip()
    return l2, st


def load_dict(dict_path):
    """
    读字典 → {匹配串: (代号, 级别)}

    ⭐ v3 修：【真问题】字典原表是「代号｜含义｜路径」——
       “含义”是【说明性文字】（如「三条红线：禁止强制终止进程…」），
       正文里并不会逐字出现它 → L3 编码命中恒为 0。

    修法：表新增【可选】列「匹配式」——
       · 支持多别名，用 、/, 分隔
       · 支持 /正则/ 形式
       · 无此列时，回退用「含义」做匹配（兼容旧字典）
    """
    if not dict_path or not os.path.exists(dict_path):
        return {}
    txt = open(dict_path, 'rb').read().decode('utf-8', 'replace')
    m = {}
    for line in txt.split('\n'):
        if '|' not in line:
            continue
        cells = [c.strip().strip('`').strip('*').strip()
                 for c in line.strip().strip('|').split('|')]
        if len(cells) < 2:
            continue
        code, meaning = cells[0], cells[1]
        if code in ('代号', '词', 'Code', 'Word') or set(code) <= set('-: '):
            continue
        if not re.match(r'^[A-Za-z0-9_\-]{2,24}$', code):
            continue

        # 级别：在后续列里找完全等于 ≡/≈/⊃ 的格子（否则默认 ≈）
        lvl = ''
        for c in cells[2:]:
            if c and c[0] in ('≡', '≈', '⊃'):
                lvl = c
                break
        if not lvl:
            lvl = '≈'

        # 匹配式：优先专用列（表头含「匹配」），否则用含义
        match_cell = ''
        for c in cells[2:]:
            if c and c[0] in ('≡', '≈', '⊃'):
                continue
            if re.search(r'[\u4e00-\u9fff]', c) and ('/' not in c and '\\' not in c):
                # 不像路径 → 可能是匹配式列
                if c and c != meaning:
                    match_cell = c
                    break

        candidates = []
        if match_cell:
            candidates = [x.strip() for x in re.split(r'[、,，;；]', match_cell) if x.strip()]
        if not candidates:
            candidates = [meaning]

        for cand in candidates:
            if len(cand) >= 4 or cand.startswith('/'):
                m[cand] = (code, lvl)
    return m


def l3_encode(l2, dmap):
    """
    L2 → L3：把已登记的匹配串替换为代号
    返回 (l3, 命中代号列表, 疑似未登记代号, 用量表)
    """
    hits, used = [], {}
    l3 = l2
    # 长串优先，避免短串先替换把长串截断
    for match in sorted(dmap.keys(), key=len, reverse=True):
        code, lvl = dmap[match]
        if match.startswith('/') and match.endswith('/') and len(match) > 2:
            # 正则模式
            try:
                rx = re.compile(match[1:-1])
            except re.error:
                continue
            found = rx.findall(l3)
            if found:
                n = len(found)
                l3 = rx.sub(code, l3)
                used[code] = (match, n, lvl)
                hits.append(code)
            continue
        if match and match in l3:
            n = l3.count(match)
            l3 = l3.replace(match, code)
            used[code] = (match, n, lvl)
            hits.append(code)

    # 未登记但像代号的串（提示可编码）
    unknown = []
    for mm in re.finditer(r'[A-Za-z][A-Za-z0-9]*(?:-[A-Z0-9]+)+', l2):
        if mm.group(0) not in dmap and mm.group(0) not in hits:
            unknown.append(mm.group(0))
    return l3, hits, sorted(set(unknown)), used


def main():
    setup_stdout()
    argv = sys.argv[1:]

    if '--demo' in argv:
        return run_demo()

    if not argv:
        print(__doc__)
        return 2

    src = argv[0]
    if not os.path.exists(src):
        print('FAIL: 输入文件不存在: %s' % src)
        return 2
    text = open(src, 'rb').read().decode('utf-8', 'replace')

    dict_path = None
    if '--dict' in argv:
        i = argv.index('--dict')
        if i + 1 < len(argv):
            dict_path = argv[i + 1]

    l2, st = l2_reduce(text)
    dmap = load_dict(dict_path)
    l3, hits, unknown, used = l3_encode(l2, dmap)

    # ── 报告 ──
    r = Report(os.path.basename(src), '文本')
    r.size('L1', count(text))
    r.size('L2', count(l2))
    r.size('L3', count(l3))

    if st['filler']:
        r.mark(EQ_SEM, st['filler'])
    if st['dedup']:
        r.mark(EQ_SEM, st['dedup'])
    if st['degree']:
        r.mark(LOSS + '修饰', st['degree'])
    if st.get('drop_举例'):
        r.mark(LOSS + '举例', st['drop_举例'])
    if st.get('drop_推导'):
        r.mark(LOSS + '推导', st['drop_推导'])

    r.extra['保留句子数'] = st['kept_sent']
    r.extra['套话删除'] = st['filler']
    r.extra['程度修饰删除'] = st['degree']
    r.extra['重复句合并'] = st['dedup']
    r.extra['丢弃句子'] = st.get('drop_举例', 0) + st.get('drop_推导', 0) + st.get('drop_样式', 0)
    if dmap:
        r.extra['字典词条'] = len(dmap)
        r.extra['L3 命中代号'] = len(hits)

    # ── 门禁（docs/04 §5.1）──
    if st['kept_sent'] == 0 and count(text) > 0:
        r.gate('L2 非空', 'FAIL', 'L2 无内容 —— 归约过度')
    else:
        r.gate('L2 非空', 'OK', '保留 %d 句' % st['kept_sent'])

    lost = st['degree'] + st.get('drop_举例', 0) + st.get('drop_推导', 0)
    if count(text) and count(l2) / float(count(text)) < 0.15:
        r.gate('L2 保留率', 'WARN', '保留率 %.1f%% < 15%%，归约可能过激' % ratio(count(text), count(l2)))
    else:
        r.gate('L2 保留率', 'OK', '%.1f%%' % ratio(count(text), count(l2)))

    # ⭐ 膨胀门禁：压缩反而变大 = 没压 → 必须拦
    l1n, l2n = r.sizes.get('L1', 0), r.sizes.get('L2', 0)
    if l2n > l1n and l1n:
        r.gate('L2 未膨胀', 'FAIL', 'L2(%d) > L1(%d) → 归约无效' % (l2n, l1n))
    else:
        r.gate('L2 未膨胀', 'OK', 'L2 为 L1 的 %.1f%%' % ratio(l1n, l2n))

    if dmap and hits:
        r.gate('L3 代号已登记', 'OK', '%d 个代号全部有字典定义' % len(hits))
    elif dmap:
        r.gate('L3 代号已登记', 'WARN', '字典非空但无命中')
    else:
        r.gate('L3 代号已登记', 'WARN', '未提供字典 → L3 等同 L2（未编码）')

    if unknown:
        r.gate('无未登记代号', 'WARN', '疑似未登记: %s' % '、'.join(unknown[:5]))

    # 保护词未受损检查
    dmg = []
    for w in ['仅', '至少', '最多', '必须', '禁止', '不得']:
        if w in text and w not in l2:
            dmg.append(w)
    if dmg:
        r.gate('保护词未受损', 'FAIL', '限界/模态词被删: %s' % '、'.join(dmg))
    else:
        r.gate('保护词未受损', 'OK', '限界/模态词完好')

    # ⭐ 反引号配对门禁（首跑发现：抽句可能切坏行内代码段）
    n1, n2 = text.count('`'), l2.count('`')
    if n1 and (n2 != n1):
        r.gate('反引号配对', 'WARN',
               'L1=%d 个，L2=%d 个 → 行内代码段可能在抽句时被切断' % (n1, n2))
    else:
        r.gate('反引号配对', 'OK', 'L1/L2 均为 %d 个' % n1)

    nfail = r.dump()

    if '--out-l2' in argv:
        p = argv[argv.index('--out-l2') + 1]
        open(p, 'wb').write(l2.encode('utf-8'))
        print(' 已写 L2: %s' % p)
    if '--out-l3' in argv:
        p = argv[argv.index('--out-l3') + 1]
        open(p, 'wb').write(l3.encode('utf-8'))
        print(' 已写 L3: %s' % p)
    if '--report-json' in argv:
        p = argv[argv.index('--report-json') + 1]
        open(p, 'wb').write(json.dumps({
            'source': src, 'sizes': r.sizes, 'marks': r.marks,
            'stats': st, 'gates': r.gates, 'uncompressed': r.uncompressed,
        }, ensure_ascii=False, indent=2).encode('utf-8'))
        print(' 已写报告 JSON: %s' % p)
    return 1 if nfail else 0


def run_demo():
    demo = (
        '值得注意的是，这套方案非常重要，我们非常需要它。\n'
        '综上所述，仅支持 3 种格式。\n'
        '例如，日志文件 app.log 位于 /var/log 目录。\n'
        '例如，还有一些别的例子。\n'
        '由此可见，必须禁止未授权访问。\n'
        '简单来说，就是这样一个东西。\n'
        '这套方案非常重要，我们非常需要它。\n'
    )
    l2, st = l2_reduce(demo)
    print('--- L1 ---')
    print(demo)
    print('--- L2 ---')
    print(l2)
    print('--- 统计 ---')
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
