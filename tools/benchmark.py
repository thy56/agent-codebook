#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""benchmark.py — 多文件基准：把「压缩收益」从单点实测升级为对照实验（R7）。

规范来源：`docs/05_引索符系统规范.md`
  §10.3 盈亏平衡判据（基线 / 常驻成本）→ 本工具逐文件算比值
  §9.1 / §9.2 展开六步与成功判据 → 往返完整性由 `src/decode.expand_file` 真跑
  §3.3  `dict` 定位（字典与符号串分离，§2.1 INV-1）

为什么需要它（R7「未经大规模基准验证」）：
  单点实测（一个文件、一份字典）**不能**支撑「压缩有效」的结论 ——
  短文件会被头部固定开销吃掉，概念密度的差异也会让不同文件结论相反。
  故本工具对**每个**输入文件分别算基线 / 常驻 / 比值，并做**真实展开**证明可逆，
  最后给聚合统计。任一片面结论都会在聚合里显形。

口径（与 `src/encode.py` 的 `EncodeResult` 一致）：
  基线      = 文件字符数（**不含换行**）
  常驻      = 头部字符数 + 替换后正文（**不含换行**）
  比值      = 常驻 / 基线（< 1 才是真的变短）
  命中概念  = 字典 term 或 `match` 别名在文件中的出现（**单遍最长匹配**，不重复替换）

CLI：
    python tools/benchmark.py <文件或目录>... --dict <字典.tsv> [--root <目录>]
        [--json] [--max-files 50]

零第三方依赖（仅标准库）。判据词只用 ASCII（OK / FAIL）——
Windows GBK 控制台会把 ✅/❌ 渲染成同一个 "?"。
退出码：0 = 全部文件往返完整；1 = 存在完整性失败；2 = 用法/路径错误。
"""
from __future__ import annotations

import io
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import decode as D              # noqa: E402  （§9.1 六步展开 / §9.2 成功判据）
import lexicon as LX           # noqa: E402  （§5.1 TSV 读写）

__all__ = ["FileResult", "BenchResult", "run_benchmark", "collect_files",
           "build_stream"]

_TEXT_SUFFIXES: Tuple[str, ...] = (".md", ".txt", ".rst", ".py", ".json", ".tsv")
# 头部只声明 dict（§3.3 唯一必填属性）—— 本工具不产出模态/前缀，故不需 mods / ns
_HEADER = "#LX1 dict={dict_name}"


@dataclass
class FileResult:
    """单个文件的基准结果。"""

    path: str
    baseline: int = 0
    resident: int = 0
    header: int = 0
    matched: int = 0                # 命中概念的出现次数
    codes: List[str] = field(default_factory=list)
    ratio: float = 1.0
    integrity: str = "OK"           # OK / FAIL
    detail: str = ""
    skipped: bool = False           # 无命中概念 → 不计入比值与完整性分母


@dataclass
class BenchResult:
    """一次基准运行的聚合结果。"""

    dict_path: str = ""
    files: List[FileResult] = field(default_factory=list)
    files_considered: int = 0       # 实际纳入统计的文件数（已参与压缩的）
    files_skipped: int = 0          # 无命中概念 / 被 --max-files 截掉
    baseline_total: int = 0
    resident_total: int = 0
    integrity_pass: int = 0
    integrity_fail: int = 0
    truncated: int = 0              # 因 --max-files 未纳入的文件数
    dict_len: int = 0               # 外置字典长度（一次性展开成本，§10.3）

    @property
    def ratio(self) -> float:
        if self.baseline_total == 0:
            return 1.0
        return round(self.resident_total / self.baseline_total, 4)

    @property
    def exit_code(self) -> int:
        return 1 if self.integrity_fail else 0


# ─────────────────────────── 文件收集与替换 ───────────────────────────

def collect_files(inputs: Sequence[str], *, root: Optional[Path] = None,
                  max_files: int = 50) -> Tuple[List[Path], int]:
    """展开输入为文件列表（目录递归取文本类文件），并应用 `--max-files`。

    返回 `(纳入的文件, 被截掉的文件数)`。被截掉的数量**如实返回**，不静默丢弃。
    """
    out: List[Path] = []
    for raw in inputs:
        p = Path(raw) if root is None or Path(raw).exists() else root / raw
        if p.is_dir():
            out.extend(sorted(q for q in p.rglob("*")
                              if q.is_file() and q.suffix.lower() in _TEXT_SUFFIXES))
        else:
            out.append(p)
    seen: set[str] = set()
    uniq: List[Path] = []
    for p in out:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    if max_files > 0 and len(uniq) > max_files:
        return uniq[:max_files], len(uniq) - max_files
    return uniq, 0


def _search_keys(lexicon: LX.Lexicon) -> List[Tuple[str, str]]:
    """展开搜索表：`(待匹配字面串, 代号)`，按字面串长度**降序**（最长匹配优先）。"""
    table: List[Tuple[str, str]] = []
    for code, entry in lexicon.entries.items():
        keys = {entry.term, *entry.aliases}
        for k in keys:
            if k and k.strip():
                table.append((k, code))
    table.sort(key=lambda kv: len(kv[0]), reverse=True)
    return table


def compress_text(text: str, table: Sequence[Tuple[str, str]]) -> Tuple[str, List[str]]:
    """把文本里命中字典的概念改成 `@code`（单遍最长匹配）。

    返回 `(替换后文本, 按出现顺序的代号列表)`。单遍 `re.sub` 保证**不会**
    在刚替换出来的 `@code` 上二次替换（逐条 `str.replace` 会）。
    """
    if not table:
        return text, []
    mapping = dict(table)
    pattern = re.compile("|".join(re.escape(k) for k, _ in table))
    codes: List[str] = []

    def _sub(m: re.Match[str]) -> str:
        key = m.group(0)
        code = mapping[key]
        codes.append(code)
        return f"@{code}"

    return pattern.sub(_sub, text), codes


def build_stream(codes: Sequence[str], dict_name: str) -> str:
    """为该文件命中概念的**出现序列**构造符号串（§3.1）。

    形态：`#LX1 dict=<name>` + `@C1|@C2|…;;` —— 每个命中的概念一个 atom，
    顺序与在文件中出现的顺序一致（并列符 `|`，§4.4）。
    """
    header = _HEADER.format(dict_name=dict_name)
    body = "|".join(f"@{c}" for c in codes) + ";;"
    return f"{header}\n{body}\n"


# ─────────────────────────── 主流程 ───────────────────────────

def _count_chars(text: str) -> int:
    """字符数（**不含换行**）—— 与 §16.2 的计数口径一致。"""
    return len(text.replace("\n", "").replace("\r", ""))


def run_benchmark(
    inputs: Sequence[str],
    dict_path: Path,
    *,
    root: Optional[Path] = None,
    max_files: int = 50,
    tmp_dir: Optional[Path] = None,
) -> BenchResult:
    """对每个输入文件算基线/常驻/比值，并用 `expand_file` **真跑**往返完整性。

    往返完整性的判据（三条同时满足才算 OK）：
      1. 替换后正文里的每个 `@code` 都能在字典里查到
      2. 该文件的符号串经 `src/decode.expand_file` 成功展开（§9.2 成功判据）
      3. 展开出的含义序列与字典逐一对应（顺序一致、无占位）
    """
    lexicon = LX.load(dict_path)
    if not lexicon.ok:
        raise LX.LexiconError(
            "LX-E010", f"字典存在结构性错误，基准结论不可信：{lexicon.errors}")

    files, truncated = collect_files(inputs, root=root, max_files=max_files)
    res = BenchResult(dict_path=str(dict_path), truncated=truncated,
                      dict_len=_count_chars(dict_path.read_text(encoding="utf-8")))
    table = _search_keys(lexicon)

    own_tmp = tmp_dir is None
    tmp_ctx = tempfile.TemporaryDirectory() if own_tmp else None
    base = Path(tmp_ctx.name) if own_tmp else Path(str(tmp_dir))
    try:
        lex_copy = base / "lexicon.tsv"
        lex_copy.write_text(dict_path.read_text(encoding="utf-8"),
                            encoding="utf-8", newline="\n")
        for i, path in enumerate(files):
            item = _bench_one(path, table, lexicon, base, lex_copy, i)
            res.files.append(item)
            if item.skipped:
                res.files_skipped += 1
                continue
            res.files_considered += 1
            res.baseline_total += item.baseline
            res.resident_total += item.resident
            if item.integrity == "OK":
                res.integrity_pass += 1
            else:
                res.integrity_fail += 1
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()
    return res


def _bench_one(path: Path, table: Sequence[Tuple[str, str]], lexicon: LX.Lexicon,
               base: Path, lex_copy: Path, index: int) -> FileResult:
    """算一个文件：基线 / 常驻 / 比值 / 完整性（往返真跑）。"""
    item = FileResult(path=str(path))
    if not path.exists():
        # 文件不存在是**真失败**，不是「跳过」——不计入跳过数，计入完整性失败
        item.integrity = "FAIL"
        item.detail = "文件不存在（无法参与基准，不得静默当作已通过）"
        return item

    text = path.read_text(encoding="utf-8")
    item.baseline = _count_chars(text)
    compressed, codes = compress_text(text, table)
    header = _HEADER.format(dict_name=lex_copy.name)
    item.header = _count_chars(header)
    item.resident = item.header + _count_chars(compressed)
    item.matched = len(codes)
    item.codes = codes
    item.ratio = round(item.resident / item.baseline, 4) if item.baseline else 1.0

    if not codes:
        # 无命中概念 → 不产出符号串；如实标记，**不计入**比值与完整性分母
        item.skipped = True
        item.detail = "无命中概念（该文件不含字典中的 term/别名）"
        return item

    unknown = sorted({c for c in codes if c not in lexicon.entries})
    if unknown:
        item.integrity = "FAIL"
        item.detail = f"替换结果含字典未定义的代号 {unknown}"
        return item

    stream_path = base / f"stream_{index:03d}.lx"
    stream_path.write_text(build_stream(codes, lex_copy.name),
                           encoding="utf-8", newline="\n")
    try:
        exp = D.expand_file(stream_path, lex_copy)
    except D.DecodeError as exc:
        item.integrity = "FAIL"
        item.detail = f"展开失败：{exc.code} {exc.message}"
        return item

    expected = [lexicon.get(c).meaning for c in codes]
    if not exp.ok or exp.meanings() != expected:
        item.integrity = "FAIL"
        got = len(exp.meanings())
        item.detail = (f"展开结果与字典不符（期望 {len(expected)} 条 / 实得 {got} 条；"
                       f"§9.2 判据 3）")
        return item

    item.integrity = "OK"
    item.detail = f"{item.matched} 个概念展开一致"
    return item


# ─────────────────────────── 报告 ───────────────────────────

_USAGE = """用法：
  python tools/benchmark.py <文件或目录>... --dict <字典.tsv> [--root <目录>]
      [--json] [--max-files 50]

对每个输入文件算：基线（不含换行）/ 压缩后常驻（头部 + 替换后正文）/ 比值 /
往返完整性（`src/decode.expand_file` 真跑展开，§9.2）。
末尾给聚合统计与整体比 —— 单点实测不足以支撑结论（R7）。

退出码：0 = 全部文件往返完整；1 = 存在完整性失败；2 = 用法/路径错误。
"""


def _print_report(res: BenchResult, as_json: bool, max_files: int) -> None:
    """打印逐文件结果与聚合统计（或 JSON）。"""
    if as_json:
        print(json.dumps({
            "dict": res.dict_path,
            "dict_chars": res.dict_len,
            "max_files": max_files,
            "truncated": res.truncated,
            "files_considered": res.files_considered,
            "files_skipped": res.files_skipped,
            "baseline_total": res.baseline_total,
            "resident_total": res.resident_total,
            "ratio": res.ratio,
            "integrity_pass": res.integrity_pass,
            "integrity_fail": res.integrity_fail,
            "exit_code": res.exit_code,
            "files": [{
                "path": f.path, "baseline": f.baseline, "resident": f.resident,
                "header": f.header, "matched": f.matched, "ratio": f.ratio,
                "integrity": f.integrity, "detail": f.detail, "skipped": f.skipped,
            } for f in res.files],
        }, ensure_ascii=False, indent=2))
        return

    print("=" * 78)
    print(" 多文件基准 benchmark（R7 对照实验）")
    print("=" * 78)
    print(f" 字典：{res.dict_path}（{res.dict_len} 字符，外置不计常驻）")
    print(f" 文件：纳入 {res.files_considered} ｜ 无命中/跳过 {res.files_skipped}"
          f" ｜ 超 --max-files 未纳入 {res.truncated}")
    print("")
    print(f" {'判据':<4} {'文件':<40} {'基线':>7} {'常驻':>7} {'比值':>6}")
    print(" " + "-" * 74)
    for f in res.files:
        name = Path(f.path).name
        if len(name) > 38:
            name = name[:35] + "..."
        tag = f.integrity if not f.skipped else "SKIP"
        print(f" {tag:<4} {name:<40} {f.baseline:>7} {f.resident:>7} {f.ratio:>6.3f}")
    print(" " + "-" * 74)
    print(" 聚合：")
    print(f"   文件数        {res.files_considered}（另 {res.files_skipped} 个无命中概念）")
    print(f"   基线合计      {res.baseline_total} 字符")
    print(f"   压缩后合计    {res.resident_total} 字符")
    print(f"   整体比        {res.ratio}")
    print(f"   完整性通过    {res.integrity_pass}/{res.files_considered}")
    fails = [f for f in res.files if f.integrity == "FAIL"]
    if fails:
        print("   完整性失败明细：")
        for f in fails:
            print(f"     - {Path(f.path).name}：{f.detail}")
    print(f" 退出码：{res.exit_code}")
    print("=" * 78)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 入口。"""
    try:                                    # Windows 控制台 UTF-8（GBK 会吞中文）
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0

    as_json = "--json" in args
    max_files = 50
    dict_arg: Optional[str] = None
    root: Optional[Path] = None
    positional: List[str] = []

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            i += 1
        elif a in ("--dict", "--root", "--max-files") and i + 1 < len(args):
            if a == "--dict":
                dict_arg = args[i + 1]
            elif a == "--root":
                root = Path(args[i + 1])
            else:
                try:
                    max_files = int(args[i + 1])
                except ValueError:
                    print(f"FAIL --max-files 不是整数：{args[i + 1]!r}")
                    return 2
            i += 2
        else:
            positional.append(a)
            i += 1

    if not positional or dict_arg is None:
        print("FAIL 需要至少一个输入文件/目录，且必须给 --dict")
        print(_USAGE)
        return 2

    dict_path = Path(dict_arg)
    if not dict_path.exists() and root is not None and (root / dict_arg).exists():
        dict_path = root / dict_arg
    if not dict_path.exists():
        print(f"FAIL 字典文件不存在：{dict_path}")
        return 2

    try:
        res = run_benchmark(positional, dict_path, root=root, max_files=max_files)
    except (LX.LexiconError, OSError, UnicodeDecodeError) as exc:
        print(f"FAIL 基准无法进行（不静默出结论）：{exc}")
        return 2

    _print_report(res, as_json, max_files)
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
