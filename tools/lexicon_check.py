#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
lexicon_check.py — 字典可达性校验（字典压缩法配套）v2

校验三件事：
  1. 字典每条「详情位置」是否真实存在（防悬空引用）
  2. 代号是否重复定义（防一词两表）
  3. 正文索引里的词名，字典里是否都有（防孤儿代号）

用法：
    python lexicon_check.py <字典文件.md>
    python lexicon_check.py <字典文件.md> --index <正文文件>
    python lexicon_check.py <字典文件.md> --root <路径> [--root <路径>...]
    python lexicon_check.py <字典文件.md> --alias 背包="C:/path/to/desktop"
    python lexicon_check.py <字典文件.md> --json

判据回显一律用 ASCII 词（OK / WARN / FAIL）——
GBK 控制台会把 ✅/❌ 渲染成同一个 "?"，逐项校验当场失去判读力（实测踩过）。

v2 修（首跑抓到的两个自身 bug）：
  · 相对路径按【多个候选根】解析（字典目录 / --root / --alias），
    而非只看字典所在目录 —— 否则字典写法相对工作区根时会全量假报"悬空"
  · 正文索引正则容忍 `**词表（29）**：` 这种粗体包裹
  · 示例/模板类条目（路径明显为占位）降级为 WARN

零依赖：仅标准库。
"""
import io
import os
import re
import sys
import json

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass

# 视为"外部/无需本地校验"的前缀
EXTERNAL_PREFIX = ('http://', 'https://', 'ftp://')
# 表格行：| `CODE` | 含义 | 路径 |  或  | **CODE** | 含义 | 路径 |
ROW_RE = re.compile(r'^\|\s*\*{0,2}`?([A-Za-z0-9_\-\.]+)`?\*{0,2}\s*\|(.+)\|(.+)\|\s*$')
PATH_RE = re.compile(r'`([^`]+)`')
# 正文索引行（容忍粗体/括号）：> **词表（29）**：A、B、C
INDEX_RE = re.compile(r'词表\s*[（(]\s*\d*\s*[）)]\s*\*{0,2}\s*[:：]\s*(.+)')
# 占位路径特征（示例/模板）
PLACEHOLDER_HINT = re.compile(r'(docs/|path/to|your/|example|xxx|<[^>]+>)', re.I)
# 裸文件名（如 `AGENTS.md`）也是合法路径 —— v2.1 修：原先只认含分隔符的路径
BARE_FILE_RE = re.compile(r'^[\w\-\.]+\.\w{1,6}$')


def read(p):
    return open(p, 'rb').read().decode('utf-8', 'replace')


def parse_dictionary(text):
    """
    解析字典表格 → [(code, meaning, paths, loc, lineno)]

    ⭐ v3 修（兼容「匹配式」新列）：
      首版把【最后一列】当路径 → 新增「匹配式」列后就不灵了。
      修法：按【内容特征】选列，不靠位置：
        · code    = 第1列
        · meaning = 第2列
        · 级别     = 首个以 ≡/≈/⊃ 开头的列
        · 路径     = 首个“长得像路径”的列
    """
    def looks_like_path(v):
        if not v:
            return False
        if v.startswith(EXTERNAL_PREFIX):
            return True
        for p in PATH_RE.findall(v):
            if '/' in p or '\\' in p or re.match(r'^[\w\-\.]+\.[A-Za-z]{1,6}$', p):
                return True
        return False

    out = []
    for i, line in enumerate(text.split('\n'), 1):
        if '|' not in line:
            continue
        m = ROW_RE.match(line.rstrip())
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if not cells:
            continue
        code = cells[0].strip('`*')
        if code in ('代号', '词', 'Code', 'Word') or set(code) <= set('-: '):
            continue
        if not code:
            continue
        meaning = cells[1].strip('`*') if len(cells) > 1 else ''
        if not meaning:
            continue
        loc = ''
        for c in cells[2:]:
            if looks_like_path(c):
                loc = c
                break
        out.append((code, meaning, PATH_RE.findall(loc), loc, i))
    return out


def resolve(p, roots, aliases):
    """按 绝对 → 别名 → 各候选根 依次解析"""
    if os.path.isabs(p):
        return p
    # 别名前缀（如 背包\xxx）
    for name, base in aliases.items():
        if p == name or p.startswith(name + os.sep) or p.startswith(name + '/'):
            rest = p[len(name):].lstrip('/\\')
            return os.path.join(base, rest.replace('/', os.sep).replace('\\', os.sep))
    for r in roots:
        cand = os.path.join(r, p.replace('/', os.sep).replace('\\', os.sep))
        if os.path.exists(cand):
            return cand
    # 都没命中 → 返回第一个候选（用于报错展示）
    return os.path.join(roots[0], p.replace('/', os.sep).replace('\\', os.sep)) if roots else p


def check_paths(entries, roots, aliases, allow_placeholder=True):
    """
    返回 (fails, warns, oks)

    ⭐ v4 修【可用性】：同一个【未知前缀】产生的多条 FAIL 会被聚合，
      而不是刷屏 —— 实测：忘给 --alias 时曾一次吐 16 条同因 FAIL，
      噪音淹没真问题（而真正的悬空路径反而看不见）。
    """
    fails, warns, oks = [], [], 0
    # 前缀 → [条目...]（用于聚合）
    prefix_groups = {}
    for code, _m, paths, loc, ln in entries:
        real = [p for p in paths
                if len(p) >= 3 and ('/' in p or '\\' in p or BARE_FILE_RE.match(p))]
        if not real:
            warns.append((code, ln, '该条目未给可解析路径（仅名称/无路径）'))
            continue
        for p in real:
            if p.startswith(EXTERNAL_PREFIX):
                oks += 1
                continue
            cand = resolve(p, roots, aliases)
            if os.path.exists(cand):
                oks += 1
                continue
            parent = os.path.dirname(cand)
            if parent and os.path.exists(parent):
                warns.append((code, ln, '文件不存在但父目录存在: %s' % p))
            elif PLACEHOLDER_HINT.search(p) and allow_placeholder:
                warns.append((code, ln, '占位/示例路径（未创建）: %s' % p))
            else:
                # 取首个路径段作为前缀候选
                head = re.split(r'[\\/]', p)[0]
                prefix_groups.setdefault(head, []).append((code, ln, p))

    # 聚合：同前缀 ≥3 条 → 一条提示；否则逐条 FAIL
    for head, items in prefix_groups.items():
        if len(items) >= 3:
            sample = items[0][2]
            fails.append((items[0][0], items[0][1],
                          '【未知前缀「%s」共 %d 条】例: %s —— 若该前缀是别名，请加 --alias %s="<实际路径>"'
                          % (head, len(items), sample, head)))
        else:
            for code, ln, p in items:
                fails.append((code, ln, '路径悬空: %s' % p))
    return fails, warns, oks


def check_dupes(entries):
    seen, dupes = {}, []
    for code, _m, _p, _l, ln in entries:
        k = code.upper()
        if k in seen:
            dupes.append((code, ln, '与 L%d 重复定义' % seen[k]))
        else:
            seen[k] = ln
    return dupes


def check_index(entries, index_file):
    text = read(index_file)
    m = INDEX_RE.search(text)
    if not m:
        return None, []
    names = [x.strip().strip('`').strip('*').strip() for x in re.split(r'[、,，]', m.group(1))]
    names = [n for n in names if n and re.match(r'^[A-Za-z0-9_\-\.]+$', n)]
    have = {c.upper() for c, *_ in entries}
    return names, [n for n in names if n.upper() not in have]


def main():
    argv = sys.argv[1:]
    as_json = '--json' in argv
    roots, aliases = [], {}
    i = 0
    positional = []
    while i < len(argv):
        a = argv[i]
        if a == '--json':
            i += 1
            continue
        if a == '--root' and i + 1 < len(argv):
            roots.append(os.path.abspath(argv[i + 1]))
            i += 2
            continue
        if a == '--alias' and i + 1 < len(argv):
            kv = argv[i + 1]
            if '=' in kv:
                k, v = kv.split('=', 1)
                aliases[k] = os.path.abspath(v)
            i += 2
            continue
        if a == '--index' and i + 1 < len(argv):
            i += 2
            continue
        positional.append(a)
        i += 1

    index_file = None
    if '--index' in argv:
        j = argv.index('--index')
        if j + 1 < len(argv):
            index_file = argv[j + 1]

    if not positional:
        print(__doc__)
        return 2
    dic = os.path.abspath(positional[0])
    if not os.path.exists(dic):
        print('FAIL: 字典文件不存在: %s' % dic)
        return 2

    # 候选根：字典目录 + 显式 --root + cwd
    if os.path.dirname(dic) not in roots:
        roots.insert(0, os.path.dirname(dic))
    if os.getcwd() not in roots:
        roots.append(os.getcwd())

    text = read(dic)
    entries = parse_dictionary(text)
    fails, warns, oks = check_paths(entries, roots, aliases)
    dupes = check_dupes(entries)
    idx_names = idx_missing = None
    if index_file and os.path.exists(index_file):
        idx_names, idx_missing = check_index(entries, index_file)

    if as_json:
        print(json.dumps({
            'dictionary': dic, 'entries': len(entries), 'roots': roots,
            'paths_ok': oks, 'fail': len(fails) + len(dupes), 'warn': len(warns),
            'fails': [{'code': c, 'line': l, 'detail': d} for c, l, d in fails + dupes],
            'warns': [{'code': c, 'line': l, 'detail': d} for c, l, d in warns],
            'index_missing': idx_missing,
        }, ensure_ascii=False, indent=2))
        return 0 if not (fails or dupes) else 1

    print('=' * 60)
    print(' 字典可达性校验 (lexicon_check v2)')
    print('=' * 60)
    print(' 字典文件: %s' % dic)
    print(' 词条数  : %d' % len(entries))
    print(' 候选根  : %s' % ' ｜ '.join(roots))
    if aliases:
        print(' 别名    : %s' % ' ｜ '.join('%s=%s' % (k, v) for k, v in aliases.items()))
    print('')

    def dump(items, tag):
        if not items:
            return
        print(' %s  %d 项' % (tag, len(items)))
        for c, l, d in items:
            print('   - [%s] L%d  %s' % (c, l, d))
        print('')

    dump(fails, 'FAIL')
    dump(dupes, 'FAIL(重复)')
    dump(warns, 'WARN')

    if idx_missing is not None:
        print(' 正文索引对照:')
        print('   索引词名 %d 个 ｜ 字典缺失 %d 个' % (len(idx_names or []), len(idx_missing or [])))
        if idx_missing:
            print('   FAIL 索引有但字典没有: %s' % '、'.join(idx_missing))
        print('')

    print('-' * 60)
    print(' 小结: 路径可达 %d ｜ FAIL=%d ｜ WARN=%d'
          % (oks, len(fails) + len(dupes), len(warns)))
    print(' 判据: FAIL=必须修（悬空引用/重复定义）｜ WARN=建议复核')
    print('-' * 60)
    return 0 if not (fails or dupes) else 1


if __name__ == '__main__':
    sys.exit(main())
