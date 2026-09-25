# -*- coding: utf-8 -*-
"""
三级概括法 · 共用模块（tier_common）

提供：
  · 损失标记体系（≡ / ≈ / ⊃ + 损失类别）
  · 保护词表（承载真值，禁止删除）
  · 保守删除词表（仅多字无歧义词）
  · 统一报告输出（ASCII 判据，规避 GBK 控制台问题）

零依赖：仅标准库。
"""

import io
import sys
import re

# ── 损失标记 ──────────────────────────────────────────
EQ_STRICT = '≡'    # 严格等价（字面/语义完全一致）
EQ_SEM = '≈'       # 语义等价（要素全保留，措辞有变）
LOSS = '⊃'         # 信息损失（必须带类别标签）
LOSS_TAGS = ('修饰', '举例', '推导', '细节', '样式')

# ── 保护词（承载真值：限界/模态/量词）—— 绝不允许删除 ──
# ⚠️ 这些词一删，句子从「事实」变「错误」。是反例 3 的直接防线。
PROTECT_WORDS = [
    '仅', '只', '至少', '最多', '不超过', '超过', '唯一', '必须', '禁止',
    '不得', '只能', '全部', '部分', '均不', '均', '都', '从不', '永远',
    '一定', '可能', '大约', '左右', '以上', '以下', '之内', '之外',
    '首次', '不再', '尚未', '不能', '不可以', '务必', '凡是', '所有',
    '任何', '每个', '一旦', '否则', '除非',
]

# ── 程度修饰（不改变真值，可删）─────────────────────────
# ⚠️ 只收【多字且无歧义】的。
#    单字的「最/更/太/很/挺」故意不收 —— 它们大量构成合成词
#    （最近 / 更加 / 甚至 / 何况），删了就破坏词义。属已知限制。
DEGREE_WORDS = [
    '非常', '十分', '极其', '极为', '格外', '相当', '稍微', '略微',
    '有点', '有些', '颇为', '异常', '甚是', '特别', '尤其是', '尤为',
]

# ── 套话框架词（删词、留其后内容）───────────────────────
FILLER_PHRASES = [
    '值得注意的是，', '值得注意的是', '需要指出的是，', '需要指出的是',
    '需要说明的是，', '需要说明的是', '需要强调的是，', '需要强调的是',
    '值得一提的是，', '值得一提的是', '不得不说的是，', '不得不说',
    '众所周知，', '众所周知', '显而易见，', '显而易见',
    '毋庸置疑，', '毋庸置疑', '综上所述，', '综上所述',
    '总而言之，', '总而言之', '总的来说，', '总的来说',
    '一言以蔽之，', '换句话说，', '也就是说，',
    '事实上，', '实际上，', '严格来说，', '严格地说，',
    '客观地说，', '客观来说，', '就目前而言，', '从某种意义上说，',
    '在一定程度上，', '正如前面所述，', '如前所述，', '由上可知，',
    '简单来说，', '简单地说，', '概括地说，', '总的来说',
]

# ── 关键句保护模式（命中即必留）─────────────────────────
KEY_PATTERNS = [
    (r'\d', '数值/日期'),
    (r'`[^`]+`', '代码/命令'),
    (r'[A-Za-z0-9_.\-]+\.(?:md|py|json|txt|log|js|ts|html|css|yaml|yml|toml|ps1|cmd|sh|c|cpp|h|sql)', '文件路径'),
    (r'[A-Z]{2,}(?:-[A-Z0-9]+)+', '代号'),
    (r'(?:结论|因此|所以|导致|表明|说明|意味着|证明)', '结论/因果'),
    (r'(?:必须|禁止|不得|要求|应当|需要|务必)', '规范/要求'),
    (r'(?:如果|若|当|除非|只有|一旦|否则)', '条件'),
    (r'(?:但是|然而|不过|却|反而|而是|但)', '转折'),
    (r'(?:失败|错误|故障|漏洞|风险|崩溃|拒绝|失效)', '风险/失败'),
]

# ── 可丢句子模式（仅在无保护命中时丢）───────────────────
DROP_PATTERNS = [
    (r'(?:例如|比如|譬如|举个例子|举例来说|诸如|好比)', '举例', '举例'),
    (r'(?:简单来说|概括地说|说白了|通俗地讲)', '推导', '铺垫'),
    (r'^\s*$', '样式', '空行'),
]


def setup_stdout():
    """Windows GBK 控制台安全输出"""
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    except Exception:
        pass


def count(text):
    return len(text or '')


def ratio(a, b):
    """压缩率：b 相对 a 的比例（保留 1 位）"""
    if not a:
        return 0.0
    return round(100.0 * b / a, 1)


def mask_protect(text):
    """把保护词换成占位符，避免被后续删除规则误伤。返回 (masked, table)"""
    table = {}
    for i, w in enumerate(sorted(PROTECT_WORDS, key=len, reverse=True)):
        ph = '\x00P%03d\x00' % i
        if w in text:
            text = text.replace(w, ph)
            table[ph] = w
    return text, table


def unmask(text, table):
    for ph, w in table.items():
        text = text.replace(ph, w)
    return text


def split_sentences(text):
    """按中英文句末标点切句，保留标点"""
    parts = re.findall(r'[^。！？；\n]*[。！？；]|[^。！？；\n]+', text)
    return [p for p in parts if p is not None]


def sentence_verdict(sent):
    """
    判断句子该留还是该丢。
    返回 ('keep', 原因) / ('drop', 类别, 原因) / ('keep', '默认')
    """
    for pat, why in KEY_PATTERNS:
        if re.search(pat, sent):
            return ('keep', why)
    for pat, tag, why in DROP_PATTERNS:
        if re.search(pat, sent):
            return ('drop', tag, why)
    return ('keep', '默认')


class Report(object):
    """统一报告：串起三级统计 + 损失分布 + 门禁结果"""

    def __init__(self, name, domain):
        self.name = name
        self.domain = domain
        self.sizes = {}          # 级 → 字符数
        self.marks = {}          # ≡/≈/⊃tag → 次数
        self.uncompressed = []   # 未压缩项 + 原因
        self.gates = []          # (门禁名, OK/WARN/FAIL, 说明)
        self.extra = {}          # 域特有指标

    def size(self, tier, n):
        self.sizes[tier] = n

    def mark(self, key, n=1):
        self.marks[key] = self.marks.get(key, 0) + n

    def skip(self, what, why):
        self.uncompressed.append((what, why))

    def gate(self, name, verdict, note=''):
        self.gates.append((name, verdict, note))

    def dump(self, show_text=True):
        print('=' * 64)
        print(' 三级概括法报告 · %s (%s)' % (self.name, self.domain))
        print('=' * 64)

        print(' 【各级体量】')
        base = self.sizes.get('L1', 0)
        for t in ('L1', 'L2', 'L3'):
            if t in self.sizes:
                n = self.sizes[t]
                print('   %s  %8d 字符   %5.1f%% of L1' % (t, n, ratio(base, n)))
        if 'L1' in self.sizes and 'L3' in self.sizes:
            l1, l3 = self.sizes['L1'], self.sizes['L3']
            if l3:
                print('   端到端压缩比: %.1f : 1  (省 %.1f%%)'
                      % (l1 / float(l3) if l3 else 0, 100 - ratio(l1, l3)))

        print()
        print(' 【损失分布】（铁律：带 ⊃ 者不得作最终依据）')
        if not self.marks:
            print('   （无）')
        else:
            for k in sorted(self.marks, key=lambda x: -self.marks[x]):
                tag = 'LOSS' if k.startswith(LOSS) else 'OK  '
                note = '  ← 不可作最终依据' if k.startswith(LOSS) else ''
                print('   %s  %-10s ×%-5d%s' % (tag, k, self.marks[k], note))

        if self.extra:
            print()
            print(' 【域内指标】')
            for k, v in self.extra.items():
                print('   %-22s %s' % (k, v))

        if self.uncompressed:
            print()
            print(' 【未压缩项】（无法归约/模板化的，必须列明原因）')
            for what, why in self.uncompressed[:20]:
                print('   - %s  ｜ %s' % (what[:52], why))
            if len(self.uncompressed) > 20:
                print('   … 另有 %d 项' % (len(self.uncompressed) - 20))

        print()
        print(' 【门禁】')
        nfail = 0
        for name, verdict, note in self.gates:
            if verdict == 'FAIL':
                nfail += 1
            print('   %-6s %-24s %s' % (verdict, name, note))
        print('-' * 64)
        if nfail:
            print(' 小结: FAIL=%d ｜ 判据: FAIL=必须修' % nfail)
        else:
            print(' 小结: 全部门禁通过 (FAIL=0)')
        print('-' * 64)
        return nfail
