#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tier_code.py — 代码域三级封装（L1 片段 → L2 功能 → L3 结构）

用法：
    python tier_code.py <源文件> [--dict <字典文件>] [--out-l2 <f>] [--out-l3 <f>]
    python tier_code.py <源文件> --json

三级定义（见 docs/04 §2.3）：
    L1 片段级  完整实现（函数体/类体全文）
    L2 功能级  签名 + docstring 首行 + 关键行（常量/异常/鉴权/外部调用/返回）
    L3 结构级  模块 → 类 → 函数清单 + 职责一行 + 行号锚点

关键行判据（L2 必留）：
    常量定义 / raise / assert / return / 鉴权校验 / 外部调用 / 装饰器

⚠️ 语言支持（诚实声明）：
    · Python  → 用 ast 精确解析（推荐）
    · 其他语言 → 正则大纲【降级模式】，报告会标 DEGRADED

零依赖：仅标准库。
"""

import io
import os
import re
import sys
import json
import ast

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier_common import EQ_SEM, LOSS, setup_stdout, count, ratio, Report  # noqa: E402

AUTH_HINT = re.compile(r'(auth|permission|verify|check_|token|secret|credential|login|admin)', re.I)
EXT_HINT = re.compile(r'(requests|urllib|subprocess|os\.system|psycopg|socket|open\(|connect)', re.I)


# ══════════════ Python：AST 精确模式 ══════════════

def _sig(node):
    """重建签名（含默认值与返回标注）"""
    try:
        return node.name + ast.unparse(node.args) + (
            ' -> ' + ast.unparse(node.returns) if node.returns else '')
    except Exception:
        return node.name + '(...)'


def _first_doc(node):
    d = ast.get_docstring(node)
    return (d or '').strip().split('\n')[0] if d else ''


def _key_lines(node, src_lines):
    """挑 L2 必留的关键行（带行号）"""
    out = []
    for n in ast.walk(node):
        ln = getattr(n, 'lineno', None)
        if not ln:
            continue
        raw = src_lines[ln - 1].strip() if ln - 1 < len(src_lines) else ''
        if not raw:
            continue
        tag = None
        if isinstance(n, ast.Raise):
            tag = '异常抛出'
        elif isinstance(n, ast.Assert):
            tag = '断言'
        elif isinstance(n, ast.Return):
            tag = '返回'
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    tag = '常量定义'
                    break
        elif isinstance(n, ast.If) and AUTH_HINT.search(raw):
            tag = '鉴权/校验'
        elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and EXT_HINT.search(raw):
            tag = '外部调用'
        if tag:
            out.append((ln, tag, raw[:120]))
    # 去重 + 排序
    seen, uniq = set(), []
    for ln, tag, raw in sorted(out):
        if ln in seen:
            continue
        seen.add(ln)
        uniq.append((ln, tag, raw))
    return uniq


def parse_python(path, text):
    lines = text.split('\n')
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return None, 'SyntaxError: %s' % e

    mod_doc = _first_doc(tree) or '(无模块 docstring)'
    struct = {'module_doc': mod_doc, 'classes': [], 'functions': [], 'lineno': len(lines)}

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            cls = {'name': node.name, 'line': node.lineno, 'doc': _first_doc(node),
                   'bases': [ast.unparse(b) for b in node.bases], 'methods': []}
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cls['methods'].append({
                        'name': sub.name, 'line': sub.lineno, 'end': getattr(sub, 'end_lineno', sub.lineno),
                        'sig': _sig(sub), 'doc': _first_doc(sub),
                        'keys': _key_lines(sub, lines),
                    })
            struct['classes'].append(cls)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            struct['functions'].append({
                'name': node.name, 'line': node.lineno, 'end': getattr(node, 'end_lineno', node.lineno),
                'sig': _sig(node), 'doc': _first_doc(node),
                'keys': _key_lines(node, lines),
            })
    return struct, None


def render_l2_py(struct):
    out = ['# L2 功能级封装 ｜ 签名 + 职责 + 关键行', '']
    out.append('## 模块：%s' % struct['module_doc'])
    out.append('')
    for f in struct['functions']:
        out.append('### def %s  (L%d-%d)' % (f['sig'], f['line'], f['end']))
        if f['doc']:
            out.append('  职责：%s' % f['doc'])
        for ln, tag, raw in f['keys'][:8]:
            out.append('  [%s] L%d: %s' % (tag, ln, raw))
        out.append('')
    for c in struct['classes']:
        base = ('(%s)' % ', '.join(c['bases'])) if c['bases'] else ''
        out.append('### class %s%s  (L%d)' % (c['name'], base, c['line']))
        if c['doc']:
            out.append('  职责：%s' % c['doc'])
        for m in c['methods']:
            out.append('  · def %s  (L%d-%d)' % (m['sig'], m['line'], m['end']))
            if m['doc']:
                out.append('      职责：%s' % m['doc'])
            for ln, tag, raw in m['keys'][:6]:
                out.append('      [%s] L%d: %s' % (tag, ln, raw))
        out.append('')
    return '\n'.join(out)


def render_l3_py(struct):
    out = ['# L3 结构级封装 ｜ 模块 → 类 → 函数清单', '']
    out.append('模块职责：%s' % struct['module_doc'])
    out.append('')
    if struct['functions']:
        out.append('## 顶层函数（%d）' % len(struct['functions']))
        for f in struct['functions']:
            out.append('  F-%-22s L%-4d %s' % (f['name'].upper().replace('_', '-'),
                                               f['line'], f['doc'] or '(无职责说明)'))
        out.append('')
    if struct['classes']:
        out.append('## 类（%d）' % len(struct['classes']))
        for c in struct['classes']:
            out.append('  C-%-22s L%-4d %s' % (c['name'].upper().replace('_', '-'),
                                               c['line'], c['doc'] or '(无职责说明)'))
            for m in c['methods']:
                out.append('      .%-20s L%-4d %s' % (m['name'], m['line'],
                                                      m['doc'] or '(无职责说明)'))
        out.append('')
    return '\n'.join(out)


# ══════════════ 其他语言：正则大纲【降级】 ══════════════

RE_OTHER = [
    (re.compile(r'^\s*(?:public|private|protected|static|inline|extern|\s)*\s*'
                r'(?:void|int|char|float|double|bool|string|auto|[A-Z_][A-Za-z0-9_<>:]*)\s+'
                r'([A-Za-z_][A-Za-z0-9_]*)\s*\(', re.M), 'FUNC'),
    (re.compile(r'^\s*(?:class|struct|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)', re.M), 'TYPE'),
    (re.compile(r'^\s*(?:function|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?\(', re.M), 'FUNC'),
    (re.compile(r'^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)', re.M), 'FUNC'),
]


def parse_other(path, text):
    """降级：正则大纲，无签名细节、无关键行"""
    lines = text.split('\n')
    items = []
    for rx, kind in RE_OTHER:
        for m in rx.finditer(text):
            ln = text[:m.start()].count('\n') + 1
            items.append((ln, kind, m.group(1)))
    seen, uniq = set(), []
    for ln, kind, name in sorted(items):
        if (ln, name) in seen:
            continue
        seen.add((ln, name))
        uniq.append({'line': ln, 'kind': kind, 'name': name,
                     'sig': lines[ln - 1].strip()[:110] if ln - 1 < len(lines) else ''})
    return uniq


# ══════════════ 主流程 ══════════════

def main():
    setup_stdout()
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2

    src = argv[0]
    if not os.path.exists(src):
        print('FAIL: 源文件不存在: %s' % src)
        return 2
    text = open(src, 'rb').read().decode('utf-8', 'replace')
    ext = os.path.splitext(src)[1].lower()

    r = Report(os.path.basename(src), '代码')
    r.size('L1', count(text))
    degraded = False

    if ext == '.py':
        struct, err = parse_python(src, text)
        if err:
            r.gate('AST 解析', 'FAIL', err)
            struct = None
    else:
        struct = None

    if struct:
        l2 = render_l2_py(struct)
        l3 = render_l3_py(struct)
        n_func = len(struct['functions'])
        n_meth = sum(len(c['methods']) for c in struct['classes'])
        n_cls = len(struct['classes'])
        r.extra['顶层函数'] = n_func
        r.extra['类'] = n_cls
        r.extra['方法'] = n_meth
        r.extra['关键行总数'] = sum(len(f['keys']) for f in struct['functions']) + \
            sum(len(m['keys']) for c in struct['classes'] for m in c['methods'])
        # 签名完整（L2 保留全部签名）
        r.mark(EQ_SEM, n_func + n_cls + n_meth)
        # 实现细节丢弃
        dropped = (n_func + n_meth)
        if dropped:
            r.mark(LOSS + '细节', dropped)
        r.gate('AST 解析', 'OK', 'Python 精确模式')
        for f in struct['functions']:
            if not f['doc']:
                r.skip('def %s (L%d)' % (f['name'], f['line']), '无 docstring → 无法生成职责行')
        for c in struct['classes']:
            for m in c['methods']:
                if not m['doc']:
                    r.skip('%s.%s (L%d)' % (c['name'], m['name'], m['line']), '无 docstring → 无法生成职责行')
    else:
        degraded = True
        items = parse_other(src, text)
        l2 = '# L2 功能级（降级：正则大纲）\n' + '\n'.join(
            '  %s  L%-4d  %s' % (i['kind'], i['line'], i['sig']) for i in items)
        l3 = '# L3 结构级（降级：正则大纲）\n' + '\n'.join(
            '  %s-%-20s L%d' % (i['kind'], i['name'].upper(), i['line']) for i in items)
        r.extra['正则命中'] = len(items)
        r.mark(EQ_SEM, len(items))
        r.mark(LOSS + '细节', len(items))
        r.gate('AST 解析', 'WARN', '非 Python 或解析失败 → 降级为正则大纲（无签名细节）')

    r.size('L2', count(l2))
    r.size('L3', count(l3))

    # ── 门禁 ──
    if not l2.strip():
        r.gate('L2 非空', 'FAIL', '未提取到任何结构')
    else:
        r.gate('L2 非空', 'OK', '%d 字符' % count(l2))

    # 签名完整性：L2 里函数名必须都在
    if struct:
        names = [f['name'] for f in struct['functions']] + \
                [m['name'] for c in struct['classes'] for m in c['methods']]
        miss = [n for n in names if n not in (l2 + l3)]
        if miss:
            r.gate('签名完整', 'FAIL', 'L2/L3 缺函数: %s' % '、'.join(miss[:5]))
        else:
            r.gate('签名完整', 'OK', '%d 个函数/方法签名全保留' % len(names))

        # 行号锚点：L3 每项必须带 L\d+
        anchors = re.findall(r'L\d+', l3)
        if len(anchors) < len(names):
            r.gate('行号锚点', 'FAIL', 'L3 锚点数 %d < 函数数 %d' % (len(anchors), len(names)))
        else:
            r.gate('行号锚点', 'OK', '%d 个锚点（可回查 L1）' % len(anchors))
    else:
        r.gate('签名完整', 'WARN', '降级模式不保证')
        r.gate('行号锚点', 'OK' if re.search(r'L\d+', l3) else 'FAIL', '')

    nfail = r.dump()

    if '--out-l2' in argv:
        p = argv[argv.index('--out-l2') + 1]
        open(p, 'wb').write(l2.encode('utf-8'))
        print(' 已写 L2: %s' % p)
    if '--out-l3' in argv:
        p = argv[argv.index('--out-l3') + 1]
        open(p, 'wb').write(l3.encode('utf-8'))
        print(' 已写 L3: %s' % p)
    if '--json' in argv:
        print(json.dumps({'source': src, 'degraded': degraded, 'sizes': r.sizes,
                          'marks': r.marks, 'gates': r.gates,
                          'uncompressed': r.uncompressed}, ensure_ascii=False, indent=2))
    return 1 if nfail else 0


if __name__ == '__main__':
    sys.exit(main())
