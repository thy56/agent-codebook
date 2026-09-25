#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""lexicon_audit.py — 字典可用性审计：R1~R6 六项机器判据。

规范来源：`docs/05_引索符系统规范.md`
  §10.3 盈亏平衡判据（常驻成本 + 一次性展开成本 vs 基线）→ R5
  §5.2  必填列（`term` 非空）→ R2
  §2.1  INV-2「代号全局唯一且只增不改」→ R3（调用 `src/lexicon.compare_rev`）
  §2.2  压缩来源为**整条概念单元**（低频概念不该编码）→ R4
  §5.2 / §3.1 代号命名应可联想 → R6
  §2.3  载体分离的前提是「索引行留在常驻侧」→ R1

六项判据的精确口径（逐条可复核）写在各 `_check_rN` 的 docstring 里，
改口径必须同时改 docstring 与 `--self-test` 的样例。

CLI：
    python tools/lexicon_audit.py <字典.tsv> [--index <正文文件>]
        [--corpus <语料文件或目录>] [--root <目录>] [--index-budget 300]
        [--json] [--self-test]

输出逐项判据行，形如：
    OK   R1 index_budget      索引 128 字符 / 上限 300
任一项 FAIL → 退出码 1；全 OK/WARN → 退出码 0。

零第三方依赖（仅标准库）。判据词只用 ASCII（OK / WARN / FAIL）——
Windows GBK 控制台会把 ✅/❌ 渲染成同一个 "?"，逐项判据当场失去判读力。
"""
from __future__ import annotations

import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import lexicon as LX            # noqa: E402  （§5.1 TSV 读写 / §5.1.1 字段转义 / compare_rev）

__all__ = [
    "Check", "audit_text", "extract_index_line", "derived_index_line",
    "read_corpus", "LEVELS", "RISK_IDS", "self_test",
]

LEVELS: Tuple[str, ...] = ("OK", "WARN", "FAIL")
RISK_IDS: Tuple[str, ...] = ("R1", "R2", "R3", "R4", "R5", "R6")

# 字典列名行（§5.1）：本工具用**独立**的原始行读取器，
# 因为 §5.1 的严格解析器会丢弃重复代号行，而 R3 正需要看到那些行。
_DEFAULT_HEADER = ("code", "term", "meaning", "fidelity", "detail_path",
                   "match", "loss", "rev")
# §6.1 `code` 产生式；纯顺序编号形如 R1 / C12 / AB7
_SEQ_CODE_RE = re.compile(r"^[A-Z]{0,3}[0-9]+$")
# 索引行里出现的裸代号（`@`/`^` 是引索符，索引应是词名而非符号串）
_BARE_CODE_RE = re.compile(r"[@^][A-Za-z0-9]")
# 「词表（N）」索引行标记（与 docs/03 的真实写法一致）
# 真实写法是 `**词表（29）**：RL-3、…` —— `）` 与 `：` 之间夹着 Markdown 粗体标记，
# 故这里必须容忍 `*` 等强调字符（首版漏容忍 → 索引行抽不到，静默退化成按 term 推导，
# 由本工具自己的 --self-test 抓到）。
_INDEX_LINE_RE = re.compile(r"^.*词表\s*[（(][^）)]*[）)]\s*\**\s*[:：].*$", re.M)


@dataclass
class Check:
    """一项判据的结果：`rid` 为风险编号（R1~R6），`level` 取 OK / WARN / FAIL。

    字段顺序刻意是「rid → name → detail → data → level」：六项判据的构造点
    都写 `Check(id, name, 说明, 数据, 级别)`，级别落在最后便于一眼核对。
    """

    rid: str
    name: str
    detail: str
    data: Dict[str, Any] = field(default_factory=dict)
    level: str = "OK"

    def line(self) -> str:
        """渲染为一行判据（判据词 ASCII、宽度对齐）。"""
        return f"{self.level:<4} {self.rid} {self.name:<14} {self.detail}"


# ─────────────────────────── 读取与解析辅助 ───────────────────────────

def _read_text(path: Path) -> str:
    """读 UTF-8 文本；失败时**抛异常**（不静默返回空串 —— 空与失败必须可区分）。"""
    return path.read_text(encoding="utf-8")


def read_corpus(path: Path) -> str:
    """读语料：文件直接读；目录则按名排序拼接其中的 `.md` / `.txt`（§10.3 的分母）。"""
    if path.is_dir():
        files = sorted(p for p in path.rglob("*")
                       if p.is_file() and p.suffix.lower() in (".md", ".txt"))
        if not files:
            raise FileNotFoundError(f"语料目录内无 .md/.txt 文件：{path}")
        return "".join(_read_text(p) for p in files)
    return _read_text(path)


def _raw_rows(text: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """按 §5.1 切列，但**保留重复代号行**（R3 需要）。

    与 `lexicon.parse_lexicon` 的差别：本函数不做校验、不丢坏行、不去重，
    只把「列名行 + 每行字典」摊平成 dict（缺列以空串占位）。
    """
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return [], []
    header = [c.strip() for c in lines[0].split("\t")]
    if not header or header == [""]:
        header = list(_DEFAULT_HEADER)
    rows: List[Dict[str, str]] = []
    for lineno, raw in enumerate(lines[1:], start=2):
        if raw.strip() == "":
            continue
        cells = raw.split("\t")
        row: Dict[str, str] = {}
        for i, col in enumerate(header):
            row[col] = cells[i] if i < len(cells) else ""
        row["__line__"] = str(lineno)
        rows.append(row)
    return header, rows


def _unescape(value: str, *, is_match: bool = False) -> str:
    """按 §5.1.1 还原字段（`match` 列的 `;` 交由 `split_aliases` 处理）。"""
    if is_match:
        return value
    return LX.unescape_field(value)


def _row_aliases(row: Dict[str, str]) -> Tuple[str, ...]:
    """一条字典项的别名集（§5.2 `match` 列，`;` 分隔）。"""
    return LX.split_aliases(row.get("match", ""))


def extract_index_line(text: str) -> Optional[str]:
    """从正文文件中抽出「词表（N）：…」那一行（§2.3 常驻侧就是这一行）。

    抽不到（正文里没有索引行）返回 `None`，由调用方改用字典 term 推导 ——
    绝不拿整篇正文当索引（那会把常驻成本算成基线，结论失真）。
    """
    m = _INDEX_LINE_RE.search(text)
    return m.group(0).strip() if m else None


def derived_index_line(terms: Sequence[str]) -> str:
    """按字典 term 推导索引行（无 `--index` 时的显式替代，绝不静默当成 0）。"""
    return f"**词表（{len(terms)}）**：" + "、".join(terms)


# ─────────────────────────── R1 字典膨胀 ───────────────────────────

def _check_r1(index_line: str, terms: Sequence[str], budget: int,
              origin: str) -> Check:
    """R1 字典膨胀：索引行长度 ≤ `--index-budget`（默认 300）。

    超限 → WARN，并给出「应并入字典文件的候选」= 按 term 长度降序取前若干条
    （条数 = 至少 1、至多 10，取到「删掉这些 term 后索引可落回预算内」为止）。
    索引源：`--index` 里抽到的「词表（N）」行；抽不到则用字典 term 推导。
    """
    n = len(index_line)
    data: Dict[str, Any] = {
        "index_len": n, "index_budget": budget, "index_origin": origin,
        "candidates": [],
    }
    src = "正文索引行" if origin == "file" else "字典 term 推导"
    if n <= budget:
        return Check("R1", "index_budget",
                     f"索引 {n} 字符 / 上限 {budget}（来源：{src}）", data, "OK")

    # 需要让出多少字符：按 term 长度降序移除，直到落回预算内
    need = n - budget
    freed = 0
    cands: List[str] = []
    for term in sorted((t for t in terms if t), key=len, reverse=True):
        if freed >= need or len(cands) >= 10:
            break
        cands.append(term)
        freed += len(term) + 1          # 「、」分隔符也一并省下
    data["candidates"] = cands
    return Check(
        "R1", "index_budget",
        f"索引 {n} 字符 > 上限 {budget}；应并入字典文件的候选（term 长→短）："
        f"{'、'.join(cands) if cands else '（无 term 可让）'}",
        data, "WARN")


# ─────────────────────────── R2 代号不可读 ───────────────────────────

def _check_r2(rows: Sequence[Dict[str, str]], index_line: str) -> Check:
    """R2 代号不可读：每条 entry 的 `term` 必须非空（§5.2）。

    * `term` 为空 → FAIL（依据 §5.2 必填；§8.1 `LX-W004` 只管「缺列」这一形态）
    * 索引行里出现裸 `@`/`^` 代号 → FAIL（索引侧必须是词名，不是符号串）
    * `term` 长度 < 2 或纯 ASCII 无字母 → WARN（读者无法据此联想）
    """
    missing = [r.get("code", "") or f"行{r['__line__']}"
               for r in rows if not _unescape(r.get("term", "")).strip()]
    bare = sorted(set(_BARE_CODE_RE.findall(index_line)))
    weak = []
    for r in rows:
        term = _unescape(r.get("term", "")).strip()
        if not term:
            continue
        if len(term) < 2 or (term.isascii() and not any(c.isalpha() for c in term)):
            weak.append(term)
    data = {"term_empty": missing, "bare_codes_in_index": bare, "weak_terms": weak}

    if missing or bare:
        parts = []
        if missing:
            parts.append(f"term 为空 {len(missing)} 条：{'、'.join(missing)}（§5.2 必填）")
        if bare:
            parts.append(f"索引行含裸代号 {bare}（索引须写 term）")
        return Check("R2", "term_readable", "；".join(parts), data, "FAIL")
    if weak:
        return Check("R2", "term_readable",
                     f"`term` 过短/无可读字母 {len(weak)} 条：{'、'.join(weak)}", data, "WARN")
    return Check("R2", "term_readable",
                 f"{len(rows)} 条 term 均非空；索引行无裸代号", data, "OK")


# ─────────────────────────── R3 映射漂移 ───────────────────────────

def _check_r3(rows: Sequence[Dict[str, str]], lex: LX.Lexicon,
              parse_errors: Sequence[str]) -> Check:
    """R3 映射漂移（违 §2.1 INV-2）。

    (a) 同一 `term` 被两个不同 code 使用 → FAIL
    (b) 同一 code 的 `rev` 发生变化 → FAIL（用 `src/lexicon.compare_rev` 比对
        同一文件内该 code 的首次定义与后续定义，二者不一致即漂移）
    (c) 两条 entry 的 `match` 别名有交集 → WARN
    """
    # (a) term → 多个 code
    by_term: Dict[str, List[str]] = {}
    for r in rows:
        term = _unescape(r.get("term", "")).strip()
        code = r.get("code", "").strip()
        if term and code:
            by_term.setdefault(term, [])
            if code not in by_term[term]:
                by_term[term].append(code)
    dup_terms = {t: c for t, c in by_term.items() if len(c) > 1}

    # (b) 同一 code 的 rev 变化 → 交给 lexicon.compare_rev
    by_code: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        code = r.get("code", "").strip()
        if code:
            by_code.setdefault(code, []).append(r)
    rev_drift: List[str] = []
    for code, group in by_code.items():
        if len(group) < 2:
            continue
        first, later = group[0], group[1]
        old = _lexicon_from_rows([first], lex.columns)
        new = _lexicon_from_rows([later], lex.columns)
        for diag in LX.compare_rev(old, new):
            rev_drift.append(f"{code}（行{first['__line__']}→行{later['__line__']}）："
                             f"{diag.code} {diag.message}")

    # (c) match 别名交集
    alias_owner: Dict[str, List[str]] = {}
    for r in rows:
        code = r.get("code", "").strip()
        for a in _row_aliases(r):
            if a:
                alias_owner.setdefault(a, [])
                if code and code not in alias_owner[a]:
                    alias_owner[a].append(code)
    alias_clash = {a: c for a, c in alias_owner.items() if len(c) > 1}

    data = {"same_term_two_codes": dup_terms, "rev_drift": rev_drift,
            "alias_overlap": alias_clash, "parse_errors": list(parse_errors)}

    if dup_terms or rev_drift or parse_errors:
        parts = [f"同 term 双 code {len(dup_terms)} 组：" +
                 "；".join(f"{t}→{c}" for t, c in dup_terms.items())] if dup_terms else []
        if rev_drift:
            parts.append(f"rev 漂移 {len(rev_drift)} 处：" + "；".join(rev_drift))
        if parse_errors:
            parts.append("字典解析错误：" + "；".join(parse_errors[:3]))
        return Check("R3", "mapping_drift", " ｜ ".join(parts), data, "FAIL")
    if alias_clash:
        return Check("R3", "mapping_drift",
                     f"`match` 别名跨条目重叠 {len(alias_clash)} 个：" +
                     "；".join(f"{a}→{c}" for a, c in alias_clash.items()),
                     data, "WARN")
    return Check("R3", "mapping_drift",
                 f"{len(by_code)} 个代号：term 唯一、rev 无漂移、别名无重叠", data, "OK")


def _lexicon_from_rows(rows: Sequence[Dict[str, str]], columns: Sequence[str]) -> LX.Lexicon:
    """把原始行拼成一个 `Lexicon`（供 `compare_rev` 使用；缺列取空串）。"""
    cols = [c for c in columns if not c.startswith("__")]
    if "rev" not in cols:
        cols = cols + ["rev"]
    entries: Dict[str, LX.Entry] = {}
    for r in rows:
        code = r.get("code", "").strip()
        if not code:
            continue
        entries[code] = LX.Entry(
            code=code,
            term=_unescape(r.get("term", "")),
            meaning=_unescape(r.get("meaning", "")),
            fidelity=r.get("fidelity", "") or ".~",
            detail_path=_unescape(r.get("detail_path", "")),
            match=r.get("match", ""),
            loss=r.get("loss", ""),
            rev=r.get("rev", ""),
            line=int(r.get("__line__", "0") or 0),
        )
    return LX.Lexicon(entries=entries, columns=list(cols))


# ─────────────────────────── R4 替换收益 ───────────────────────────

def _concept_freq(corpus: str, term: str, aliases: Sequence[str]) -> int:
    """概念在语料中的出现次数：取 term 与各别名中**出现最多者**。

    取 max（而非求和）是刻意的：别名常互为子串（如 term `字典` 与别名 `字典文件`），
    求和会把同一次出现数两遍，把「低频概念」误判成高频。
    """
    cands = {c for c in (term,) + tuple(aliases) if c}
    return max((corpus.count(c) for c in cands), default=0)


def _check_r4(rows: Sequence[Dict[str, str]], corpus: Optional[str]) -> Check:
    """R4 替换收益：`saving = freq * (len(meaning) - len(code))`；`saving <= 0` → WARN。

    `freq` 为概念在语料中的出现次数（口径见 `_concept_freq`）。
    末尾输出 `总节省字符 = N`。需要 `--corpus`（或用 `--index` 的正文当语料）。
    """
    if corpus is None:
        return Check("R4", "saving", "缺少 --corpus/--index，无法计算替换收益",
                     {"total_saving": None}, "WARN")
    bad: List[Dict[str, Any]] = []
    details: List[Dict[str, Any]] = []
    total = 0
    for r in rows:
        code = r.get("code", "").strip()
        if not code:
            continue
        term = _unescape(r.get("term", "")).strip()
        meaning = _unescape(r.get("meaning", ""))
        freq = _concept_freq(corpus, term, _row_aliases(r))
        saving = freq * (len(meaning) - len(code))
        total += saving
        details.append({"code": code, "freq": freq, "saving": saving})
        if saving <= 0:
            bad.append({"code": code, "freq": freq, "saving": saving,
                        "meaning_len": len(meaning), "code_len": len(code)})
    data = {"per_code": details, "low_yield": bad, "total_saving": total}
    if bad:
        return Check("R4", "saving",
                     f"低/负收益代号 {len(bad)} 条（不该编码）：" +
                     "；".join(f"{b['code']}(freq {b['freq']}, saving {b['saving']})"
                               for b in bad) +
                     f" ｜ 总节省字符 = {total}", data, "WARN")
    return Check("R4", "saving",
                 f"{len(details)} 条代号 saving 全为正 ｜ 总节省字符 = {total}",
                 data, "OK")


# ─────────────────────────── R5 压缩悖论 ───────────────────────────

def _check_r5(index_line: str, lexicon_len: int,
              corpus: Optional[str], origin: str) -> Check:
    """R5 压缩悖论（§10.3 的盈亏平衡）。

    `总成本 = 常驻成本（索引行长度）+ 一次性展开成本（字典文本长度 × 1)`；
    与 `基线 = 语料原文长度`（去换行）比。**总成本 > 基线 → FAIL**，三个数字全打印。
    """
    if corpus is None:
        return Check("R5", "breakeven", "缺少 --corpus/--index，无法算盈亏平衡",
                     {"resident": None, "oneoff": lexicon_len}, "WARN")
    baseline = len(corpus.replace("\n", "").replace("\r", ""))
    resident = len(index_line)
    oneoff = lexicon_len
    total = resident + oneoff
    src = "正文索引行" if origin == "file" else "字典 term 推导"
    data = {"resident": resident, "oneoff": oneoff, "total": total,
            "baseline": baseline, "index_origin": origin}
    msg = (f"常驻 {resident}（{src}）+ 展开 {oneoff} = 总成本 {total} / 基线 {baseline}")
    if total > baseline:
        return Check("R5", "breakeven", msg + " → 压缩为负（§10.3）", data, "FAIL")
    return Check("R5", "breakeven", msg, data, "OK")


# ─────────────────────────── R6 语义风险 ───────────────────────────

def _shared_run_letters(code_letters: str, term: str) -> int:
    """代号字母序列与 term 的**最长连续字母重合长度**（大小写不敏感）。"""
    low = term.lower()
    best = 0
    n = len(code_letters)
    for i in range(n):
        for j in range(n, i + 1, -1):
            piece = code_letters[i:j].lower()
            if len(piece) <= best:
                break
            if piece in low:
                best = len(piece)
    return best


def _term_initials(term: str) -> str:
    """term 各词首字母（仅 ASCII 字母；中文词无首字母）。"""
    words = [w for w in re.split(r"[\s_\-/]+", term) if w]
    return "".join(w[0] for w in words if w[0].isascii() and w[0].isalpha())


def _check_r6(rows: Sequence[Dict[str, str]], allow_seq: bool) -> Check:
    """R6 语义风险：代号应可联想。

    判据：代号去分隔符/数字后的**字母序列**，要么等于 term 各词首字母（缩写），
    要么与 term 有 **≥2 个连续字母**重合。不满足 → WARN（列出不透明代号）。
    纯顺序编号（如 `R1`/`C12`）默认 WARN（`--allow-seq` 可跳过）。
    """
    opaque: List[Dict[str, str]] = []
    for r in rows:
        code = r.get("code", "").strip()
        if not code:
            continue
        term = _unescape(r.get("term", "")).strip()
        if allow_seq and _SEQ_CODE_RE.match(code):
            continue
        letters = "".join(c for c in code if c.isalpha())
        initials = _term_initials(term)
        as_abbr = bool(initials) and letters.lower() == initials.lower()
        run = _shared_run_letters(letters, term) if letters and term else 0
        if as_abbr or run >= 2:
            continue
        reason = ("顺序编号" if _SEQ_CODE_RE.match(code) else
                  "无字母" if not letters else
                  f"最长连续字母重合 {run} < 2")
        opaque.append({"code": code, "term": term, "reason": reason})
    data = {"opaque": opaque, "allow_seq": allow_seq}
    if opaque:
        return Check("R6", "mnemonic",
                     f"不透明代号 {len(opaque)} 条（建议改用可联想缩写）：" +
                     "；".join(f"{o['code']}（{o['reason']}）" for o in opaque[:8]) +
                     ("…" if len(opaque) > 8 else ""), data, "WARN")
    return Check("R6", "mnemonic", f"{len(rows)} 条代号均可联想", data, "OK")


# ─────────────────────────── 汇总入口 ───────────────────────────

def audit_text(
    lexicon_text: str,
    *,
    index_text: Optional[str] = None,
    corpus_text: Optional[str] = None,
    index_budget: int = 300,
    allow_seq: bool = False,
) -> List[Check]:
    """对字典文本跑 R1~R6，返回 6 项判据（顺序固定 R1→R6）。

    本函数是**纯函数**（不读文件、不打印），供 CLI 与 `tests/run_tests.py` 共用。
    """
    header, rows = _raw_rows(lexicon_text)
    # 严格解析器（§5.1）：它的错误单独收进来，作为 R3 的结构性证据
    try:
        lex = LX.parse_lexicon(lexicon_text)
        parse_errors = [d.code for d in lex.errors]
    except LX.LexiconError as exc:
        lex = LX.Lexicon(entries={}, columns=list(header) or list(_DEFAULT_HEADER))
        parse_errors = [exc.code]

    terms = [_unescape(r.get("term", "")).strip() for r in rows]
    if index_text is not None:
        extracted = extract_index_line(index_text)
        if extracted is not None:
            index_line, origin = extracted, "file"
        else:
            index_line, origin = derived_index_line([t for t in terms if t]), "derived"
    else:
        index_line, origin = derived_index_line([t for t in terms if t]), "derived"

    corpus = corpus_text if corpus_text is not None else index_text
    return [
        _check_r1(index_line, terms, index_budget, origin),
        _check_r2(rows, index_line),
        _check_r3(rows, lex, parse_errors),
        _check_r4(rows, corpus),
        _check_r5(index_line, len(lexicon_text), corpus, origin),
        _check_r6(rows, allow_seq),
    ]


def _exit_code(checks: Sequence[Check]) -> int:
    """任一项 FAIL → 1；全 OK/WARN → 0。"""
    return 1 if any(c.level == "FAIL" for c in checks) else 0


# ─────────────────────────── 自证（--self-test） ───────────────────────────

_H = "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\trev"

# 样例字典（正例）：term 唯一、rev 稳定、别名无重叠、saving 为正、代号可联想
_DICT_OK = (
    _H + "\n"
    "OK1\tsilent fail\t静默失效：失败不报错（§9.3 ND-1）\t.~\tdocs/06_负例库.md\t静默失效\t\trev-a\n"
    "OK2\tbreakeven\t盈亏平衡判据（§10.3）\t.~\tdocs/05_引索符系统规范.md\t\t\trev-b\n"
    "OK3\tindex budget\t索引行上限（§2.3）\t.~\tdocs/05_引索符系统规范.md\t\t\trev-c\n"
)
_INDEX_OK = "**词表（3）**：OK1、OK2、OK3\n"
# 语料需足够长：R5 的基线是语料长度，样例必须留在盈亏平衡点**之上**，
# 否则正例会被 R5 正当地判成 FAIL（首版用 ×3 只有 180 字符，被测出）。
_CORPUS_OK = ("静默失效 silent fail 反复出现；盈亏平衡 breakeven 也要出现；"
              "index budget 同样出现。") * 10
# R1 反例用的超预算索引行（> 300 字符）
_INDEX_LONG = "**词表（20）**：" + "、".join(
    ["超长概念名称甲乙丙丁戊己庚辛壬癸子丑寅卯"] * 20)


def _self_cases() -> List[Tuple[str, str, str, Dict[str, Any]]]:
    """自证样例：(风险号, 说明, 期望级别, audit_text 关键字参数)。

    每项风险各给**正例 + 反例**，逐条断言该风险的级别确实被触发。
    """
    return [
        # R1
        ("R1", "正例：索引 30 字符 ≤ 预算 300", "OK",
         dict(lexicon_text=_DICT_OK, index_text=_INDEX_OK, corpus_text=_CORPUS_OK,
              index_budget=300)),
        ("R1", "反例：索引远超预算且给出候选", "WARN",
         dict(lexicon_text=_DICT_OK, index_text=_INDEX_LONG, corpus_text=_CORPUS_OK,
              index_budget=300)),
        # R2
        ("R2", "正例：term 非空、索引无裸代号", "OK",
         dict(lexicon_text=_DICT_OK, index_text=_INDEX_OK, corpus_text=_CORPUS_OK)),
        ("R2", "反例：索引行写裸代号 @OK1", "FAIL",
         dict(lexicon_text=_DICT_OK, index_text="**词表（1）**：@OK1\n",
              corpus_text=_CORPUS_OK)),
        ("R2", "反例：term 列为空", "FAIL",
         dict(lexicon_text=_H + "\n" + "OK1\t\t含义\t.~\tp.md\t\t\trev-a\n",
              index_text=_INDEX_OK, corpus_text=_CORPUS_OK)),
        # R3
        ("R3", "正例：term 唯一、rev 稳定、别名无重叠", "OK",
         dict(lexicon_text=_DICT_OK, index_text=_INDEX_OK, corpus_text=_CORPUS_OK)),
        ("R3", "反例：同一 term 被两个 code 使用", "FAIL",
         dict(lexicon_text=(
             _H + "\n"
             "AAA\tsilent fail\t含义甲\t.~\tp.md\t\t\trev-a\n"
             "BBB\tsilent fail\t含义乙\t.~\tp.md\t\t\trev-b\n"),
             index_text=_INDEX_OK, corpus_text=_CORPUS_OK)),
        ("R3", "反例：同一 code 的 rev 变化", "FAIL",
         dict(lexicon_text=(
             _H + "\n"
             "AAA\tsilent fail\t含义甲\t.~\tp.md\t\t\trev-a\n"
             "AAA\tother term\t含义乙\t.~\tp.md\t\t\trev-zzz\n"),
             index_text=_INDEX_OK, corpus_text=_CORPUS_OK)),
        ("R3", "反例：两条 entry 的 match 别名重叠", "WARN",
         dict(lexicon_text=(
             _H + "\n"
             "AAA\tsilent fail\t含义甲\t.~\tp.md\t共享别名\t\trev-a\n"
             "BBB\tother term\t含义乙\t.~\tp.md\t共享别名\t\trev-b\n"),
             index_text=_INDEX_OK, corpus_text=_CORPUS_OK)),
        # R4
        ("R4", "正例：saving 全为正", "OK",
         dict(lexicon_text=_DICT_OK, index_text=_INDEX_OK, corpus_text=_CORPUS_OK)),
        ("R4", "反例：meaning 比 code 短 → saving ≤ 0", "WARN",
         dict(lexicon_text=_H + "\n" + "SILENT-LONG\t静默失效\t短\t.~\tp.md\t\t\trev-a\n",
              index_text=_INDEX_OK, corpus_text=_CORPUS_OK)),
        # R5
        ("R5", "正例：总成本 < 基线", "OK",
         dict(lexicon_text=_DICT_OK, index_text=_INDEX_OK, corpus_text=_CORPUS_OK)),
        ("R5", "反例：基线极短 → 总成本 > 基线", "FAIL",
         dict(lexicon_text=_DICT_OK, index_text=_INDEX_OK, corpus_text="静默失效")),
        # R6
        ("R6", "正例：代号与 term 有连续字母重合", "OK",
         dict(lexicon_text=_H + "\n"
              "SF\tsilent fail\t静默失效\t.~\tp.md\t\t\trev-a\n",
              index_text=_INDEX_OK, corpus_text=_CORPUS_OK)),
        ("R6", "反例：代号与 term 不可联想", "WARN",
         dict(lexicon_text=_H + "\n"
              "ZZ\tsilent fail\t静默失效\t.~\tp.md\t\t\trev-a\n",
              index_text=_INDEX_OK, corpus_text=_CORPUS_OK)),
        ("R6", "反例：顺序编号 R1 默认 WARN；--allow-seq 跳过", "OK",
         dict(lexicon_text=_H + "\n"
              "R1\t静默失效\t静默失效\t.~\tp.md\t\t\trev-a\n",
              index_text=_INDEX_OK, corpus_text=_CORPUS_OK, allow_seq=True)),
    ]


def self_test() -> int:
    """内嵌样例自证：6 项判据各自能被触发（正例 + 反例），并核验退出码语义。"""
    failures = 0
    for rid, label, expect, kwargs in _self_cases():
        checks = audit_text(**kwargs)
        got = next((c for c in checks if c.rid == rid), None)
        level = got.level if got else "(缺项)"
        ok = level == expect
        if not ok:
            failures += 1
        print(f"{'OK' if ok else 'FAIL':<4} {rid} {label}"
              f"  期望 {expect} / 实际 {level}")
        if not ok and got is not None:
            print(f"       {got.detail}")

    # 退出码语义：FAIL 项存在 → 1，否则 0
    bad = audit_text(lexicon_text=_H + "\n" + "OK1\t\t含义\t.~\tp.md\t\t\trev-a\n",
                     index_text=_INDEX_OK, corpus_text=_CORPUS_OK)
    code_bad = _exit_code(bad)
    good = audit_text(lexicon_text=_DICT_OK, index_text=_INDEX_OK, corpus_text=_CORPUS_OK)
    code_good = _exit_code(good)
    for label, got, want in (("有 FAIL → 退出码 1", code_bad, 1),
                             ("全 OK/WARN → 退出码 0", code_good, 0)):
        ok = got == want
        failures += 0 if ok else 1
        print(f"{'OK' if ok else 'FAIL':<4} exit {label}  期望 {want} / 实际 {got}")

    print(f"\n自证：{'全部通过' if failures == 0 else f'{failures} 项不符'}"
          f"（{len(_self_cases())} 组样例 + 2 项退出码语义）")
    return 1 if failures else 0


# ─────────────────────────── CLI ───────────────────────────

_USAGE = """用法：
  python tools/lexicon_audit.py <字典.tsv> [--index <正文文件>] [--corpus <语料文件或目录>]
      [--root <目录>] [--index-budget 300] [--json] [--self-test]

判据（R1~R6，逐项一行）：
  R1 index_budget    索引行长度 ≤ 预算，超限报应并入字典的候选
  R2 term_readable   term 非空（§5.2）；索引行不得出现裸 @/^ 代号
  R3 mapping_drift   同 term 双 code / rev 漂移（§2.1 INV-2）/ 别名重叠
  R4 saving          saving = freq × (len(meaning) − len(code))，≤0 即低频概念不该编码
  R5 breakeven       常驻成本 + 展开成本 vs 基线（§10.3），超基线即压缩为负
  R6 mnemonic        代号应可联想（缩写或与 term 有 ≥2 连续字母重合）

退出码：任一项 FAIL → 1；全 OK/WARN → 0。
"""


def _resolve(path_str: str, root: Optional[Path]) -> Path:
    """解析路径：先按给定路径，再按 `--root` 拼一次（相对字典侧的常见写法）。"""
    p = Path(path_str)
    if p.exists() or root is None:
        return p
    cand = root / path_str
    return cand if cand.exists() else p


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 入口：解析参数 → 跑 R1~R6 → 打印逐项判据（或 JSON）。"""
    try:                                    # Windows 控制台 UTF-8（GBK 会吞中文）
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        return self_test()
    if not args or args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0

    as_json = "--json" in args
    index_budget = 300
    allow_seq = "--allow-seq" in args
    root: Optional[Path] = None
    index_path: Optional[str] = None
    corpus_path: Optional[str] = None
    positional: List[str] = []

    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--json", "--allow-seq"):
            i += 1
        elif a in ("--index", "--corpus", "--root", "--index-budget") and i + 1 < len(args):
            if a == "--index":
                index_path = args[i + 1]
            elif a == "--corpus":
                corpus_path = args[i + 1]
            elif a == "--root":
                root = Path(args[i + 1])
            else:
                try:
                    index_budget = int(args[i + 1])
                except ValueError:
                    print(f"FAIL --index-budget 不是整数：{args[i + 1]!r}")
                    return 2
            i += 2
        else:
            positional.append(a)
            i += 1

    if not positional:
        print(_USAGE)
        return 2
    lex_path = _resolve(positional[0], root)
    if not lex_path.exists():
        print(f"FAIL 字典文件不存在：{lex_path}")
        return 2

    try:
        lex_text = _read_text(lex_path)
        index_text = _read_text(_resolve(index_path, root)) if index_path else None
        corpus_text = (read_corpus(_resolve(corpus_path, root)) if corpus_path else None)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"FAIL 读入失败（UTF-8 必需）：{exc}")
        return 2

    checks = audit_text(lex_text, index_text=index_text, corpus_text=corpus_text,
                        index_budget=index_budget, allow_seq=allow_seq)
    code = _exit_code(checks)

    if as_json:
        print(json.dumps({
            "lexicon": str(lex_path),
            "index": index_path or "",
            "corpus": corpus_path or "",
            "index_budget": index_budget,
            "allow_seq": allow_seq,
            "exit_code": code,
            "checks": [{"id": c.rid, "name": c.name, "level": c.level,
                        "detail": c.detail, "data": c.data} for c in checks],
        }, ensure_ascii=False, indent=2))
        return code

    print("=" * 78)
    print(" 字典可用性审计 lexicon_audit（R1~R6）")
    print("=" * 78)
    print(f" 字典：{lex_path}")
    if index_path:
        print(f" 索引：{index_path}")
    if corpus_path:
        print(f" 语料：{corpus_path}")
    print("")
    for c in checks:
        print(" " + c.line())
    print("")
    print(f" 小结：OK={sum(1 for c in checks if c.level == 'OK')}"
          f" ｜ WARN={sum(1 for c in checks if c.level == 'WARN')}"
          f" ｜ FAIL={sum(1 for c in checks if c.level == 'FAIL')}"
          f" ｜ 退出码={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
