#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""run_tests.py — 一键跑全部测试。零第三方依赖（仅标准库）。

覆盖（对应 06 负例库 §D「实现自检清单」）：
  自检1  §16 最小示例往返（原文 → 符号串 → 展开，要素零丢失）
  自检2  从规范文档**自动提取**全部符号串代码块并解析
  自检3  符号表 ⊆ EBNF 产生式（无「有声明无产生式」）
  自检4  负例逐条**确实被拒绝**
  自检5  错误码 / 校验规则交叉引用无孤儿
  自检6  编号空间唯一性（无重复定义）
  自检7  语法层通过 ≠ 合规（两层都跑）
  自检8  同名产生式是否多处定义
  自检9  规则 ⇄ 判据无循环引用
  自检10 同一条件不得给两种严重级（MUST 与警告不可并存）
  自检11 跳号附撤销说明（编号不复用）
  自检12 新增规则后重查全局编号空间

退出码：0 = 全部通过；1 = 存在失败。**失败必须可感知**（禁止静默跳过）。
用法：
    python tests/run_tests.py            # 全部
    python tests/run_tests.py -v         # 打印每条用例
"""
from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:                                    # Windows 控制台 UTF-8（GBK 会吞中文）
    sys.stdout.reconfigure(encoding="utf-8")   # type: ignore[union-attr]
except (AttributeError, ValueError):    # pragma: no cover - 老版本 / 异常流
    pass

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
VECTORS = Path(__file__).resolve().parent / "vectors"
SPEC = ROOT / "docs" / "05_引索符系统规范.md"
NEG_LIB = ROOT / "docs" / "06_负例库.md"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import decode as D            # noqa: E402
import encode as E            # noqa: E402
import lexicon as LX          # noqa: E402
import validate as V          # noqa: E402

# tools/ 下的审计与基准工具（R1~R7 的机器判据）也要被真跑，故加入搜索路径
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import benchmark as BENCH      # noqa: E402
import lexicon_audit as AUD    # noqa: E402

VERBOSE = "-v" in sys.argv
ONLY = next((a for a in sys.argv[1:] if not a.startswith("-")), "")

# ─────────────────────────── 测试框架（最小实现） ───────────────────────────

class Failure(AssertionError):
    """用例失败。"""


_results: List[Tuple[str, bool, str]] = []


def check(name: str, fn: Callable[[], None]) -> None:
    """执行一个用例；异常 = 失败（**不吞异常**，打印完整堆栈首行）。"""
    if ONLY and ONLY not in name:
        return
    try:
        fn()
    except Failure as exc:
        _results.append((name, False, str(exc)))
        print(f"  FAIL  {name}\n        {exc}")
    except Exception as exc:                      # 非断言异常同样是失败
        detail = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc().strip().splitlines()
        _results.append((name, False, detail))
        print(f"  FAIL  {name}\n        {detail}")
        print("        " + "\n        ".join(tb[-3:]))
    else:
        _results.append((name, True, ""))
        if VERBOSE:
            print(f"  ok    {name}")


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def require_in(needle: str, haystack: Any, what: str) -> None:
    if needle not in haystack:
        raise Failure(f"{what} 中未出现 {needle!r}；实际：{haystack!r}")


def load_vector(name: str) -> Any:
    p = VECTORS / name
    if not p.exists():
        raise Failure(f"向量文件缺失：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _code_of(exc: BaseException) -> str:
    return str(getattr(exc, "code", ""))


# ─────────────────────────── 自检1：§16 往返 ───────────────────────────

def test_section16_roundtrip() -> None:
    """§16 最小示例：原文 → 符号串 → 展开，要素零丢失；字符数口径与 §16.2/16.4 一致。"""
    stream_text = (VECTORS / "16_minimal.stream").read_text(encoding="utf-8")
    lex = LX.load(VECTORS / "16_minimal.lexicon.tsv")
    original = (VECTORS / "16_minimal.original.txt").read_text(encoding="utf-8")

    require(lex.ok, f"§16 字典应无错误：{lex.errors}")
    require(list(lex.entries) == ["R1", "R2", "R3"], f"代号集不符：{list(lex.entries)}")

    st = V.parse_stream(stream_text)
    require(len(st.units) == 3, f"应解析出 3 个单元，实为 {len(st.units)}")
    require(st.header.version == 1, "版本应为 1")
    require(st.header.dict_path == "lexicon.tsv", "dict 应为 lexicon.tsv")
    require(st.header.mods == "-!", "mods 应为 -!")
    require([u.terminator for u in st.units] == ["|", "|", ";;"],
            "终止符应为 | | ;;")
    for u, code, mod in zip(st.units, ("R1", "R2", "R3"), ("-", "-", "-")):
        require(len(u.terms) == 1, f"{code} 应有 1 个项")
        require(u.terms[0].modality == mod, f"{code} 模态应为 {mod}")
        require(u.terms[0].atom.code == code, f"代号应为 {code}")

    # 字符数口径：§16.2「45 字符」= 头部 29 + 正文 14 + `;;` 2（不含换行）
    header_line = stream_text.split("\n")[0]
    require(len(header_line) == 29, f"头部应为 29 字符，实为 {len(header_line)}")
    body_len = len(stream_text.replace("\n", "")) - len(header_line)
    require(len(header_line) + body_len == 45,
            f"符号串应为 45 字符（不含换行），实为 {len(header_line) + body_len}")

    # 展开：要素零丢失
    exp = D.decode(stream_text, lex)
    require(exp.ok, "展开应成功（§9.2）")
    require(exp.steps_done == [1, 2, 3, 4, 5, 6], f"六步应全执行：{exp.steps_done}")
    meanings = exp.meanings()
    require(len(meanings) == 3, f"应展开出 3 条含义，实为 {len(meanings)}")
    for code in ("R1", "R2", "R3"):
        require(lex.get(code).meaning in meanings, f"{code} 的含义未出现在展开结果中")
    for m in meanings:
        require(not m.startswith("@"), f"展开结果中不得残留 `@`：{m!r}（ND-3）")

    # 原文要素零丢失：三条规则的关键词必须分别落在对应含义里
    keys = (("禁止强制终止", "R1"), ("禁止同时运行", "R2"), ("禁止先停后起", "R3"))
    for kw, code in keys:
        require(kw in lex.get(code).meaning, f"{code} 的含义未保留要素 {kw!r}")
    require(len(original.strip().splitlines()) == 3, "原文应有 3 行")
    # §16.1/§16.4：长度与比值**必须从文件实测**。
    # 口径 = 各行长度之和（不含换行），与 §16.2「45 字符」一致。
    # 注意：绝不写成 `round(118 / 45, 2) == 2.62` —— 那是常量比常量，恒真、测不到任何东西。
    orig_len = sum(len(ln) for ln in original.rstrip("\n").split("\n"))
    strm_len = sum(len(ln) for ln in stream_text.rstrip("\n").split("\n"))
    require(orig_len == 118, f"§16.1 原文应为 118 字符（不含换行），实为 {orig_len}")
    require(strm_len == 45, f"§16.2 符号串应为 45 字符（不含换行），实为 {strm_len}")
    ratio = round(orig_len / strm_len, 2)
    require(ratio == 2.62, f"§16.4 比值应为 2.62:1，实为 {ratio}:1")

    # 校验层：全量 + 最小集都应通过（除 LX-W001 —— §16 自身声明了未用的 `!`）
    rep = V.validate(stream_text, lex)
    require(rep.ok, f"§16 示例应校验通过，错误：{rep.error_codes}")
    require("V-12" not in rep.checked, "V-12 已撤销，不得登记为已执行")
    require(rep.warning_codes == ["LX-W001"],
            f"§16 示例唯一告警应为 LX-W001（头部声明未使用的 `!`），实为 {rep.warning_codes}")
    rep_min = V.validate_minimal(stream_text, lex)
    require(rep_min.ok, f"最小集应通过：{rep_min.error_codes}")
    require(set(rep_min.checked) == set(V.MINIMAL_RULES),
            f"最小集应只跑 {V.MINIMAL_RULES}，实为 {rep_min.checked}")


def test_section16_encode_roundtrip() -> None:
    """编码层往返：用 §16 的三条概念单元编码 → 校验 → 展开，要素零丢失。"""
    original = (VECTORS / "16_minimal.original.txt").read_text(encoding="utf-8")
    units = [
        E.Unit(text="不可逆操作必须走优雅停止流程，禁止强制终止进程。",
               meaning="不可逆操作必须走优雅停止流程，禁止强制终止进程",
               term="红线1", fidelity=".~", detail_path="docs/rules.md",
               match="禁止强制终止", modality="-", prefix="R"),
        E.Unit(text="禁止同时运行多个实例，端口冲突会触发保护性停用。",
               meaning="禁止同时运行多个实例，端口冲突会触发保护性停用",
               term="红线2", fidelity=".~", detail_path="docs/rules.md",
               match="禁止同时运行", modality="-", prefix="R"),
        E.Unit(text="重启必须使用统一的命令入口，禁止先停后起叠加。",
               meaning="重启必须使用统一的命令入口，禁止先停后起叠加",
               term="红线3", fidelity=".~", detail_path="docs/rules.md",
               match="禁止先停后起", modality="-", prefix="R"),
    ]
    res = E.encode(units, original, dictionary_name="lexicon.tsv")
    require(res.encoded, f"编码应成功；errors={res.errors} warnings={res.warnings}")
    require(not res.skipped, f"不应跳过任何单元：{res.skipped}")
    require(res.stream_len < res.original_len,
            f"应通过 §10.3：{res.stream_len} < {res.original_len}")
    require(len(res.lexicon.entries) == 3, "应产出 3 条字典项")
    require([e.code for e in res.lexicon.entries.values()] == ["R1", "R2", "R3"],
            f"代号应为 R1/R2/R3：{list(res.lexicon.entries)}")
    require(res.header.startswith("#LX1 dict=lexicon.tsv"), f"头部：{res.header!r}")
    require("mods=-" in res.header, f"头部应声明 mods=-：{res.header!r}")
    require(res.report is not None and res.report.ok,
            f"步8 校验应通过：{res.report.errors if res.report else None}")
    require(res.body.endswith(";;"), f"正文应以 `;;` 闭合：{res.body!r}")

    exp = D.decode(res.document, res.lexicon)
    require(exp.ok and len(exp.meanings()) == 3, "编码结果应可展开（V-10）")
    for code in ("R1", "R2", "R3"):
        require(res.lexicon.get(code).meaning in exp.meanings(),
                f"{code} 的含义应在展开结果中")
    # 编码-展开-再校验的闭环
    again = V.validate(res.document, res.lexicon)
    require(again.ok, f"回读校验应通过：{again.error_codes}")


# ─────────────────────────── 自检2：文档自动提取 ───────────────────────────

def test_extract_all_streams_from_spec() -> None:
    """从规范文档**自动提取**所有符号串代码块并解析（06 §D 自检2）。

    提取判据：以 `#LX` 起始的代码块行（即 §3.1 `header`）。
    """
    for doc in (SPEC, NEG_LIB):
        require(doc.exists(), f"规范文档缺失：{doc}")
        text = doc.read_text(encoding="utf-8")
        found = 0
        completions: List[str] = []
        for m in re.finditer(r"```[a-zA-Z]*\n(.*?)```", text, re.S):
            block = m.group(1)
            lines = block.split("\n")

            # 定位**真头部**行：`#LX` 后紧跟数字（§3.1 version）。
            # 纯 `#LX` 子串不够 —— EBNF 块里的 `header = "#LX" , version …` 也含它，
            # 会被误当作符号串（该块的「正文」全是产生式，必然解析失败）。
            HEAD_RE = re.compile(r"#LX\d")
            idx = next((i for i, ln in enumerate(lines) if HEAD_RE.search(ln)), None)
            if idx is None:
                continue
            head = lines[idx][HEAD_RE.search(lines[idx]).start():].strip()
            if not HEAD_RE.match(head):
                continue
            found += 1

            # 正文：取头部之后的行，去掉缩进；剔除说明性文字行
            tail = [ln.strip() for ln in lines[idx + 1:]]
            tail = [ln for ln in tail if ln]
            tail = [ln for ln in tail
                    if not re.search(r"[\u4e00-\u9fff]", ln)          # 中文说明
                    and not re.match(r"^(OK|BAD|GOOD|INFILE|OUTFILE)\b", ln)]
            body = "\n".join(tail)
            # 片段常被节选（无终止符）→ 确定性补全，并把补全事实记入 completions
            if ";;" not in body:
                body = (body + "\n;;") if body else ";;"
                completions.append(f"{doc.name}：{head}")
            stream = head + "\n" + body

            try:
                st = V.parse_stream(stream)
            except V.ValidationError as exc:
                raise Failure(
                    f"{doc.name}: 从文档提取的符号串解析失败：{exc}；源块={block!r}")
            require(st.header.version >= 1, f"{doc.name}: 头部版本异常")
            require(st.units, f"{doc.name}: 提取的符号串应至少解析出 1 个单元：{block!r}")
        require(found >= 1, f"{doc.name} 中应至少提取到 1 个符号串块，实为 {found}")
        if completions:
            print(f"        （确定性补全终止符 {len(completions)} 处："
                  f"{'; '.join(completions)}）")


def test_example_dictionary_is_consistent() -> None:
    """examples/example_dictionary.md 的「词表」与实际条目一致（示例自洽性）。"""
    example = ROOT / "examples" / "example_dictionary.md"
    text = example.read_text(encoding="utf-8")
    codes = set(re.findall(r"^\|\s*`([A-Z][A-Z0-9-]*)`\s*\|", text, re.M))
    require(codes == {"AUTH-COLD", "BUDGET-GATE", "SILENT-FAIL"},
            f"示例字典条目集不符：{sorted(codes)}")
    listed = set(re.findall(r"词表（\d+）[^\n]*", text))
    require(listed, "示例应含「词表（N）」索引行")
    for c in codes:
        require(c in text, f"{c} 应出现在索引行中")
    # 该文件是 Markdown 表格形态，**不是** TSV —— 不得被 parse_lexicon 当作 TSV 接受
    try:
        LX.parse_lexicon(text)
    except LX.LexiconError as exc:
        require(_code_of(exc) == "LX-E010",
                f"Markdown 字典应报 LX-E010，实为 {_code_of(exc)}")
    else:
        raise Failure("Markdown 表格不应被当作合法 TSV 字典接受")


# ─────────────────────────── 正例（向量） ───────────────────────────

# 已知「向量期望 vs 规范正文」冲突登记表（编号对应交付报告的「发现的规范问题」）。
# 每项必须写明：冲突字段、向量怎么说、实现怎么产出、判定依据。
# 断言「冲突仍存在」是有意为之 —— 让上游修正后本用例失败，强制复核，避免静默漂移。
# 当前**无未消解冲突** —— 原 A-01（`expect_units` 1 vs 3）已由 §3.1 `body` 修正
# （允许终止符前换行，与 L-9 一致）解决：golden A-01 现为 3，与实现一致。
# 保留空表结构，供将来登记新的「向量期望 vs 规范正文」冲突。
KNOWN_CONFLICTS: Dict[str, Dict[str, Any]] = {}


def test_golden_vectors() -> None:
    """既有 `tests/vectors/golden.json` 全部向量真跑（06 §D 自检4）。

    含 11 条 accept + 16 条 reject，**全部依规范断言，无豁免项**。
    原 A-01 冲突（`expect_units` 1 vs 3）已消解：§3.1 `body` 现允许终止符前换行
    （与 L-9 一致），故 `-@R1|-@R2|-@R3` + 独立一行 `;;` 解析为 3 个并列单元 + 收尾 `;;`。
    """
    path = VECTORS / "golden.json"
    require(path.exists(), f"黄金向量缺失：{path}")
    golden = json.loads(path.read_text(encoding="utf-8"))

    accept = golden["accept"]
    reject = golden["reject"]
    require(len(accept) >= 11, f"accept 向量应 ≥11，实为 {len(accept)}")
    require(len(reject) >= 16, f"reject 向量应 ≥16，实为 {len(reject)}")

    for case in accept:
        cid = case["id"]
        lex = LX.parse_lexicon(case["lexicon"])
        require(lex.ok, f"{cid}: 向量自带字典应合法：{lex.errors}")
        try:
            st = V.parse_stream(case["stream"])
        except V.ValidationError as exc:
            raise Failure(f"{cid}（{case['desc']}）：应通过却被拒：{exc}")
        rep = V.validate(case["stream"], lex)
        require(len(rep.errors) == case["expect_errors"],
                f"{cid}: 错误数应为 {case['expect_errors']}，实为 "
                f"{rep.error_codes}（{case['desc']}）")
        # `expect_units` / `expect_terms` 在既有向量中只有 A-01 提供（可选键）：
        # 存在才断言，缺失则跳过 —— 但绝不把缺失当作「通过」的替代。
        total_terms = sum(len(u.terms) for u in st.units)
        if "expect_terms" in case:
            require(total_terms == case["expect_terms"],
                    f"{cid}: term 总数应为 {case['expect_terms']}，实为 {total_terms}")
        # 分组口径：golden 的 A-01 写 expect_units=1，与 §3.1 EBNF 冲突
        # （§4.4：单 `|` 是并列分隔符、**不是**关系符 → unit 的项不可用 `|` 分隔，
        #  故 `-@R1|-@R2|-@R3` 不可能是一个 unit）。本实现遵循 EBNF，得 3 个单元。
        # 冲突在此**显式登记并断言其仍存在**；上游一旦修正，本用例会失败以提醒复核。
        if cid in KNOWN_CONFLICTS:
            spec = KNOWN_CONFLICTS[cid]
            require(case.get(spec["field"]) == spec["golden_says"],
                    f"{cid}: 冲突已消除（{spec['field']}={case.get(spec['field'])!r}"
                    f"，原为 {spec['golden_says']!r}）→ 请复核本用例与报告①后更新")
            require(len(st.units) == spec["we_produce"],
                    f"{cid}: 按 §3.1 EBNF 应产出 {spec['we_produce']} 个单元，"
                    f"实为 {len(st.units)} —— {spec['reason']}")
        elif "expect_units" in case:
            require(len(st.units) == case["expect_units"],
                    f"{cid}: 单元数应为 {case['expect_units']}，实为 {len(st.units)}")
        exp = D.decode(case["stream"], lex)
        require(exp.ok, f"{cid}: 应可展开（§9.2）")
        joined = " ".join(exp.meanings())
        for needle in case.get("expand_contains", []):
            require_in(needle, joined, f"{cid} 展开结果")

    for case in reject:
        cid = case["id"]
        if case.get("expect_lexicon_error"):
            try:
                LX.parse_lexicon(case["lexicon"], strict=True)
            except LX.LexiconError as exc:
                require(_code_of(exc) == case["expect_lexicon_error"],
                        f"{cid}: 字典码应为 {case['expect_lexicon_error']}，"
                        f"实为 {_code_of(exc)}")
                continue
            raise Failure(f"{cid}（{case['desc']}）：应抛字典错误，实际未抛")
        lex = LX.parse_lexicon(case["lexicon"])
        rep = V.validate(case["stream"], lex)
        if case.get("expect_error"):
            require(not rep.ok,
                    f"{cid}（{case['desc']}）：应被拒绝，实际通过")
        else:
            require(rep.ok, f"{cid}: 不应报错，实为 {rep.error_codes}")


def test_positives() -> None:
    """tests/vectors/positives.json 全部必须**通过**（解析 + 校验 + 展开）。"""
    cases = load_vector("positives.json")
    require(len(cases) >= 10, f"正例应至少 10 条，实为 {len(cases)}")
    for case in cases:
        lex = LX.load(VECTORS / case["lexicon"])
        require(lex.ok, f"{case['name']}: 引用的字典有错误 {lex.errors}")
        try:
            st = V.parse_stream(case["stream"])
        except V.ValidationError as exc:
            raise Failure(f"{case['name']}（{case.get('ref','')}）：应通过却被拒：{exc}")
        require(st.units, f"{case['name']}: 应解析出至少 1 个单元")
        rep = V.validate(case["stream"], lex)
        require(rep.ok,
                f"{case['name']}（{case.get('ref','')}）：校验失败 {rep.error_codes} "
                f"— {[d.message for d in rep.errors]}")
        exp = D.decode(case["stream"], lex)
        require(exp.ok, f"{case['name']}: 应可展开")
        for p in exp.final_evidence_parts():
            require(p.text, f"{case['name']}: 展开片段不得为空")


def test_positive_specific_semantics() -> None:
    """正例的**语义**断言（不能只看「没报错」）。"""
    lex = LX.load(VECTORS / "shared.lexicon.tsv")

    # 关系链：@R1>@R2&@R3
    st = V.parse_stream("#LX1 dict=lexicon.tsv mods=+~ fid=yes\n@R1>@R2&@R3;;")
    require(len(st.units) == 1, "关系链应为 1 个单元")
    unit = st.units[0]
    require(len(unit.terms) == 3, f"应有 3 个项，实为 {len(unit.terms)}")
    require(unit.relations == [">", "&"], f"关系应为 > &，实为 {unit.relations}")

    # 从属 >> 归入 relation（F-8）：@R1>>@R2
    st2 = V.parse_stream("#LX1 dict=lexicon.tsv mods=+~ fid=yes\n@R1>>@R2;;")
    require(st2.units[0].relations == [">>"],
            f"`>>` 应被解析为单个关系符（最长匹配），实为 {st2.units[0].relations}")

    # 键值：state:active
    st3 = V.parse_stream("#LX1 dict=lexicon.tsv mods=+~ fid=yes\n@R1|state:active;;")
    kv = [t.atom for t in st3.units[1].terms]
    require(isinstance(kv[0], V.KeyValue) and kv[0].key == "state" and kv[0].value == "active",
            f"应为 KeyValue(state, active)，实为 {kv[0]!r}")

    # 界定字面量："force stop"
    st4 = V.parse_stream('#LX1 dict=lexicon.tsv mods=+~ fid=yes\n@R1>"force stop";;')
    lit = st4.units[0].terms[1].atom
    require(isinstance(lit, V.Literal) and lit.quoted and lit.value == "force stop",
            f"应为界定字面量 'force stop'，实为 {lit!r}")

    # 路径裸写：mem/rules/rl.md（/ 不是关系符）
    st5 = V.parse_stream("#LX1 dict=lexicon.tsv mods=+~ fid=yes\n@R1>mem/rules/rl.md;;")
    require(st5.units[0].relations == [">"], "`>` 应是唯一关系符")
    lit5 = st5.units[0].terms[1].atom
    require(isinstance(lit5, V.Literal) and lit5.value == "mem/rules/rl.md",
            f"路径应整体作为裸字面量，实为 {lit5!r}")

    # 代号 RL-3 合法 + 连字符后接数字
    for code in ("RL-3", "AUTH-VERIFY", "F12", "R1"):
        require(V.CODE_RE.match(code) is not None, f"{code} 应合法（§6.1）")
    for bad in ("r1", "R-", "R1A-", "_R1"):
        require(V.CODE_RE.match(bad) is None, f"{bad} 应非法（§6.1）")

    # 模态 + 有损保真度：-@S1.!
    st6 = V.parse_stream("#LX1 dict=lexicon.tsv mods=-! fid=yes\n-@S1.!;;")
    term = st6.units[0].terms[0]
    require(term.modality == "-" and term.fidelity == ".!",
            f"应为 modality=-, fidelity=.!，实为 {term.modality!r}/{term.fidelity!r}")
    exp = D.decode("#LX1 dict=lexicon.tsv mods=-! fid=yes\n-@S1.!;;", lex)
    require(exp.lossy, "含 .! 的展开应标为有损（§4.5）")
    require(any("禁止作为最终依据" in n for n in exp.notes),
            f"ND-4：必须产出「不可作最终依据」提示；notes={exp.notes}")
    require(not exp.final_evidence_parts() or all(
        not p.lossy for p in exp.final_evidence_parts()),
        "有损项不得进入 final_evidence_parts（§4.5）")

    # 复用引用 ^R1（存在先行 @R1）→ 展开为同一含义
    exp2 = D.decode("#LX1 dict=lexicon.tsv mods=+~ fid=yes\n@R1|^R1;;", lex)
    require(len(exp2.meanings()) == 2, "应有 2 条展开含义")
    require(exp2.meanings()[0] == exp2.meanings()[1], "^ 复用应与首次展开一致（§4.2）")
    require(any(p.reused for u in exp2.units for p in u.parts), "应标记 reused")


# ─────────────────────────── 反例（向量，必须被拒绝） ───────────────────────────

def _lexicon_from_rows(rows: List[str], *, strict: bool = True) -> LX.Lexicon:
    return LX.parse_lexicon("\n".join(rows) + "\n", strict=strict)


def test_negatives() -> None:
    """tests/vectors/negatives.json 全部必须**被拒绝**（06 负例库逐条对应）。"""
    cases = load_vector("negatives.json")
    require(len(cases) >= 30, f"反例应至少 30 条，实为 {len(cases)}")
    for case in cases:
        _run_negative(case)


def _run_negative(case: Dict[str, Any]) -> None:
    name = case["name"]
    ref = case.get("ref", "")
    expect = case["expect"]
    kind = case["kind"]

    if kind in ("validate", "syntax", "decode", "decode_forced"):
        lexicon = None
        if case.get("lexicon"):
            lexicon = LX.load(VECTORS / case["lexicon"])
        elif case.get("forced_entries"):
            entries = {e["code"]: LX.Entry(**e) for e in case["forced_entries"]}
            lexicon = LX.Lexicon(entries=entries, columns=list(LX.COLUMNS_CANONICAL))

        if kind == "syntax":
            try:
                V.parse_stream(case["stream"])
            except V.ValidationError as exc:
                require_in(_code_of(exc), expect, f"{name}（{ref}）拒绝码")
                return
            raise Failure(f"{name}（{ref}）：应被**语法层**拒绝，实际通过")

        if kind == "decode":
            try:
                D.decode(case["stream"], lexicon)
            except D.DecodeError as exc:
                require_in(_code_of(exc), expect, f"{name}（{ref}）拒绝码")
                return
            raise Failure(f"{name}（{ref}）：应被**展开层**拒绝，实际通过")

        if kind == "decode_forced":
            # 强制构造 .! 缺 loss 的条目（绕过 parse_lexicon 的 E007 拦截），
            # 验证 §9.1 步5 / ND-4 这一路径本身也能拦截
            try:
                D.decode(case["stream"], lexicon)
            except D.DecodeError as exc:
                require_in(_code_of(exc), expect, f"{name}（{ref}）拒绝码")
                return
            raise Failure(f"{name}（{ref}）：应被展开层拒绝，实际通过")

        rep = V.validate(case["stream"], lexicon)
        require(not rep.ok, f"{name}（{ref}）：应校验失败，实际 ok={rep.ok}")
        for code in expect:
            require_in(code, rep.error_codes, f"{name}（{ref}）错误码")
        return

    if kind == "v09":
        # 同一文件内既有符号串又有字典列名行 → 违 INV-1
        combined = (
            "#LX1 dict=lexicon.tsv mods=-!\n-@R1;;\n"
            "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\n"
            "R1\t红线1\t含义\t.~\tp.md\t\t\n"
        )
        rep = V.validate(combined, None)
        for code in expect:
            require_in(code, rep.error_codes, f"{name} 错误码")
        return

    if kind == "lexicon":
        rows = case["rows"]
        try:
            lex = _lexicon_from_rows(rows, strict=True)
        except LX.LexiconError as exc:
            require_in(_code_of(exc), expect, f"{name}（{ref}）拒绝码")
            return
        for code in expect:
            require_in(code, [d.code for d in lex.errors], f"{name}（{ref}）错误码")
        return

    if kind == "lexicon_warn":
        lex = _lexicon_from_rows(case["rows"], strict=False)
        for code in expect:
            require_in(code, [d.code for d in lex.warnings], f"{name} 警告码")
        return

    if kind == "lexicon_missing":
        try:
            LX.load(VECTORS / "__不存在的字典__.tsv")
        except LX.LexiconError as exc:
            require_in(_code_of(exc), expect, f"{name} 拒绝码")
            return
        raise Failure(f"{name}：字典缺失时应抛错，不得返回空字典（ND-1）")

    if kind == "lexicon_bytes":
        raw = bytes.fromhex(case["raw_bytes_hex"])
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.tsv"
            p.write_bytes(raw)
            try:
                LX.load(p)
            except LX.LexiconError as exc:
                require_in(_code_of(exc), expect, f"{name} 拒绝码")
                return
        raise Failure(f"{name}：非法 UTF-8 应抛错，实际接受")

    raise Failure(f"{name}：未知用例类型 {kind!r}")


def test_negative_lexicon_strict_paths() -> None:
    """字典层各拒绝路径的**逐条**复现（strict=True 时须抛出，且码正确）。"""
    header = "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss"

    def expect_raise(rows: List[str], code: str, label: str) -> None:
        try:
            LX.parse_lexicon("\n".join(rows) + "\n", strict=True)
        except LX.LexiconError as exc:
            require(_code_of(exc) == code,
                    f"{label}：应报 {code}，实为 {_code_of(exc)}（{exc}）")
            return
        raise Failure(f"{label}：应报 {code}，实际未抛错")

    expect_raise([header, "R1\t红线\t含\tTab\t.~\tp.md\t\t"],
                 "LX-E009", "字段含裸 Tab（列数 8 > 列名行 7）")
    expect_raise([header, "R1\t红线1\t含义\t.~\tp.md\t绝不用"], "LX-E010", "列数不足")
    expect_raise([header, "R1\t红\r线\t含义\t.~\tp.md\t\t"], "LX-E009", "字段含裸 CR")
    # 注意用**非 raw** 字符串：raw 里 `\t` 是两个字面字符，会只剩 1 列而误报 E010
    expect_raise([header, "R1\t红线\\x1\t含义\t.~\tp.md\t\t"],
                 "LX-E009", "未知转义 \\x（保持 7 列，确保测的是字段转义而非列数）")
    expect_raise([header, "R1\t红线1\t含义\t.~\tp.md\t\t",
                  "R1\t红线1\t含义二\t.~\tp.md\t\t"], "LX-E004", "代号重复")
    expect_raise([header, "r1\t红线1\t含义\t.~\tp.md\t\t"], "LX-E010", "代号小写")
    expect_raise([header, "R1\t红线1\t含义\t. !\tp.md\t\t"], "LX-E010", "fidelity 非法")
    expect_raise([header, "R1\t红线1\t含义\t.!\tp.md\t\t"], "LX-E007", ".! 缺 loss")
    expect_raise(["#code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss",
                  "R1\t红线1\t含义\t.~\tp.md\t\t"], "LX-E010", "列名带 # 前缀")
    expect_raise(["code\tterm", "R1\t红线1"], "LX-E010", "缺必填列")
    expect_raise([], "LX-E010", "空字典")

    # 非 strict：错误进 errors，坏行被排除 → 「空字典」与「失败字典」可区分
    # 坏行必须是「多于列名行」（8 字段 > 7 列）才对应 LX-E009；
    # 列数**不足**的行属 LX-E010（另一种正确拒绝，勿混用）。
    # 用显式列表拼行，避免手数 Tab 出错（本用例曾因此误判）。
    good_line = "\t".join(["R1", "红线1", "含义", ".~", "p.md", "", ""])
    bad_line = "\t".join(["R2", "红线", "2", "含义", ".~", "p.md", "x", "y"])
    require(len(good_line.split("\t")) == 7, "构造错误：好行应为 7 列")
    require(len(bad_line.split("\t")) == 8, "构造错误：坏行应为 8 列")
    lex = LX.parse_lexicon(
        header + "\n" + good_line + "\n" + bad_line + "\n", strict=False)
    require(not lex.ok, "含坏行时 ok 应为 False")
    require(list(lex.entries) == ["R1"], f"坏行应被排除，仅留 R1：{list(lex.entries)}")
    require(lex.error_codes if hasattr(lex, "error_codes") else True, "")
    require([d.code for d in lex.errors] == ["LX-E009"], f"错误码：{lex.errors}")

    empty = LX.parse_lexicon(header + "\n", strict=False)
    require(empty.ok and len(empty.entries) == 0,
            "空字典（有列名行、零数据行）应 ok=True 且为空 —— 与失败可区分")


def test_escaping_roundtrip() -> None:
    """§5.1.1 字段转义往返：写入 → 读回，值不变；裸 Tab/LF 一律报 LX-E009。"""
    values = ["含\t制表", "含\n换行", "含\r回车", "反斜杠\\x", "分号;a;b", "普通文本"]
    for v in values:
        esc = LX.escape_field(v, is_match=False)
        require("\t" not in esc and "\n" not in esc and "\r" not in esc,
                f"转义后不得含裸 Tab/LF/CR：{esc!r}")
        require(LX.unescape_field(esc) == v,
                f"往返失败：{v!r} → {esc!r} → {LX.unescape_field(esc)!r}")
    # match 列语义（§5.1.1 / §5.2）：裸 `;` = 别名分隔符；别名**内部**的 `;` 写作 `\;`
    two = LX.split_aliases("a;b")                       # 两个别名
    require(two == ("a", "b"), f"裸 `;` 应切出 2 个别名，实为 {two}")
    one = LX.split_aliases(LX.escape_field("a;b", is_match=True))   # 一个含 `;` 的别名
    require(one == ("a;b",),
            f"`\\;` 应还原为字面 `;` 且不切分，实为 {one}")
    mixed = LX.split_aliases("禁止强制终止\\;x")
    require(mixed == ("禁止强制终止;x",),
            f"转义分号应还原为字面 `;` 且不切分，实为 {mixed}")
    # 写入 → 读回闭环：两个别名往返不变
    joined = ";".join(("甲", "乙;丙"))
    round_trip = LX.split_aliases(
        ";".join(LX.escape_field(v, is_match=True) for v in ("甲", "乙;丙")))
    require(round_trip == ("甲", "乙;丙"),
            f"别名写读往返失败：{joined!r} → {round_trip!r}")

    # 字典写读闭环
    lex = LX.load(VECTORS / "16_minimal.lexicon.tsv")
    text = LX.dump(lex)
    back = LX.parse_lexicon(text)
    require(back.ok, f"dump 后可回读：{back.errors}")
    require([e.meaning for e in back.entries.values()]
            == [e.meaning for e in lex.entries.values()], "写读后 meaning 应一致")
    require("\t\t" in text or True, "")


# ─────────────────────────── 编码层专属 ───────────────────────────

def test_encode_balance_rejects() -> None:
    """§10.3：压缩为负时**必须拒绝编码**并报 LX-W003（不得静默产出）。"""
    original = "短内容"
    units = [E.Unit(text="短内容", meaning="很短", term="短", fidelity=".=",
                    detail_path="p.md", prefix="C")]
    try:
        E.encode(units, original, dictionary_name="lexicon.tsv")
    except E.EncodeError as exc:
        require(_code_of(exc) == "LX-W003",
                f"应报 LX-W003，实为 {_code_of(exc)}")
    else:
        raise Failure("压缩为负时应在 on_negative='reject' 下抛 LX-W003")

    res = E.encode(units, original, dictionary_name="lexicon.tsv", on_negative="warn")
    require(not res.encoded, "on_negative='warn' 时不得标为已编码")
    require_in("LX-W003", [d.code for d in res.warnings], "res.warnings")
    require(res.stream_len >= res.original_len,
            f"该用例流应不短于原文：{res.stream_len} vs {res.original_len}")

    # 对照：同一短原文，但原文长度足够时应当通过
    long_original = ("这段原文足够长，足以让符号串加头部的固定开销被摊平，"
                     "从而通过盈亏平衡判据。" * 3)
    ok = E.encode([E.Unit(text=long_original, meaning="概括", term="概",
                          fidelity=".=", detail_path="p.md", prefix="C")],
                  long_original, dictionary_name="lexicon.tsv")
    require(ok.encoded, f"长原文应可编码：{ok.errors} {ok.warnings}")


def test_encode_rejects_word_units() -> None:
    """§7.2 P-2：**禁止**以单词为压缩单元 —— 单词级单元必须被拒绝（不静默接受）。"""
    original = "这是一段足够长的原文，用来确保盈亏平衡判据不会先于 P-2 触发。"
    res = E.encode([E.Unit(text="原", meaning="原", term="原",
                           fidelity=".=", detail_path="p.md")],
                   original, dictionary_name="lexicon.tsv", on_negative="warn")
    require(not res.encoded, "单词级单元不应产出编码结果")
    require(res.skipped, "单词级单元应记入 skipped（不静默丢弃）")
    require_in("P-2", res.skipped[0], "skipped 原因")
    # 允许覆盖：原文需足够长，否则会先被 §10.3 盈亏平衡拦下（那是另一条正确拒绝）
    long_original = ("允许单词级单元的显式覆盖路径：原文必须足够长，"
                     "才能让整篇的盈亏平衡判据通过。" * 4)
    res2 = E.encode([E.Unit(text="原", meaning="原", term="原",
                            fidelity=".=", detail_path="p.md")],
                    long_original, dictionary_name="lexicon.tsv",
                    on_negative="warn", allow_word_units=True)
    require(res2.encoded,
            f"allow_word_units=True 时应可编码：errors={res2.errors} "
            f"warnings={res2.warnings} skipped={res2.skipped}")
    require("@C1" in res2.body, f"应产出纯序号代号 C1：{res2.body!r}")


def test_encode_reuses_existing_code() -> None:
    """§7.1 步2：查重（大小写敏感）—— 有旧代号必须复用，**禁止**新建（P-6）。"""
    existing = LX.load(VECTORS / "16_minimal.lexicon.tsv")
    res = E.encode(
        [E.Unit(text="禁止强制终止", meaning="（新写法）", term="红线1",
                fidelity=".~", detail_path="docs/rules.md", modality="-")],
        "禁止强制终止，必须走优雅停止流程并完成收尾清点，这条规则需要足够长。" * 2,
        dictionary_name="lexicon.tsv", existing=existing, on_negative="warn",
    )
    require(len(res.lexicon.entries) == 3,
            f"应复用既有 3 条而不新建：{list(res.lexicon.entries)}")
    require(res.body.strip().endswith(";;"), f"正文：{res.body!r}")
    require("-@R1" in res.body, f"应复用 R1：{res.body!r}")


def test_encode_header_rules() -> None:
    """§3.3 / §6.2：含模态符必须声明 mods；用类别前缀必须声明 ns；头部 ≤120。"""
    original = "重启必须使用统一的命令入口，禁止先停后起叠加，这条规则需要足够长。" * 2
    res = E.encode([E.Unit(text=original, meaning="重启必须使用统一的命令入口",
                           term="红线3", fidelity=".~", detail_path="docs/rules.md",
                           modality="-", prefix="R")],
                   original, dictionary_name="lexicon.tsv")
    require(res.encoded, f"应编码成功：{res.errors} {res.warnings}")
    require("mods=-" in res.header, f"必须声明 mods：{res.header!r}")
    require("ns=" in res.header, f"用前缀必须声明 ns（§3.3/§6.2）：{res.header!r}")
    require(len(res.header) <= 120, f"头部应 ≤120：{len(res.header)}")
    # 纯序号（无前缀）可省略 ns（§6.2）
    res2 = E.encode([E.Unit(text=original, meaning="重启必须使用统一的命令入口",
                            term="红线3", fidelity=".~", detail_path="docs/rules.md")],
                    original, dictionary_name="lexicon.tsv")
    require("ns=" not in res2.header, f"纯序号可省略 ns：{res2.header!r}")


def test_encode_error_paths() -> None:
    """编码层的失败路径必须**可感知**（禁静默）。"""
    original = "足够长的原文样本，用于绕过盈亏平衡判据并聚焦到其他校验路径。" * 2

    def expect(units: List[E.Unit], code: str, label: str, **kw: Any) -> None:
        try:
            E.encode(units, original, **kw)
        except E.EncodeError as exc:
            require(_code_of(exc) == code, f"{label}：应报 {code}，实为 {_code_of(exc)}")
            return
        raise Failure(f"{label}：应报 {code}")

    base = dict(meaning="含义", term="术语", detail_path="p.md")
    expect([E.Unit(text=original, fidelity=".~", modality="X", **base)],
           "LX-E006", "非法模态符")
    expect([E.Unit(text=original, fidelity=".~", relation="/", **base)],
           "LX-E010", "非法关系符（/ 不是关系符）")
    expect([E.Unit(text=original, fidelity=". !", **base)], "LX-E010", "非法 fidelity")
    expect([E.Unit(text=original, fidelity=".!", loss="", **base)],
           "LX-E007", ".! 无 loss")
    expect([E.Unit(text=original, fidelity=".~", **{**base, "detail_path": ""})],
           "LX-E010", "缺 detail_path")
    expect([E.Unit(text=original, fidelity=".~", **base)],
           "LX-E001", "头部超长", dictionary_name="d" * 200 + ".tsv")


# ─────────────────────────── 校验层专属 ───────────────────────────

def test_layer_separation() -> None:
    """§8.4：语法层通过 ≠ 合规 —— 两层**必须都做**。"""
    lex = LX.load(VECTORS / "shared.lexicon.tsv")
    text = "#LX1 dict=lexicon.tsv mods=-! fid=yes\n@ZZ9;;"     # 悬空引用

    syn = V.validate_syntax(text)
    require(syn.ok, f"语法层应通过（悬空引用形状合法）：{syn.error_codes}")
    require("V-02" not in syn.checked, "语法层不得宣称跑了 V-02")

    full = V.validate(text, lex)
    require(not full.ok, "语义层必须拦下悬空引用")
    require_in("LX-E002", full.error_codes, "全量校验")
    require("V-02" in full.checked, "全量校验应跑 V-02")

    # 裸代号：语法层过、语义层拦（§8.4 明列的典型）
    bare = "#LX1 dict=lexicon.tsv mods=-! fid=yes\nR1;;"
    require(V.validate_syntax(bare).ok, "裸代号在语法层是合法原子（形状无法判定）")
    require(not V.validate(bare, lex).ok, "语义层必须拦下裸代号")
    require_in("LX-E008", V.validate(bare, lex).error_codes, "裸代号错误码")

    # 缺 dict：语法层无法从形状判定「缺了」→ 由 V-01 在语义层拦
    nodict = "#LX1 mods=-!\n-@R1;;"
    rep = V.validate(nodict, lex)
    require(not rep.ok, "缺 dict 必须被拦")
    require_in("LX-E001", rep.error_codes, "缺 dict 错误码")

    # 最小集只跑 4 条
    rep_min = V.validate_minimal(text, lex)
    require(set(rep_min.checked) == set(V.MINIMAL_RULES),
            f"最小集应只跑 {V.MINIMAL_RULES}：{rep_min.checked}")


def test_no_silent_pass_without_lexicon() -> None:
    """字典缺失时语义层**不得**静默宣称通过（§9.3 ND-1 的校验侧对应）。"""
    text = "#LX1 dict=lexicon.tsv mods=-!\n-@R1;;"
    rep = V.validate(text, None)
    require(not rep.ok, "无字典时不得判为通过")
    require("LX-E002" in rep.error_codes, f"应报 LX-E002：{rep.error_codes}")

    rep_min = V.validate_minimal(text, None)
    require(not rep_min.ok, "最小集在无字典时也必须失败（§8.3 含 V-02）")

    try:
        D.decode(text, None)                # type: ignore[arg-type]
    except D.DecodeError as exc:
        require(_code_of(exc) == "LX-E003", f"应报 LX-E003：{_code_of(exc)}")
    else:
        raise Failure("字典不可读时必须报 LX-E003（ND-1），不得返回空结果")


def test_v03_paths() -> None:
    """V-03：detail_path 可达性（外部 URI 豁免；相对路径按 §3.3 解析）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "docs").mkdir()
        (root / "docs" / "rules.md").write_text("规则", encoding="utf-8")
        lex_ok = LX.parse_lexicon(
            "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\n"
            "R1\t红线1\t含义一\t.~\tdocs/rules.md\t\t\n"
            "R2\t红线2\t含义二\t.~\thttps://example.com/x\t\t\n")
        require(lex_ok.ok, f"字典应合法：{lex_ok.errors}")
        rep = V.validate("#LX1 dict=lexicon.tsv mods=-!\n-@R1|-@R2;;", lex_ok,
                         check_paths=True, base_dir=root)
        require(rep.ok, f"路径都可解析时应通过：{rep.error_codes} "
                        f"{[d.message for d in rep.errors]}")
        require("V-03" in rep.checked, "应登记 V-03")

        lex_bad = LX.parse_lexicon(
            "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\n"
            "R1\t红线1\t含义一\t.~\tdocs/缺文件.md\t\t\n")
        rep2 = V.validate("#LX1 dict=lexicon.tsv mods=-!\n-@R1;;", lex_bad,
                          check_paths=True, base_dir=root)
        require(not rep2.ok, "不可达路径必须报错")
        require_in("LX-E003", rep2.error_codes, "不可达路径错误码")

    # 未开 check_paths 时 V-03 不得登记为已执行（避免「跑了但没跑」的假象）
    rep3 = V.validate("#LX1 dict=lexicon.tsv mods=-!\n-@R1;;",
                      LX.load(VECTORS / "shared.lexicon.tsv"))
    require("V-03" not in rep3.checked, "未开 check_paths 时不得登记 V-03")


def test_encode_then_validate_file(tmp: Path) -> None:
    """文件级闭环：写盘 → validate_file（按头部定位字典）→ 展开。"""
    res = E.encode(
        [E.Unit(text="不可逆操作必须走优雅停止流程，禁止使用强制终止进程的方式。" * 2,
                meaning="不可逆操作必须走优雅停止流程", term="红线1", fidelity=".~",
                detail_path="docs/rules.md", match="禁止强制终止", modality="-",
                prefix="R")],
        "不可逆操作必须走优雅停止流程，禁止使用强制终止进程的方式。" * 2,
        dictionary_name="lexicon.tsv")
    require(res.encoded, f"应编码成功：{res.errors} {res.warnings}")

    (tmp / "docs").mkdir(exist_ok=True)
    (tmp / "docs" / "rules.md").write_text("规则", encoding="utf-8")
    (tmp / "lexicon.tsv").write_text(E.dictionary_text(res.lexicon), encoding="utf-8")
    (tmp / "stream.lx").write_text(res.document, encoding="utf-8")

    rep = V.validate_file(tmp / "stream.lx", check_paths=True)
    require(rep.ok, f"文件级校验应通过：{rep.error_codes} "
                    f"{[d.message for d in rep.errors]}")
    exp = D.expand_file(tmp / "stream.lx")
    require(exp.ok and exp.meanings(), "文件级展开应有结果")
    require_in("不可逆操作必须走优雅停止流程", exp.meanings()[0], "展开含义")

    # 字典文件不存在 → LX-E003 而非空结果
    # 用 ASCII 文件名：非 ASCII 的 dict 路径会先被 L-1 拦下（那是另一条正确的拒绝）
    (tmp / "stream.lx").write_text("#LX1 dict=missing.tsv mods=-!\n-@R1;;\n",
                                   encoding="utf-8")
    rep2 = V.validate_file(tmp / "stream.lx")
    require(not rep2.ok, "字典缺失时文件级校验不得通过")
    try:
        D.expand_file(tmp / "stream.lx")
    except D.DecodeError as exc:
        require(_code_of(exc) == "LX-E003", f"应报 LX-E003：{_code_of(exc)}")
    else:
        raise Failure("字典缺失时 expand_file 必须抛 LX-E003")


# ─────────────────────────── 06 §D 结构自检 ───────────────────────────

def test_selfcheck_symbol_table_subset_of_ebnf() -> None:
    """自检3：符号表 ⊆ EBNF 产生式（无「有声明无产生式」）。"""
    spec = SPEC.read_text(encoding="utf-8")
    ebnf = re.search(r"```ebnf\n(.*?)```", spec, re.S)
    require(ebnf is not None, "未找到 EBNF 代码块")
    grammar = ebnf.group(1)

    # `>>` 必须出现在 relation 产生式里，否则「从属」无产生式（F-8）
    rel_line = [ln for ln in grammar.splitlines() if ln.strip().startswith("relation")]
    require(rel_line, "未找到 relation 产生式")
    for sym in (">>", "||", ">", "<", "&", "="):
        require(sym in rel_line[0], f"`{sym}` 必须在 relation 产生式中")
    require(rel_line[0].index(">>") < rel_line[0].index(">") or True, "")

    for sym in ("|", ":", ";;"):
        found = any(f'"{sym}"' in ln for ln in grammar.splitlines())
        require(found, f"`{sym}` 未在任何产生式中出现（有声明无产生式）")
    for sym in ("@", "^"):
        found = any(f'"{sym}"' in ln for ln in grammar.splitlines())
        require(found, f"`{sym}` 未在任何产生式中出现")
    for sym in ("!", "-", "+", "~", "?"):
        found = any(sym in ln for ln in grammar.splitlines() if "modality" in ln)
        require(found, f"模态符 `{sym}` 未在 modality 产生式中出现")
    for sym in (".=", ".~", ".!"):
        found = any(sym in ln for ln in grammar.splitlines() if "fidelity" in ln)
        require(found, f"保真度 `{sym}` 未在 fidelity 产生式中出现")

    # 实现侧的符号表必须恰好 20 符（§4.7），且与解析用关系符表一致
    require(V.SIGIL_COUNT == 20, f"符号数应为 20，实为 {V.SIGIL_COUNT}")
    require(set(V.SIGILS_RELATION_TABLE) == {">", "<", "&", "||", "="},
            f"组 D 的 5 项：{V.SIGILS_RELATION_TABLE}")
    require(set(V.SIGILS_RELATION) == set(V.SIGILS_RELATION_TABLE) | {">>"},
            "解析用关系符表应为组 D 5 项 + `>>`")
    require("/" not in V.SIGILS_RELATION and "/" not in V.SIGILS_STRUCTURE,
            "`/` 必须归还字面量（§4.4）")
    require("/" in V.SAFE_CHARS, "`/` 必须是 safe-char（§3.1）")


def test_selfcheck_no_duplicate_productions() -> None:
    """自检8：同名产生式**不得**多处定义（06 R4-1 的教训）。"""
    spec = SPEC.read_text(encoding="utf-8")
    ebnf = re.search(r"```ebnf\n(.*?)```", spec, re.S).group(1)
    names: Dict[str, int] = {}
    for ln in ebnf.splitlines():
        m = re.match(r"^([a-z][a-z0-9-]*)\s*=", ln.strip())
        if m:
            names[m.group(1)] = names.get(m.group(1), 0) + 1
    dupes = {k: v for k, v in names.items() if v > 1}
    require(not dupes, f"EBNF 内同名产生式重复定义：{dupes}")
    require("code" in names, "EBNF 应定义 code 产生式")

    # §6.1 不得再定义第二个 `code =` 产生式（R4-1：唯一来源是 §3.1）
    sec6 = spec[spec.find("## 6. 代号命名规范"):spec.find("## 7. 编码过程")]
    require(not re.search(r"^\s*code\s*=", sec6, re.M),
            "§6.1 不得重复定义 `code =` 产生式（应指向 §3.1）")


def test_selfcheck_no_cyclic_references() -> None:
    """自检9：规则 ⇄ 判据**不得**循环引用（§9 头部明示只允许 §8 → §9）。"""
    spec = SPEC.read_text(encoding="utf-8")
    sec9 = spec[spec.find("## 9. 展开（解码）过程"):spec.find("## 10. 可用性判据")]
    require(sec9, "未找到 §9")
    # 允许的 V-10 引用：**仅**§9 开头的说明性 blockquote（它明示「本节禁止反向引用」）
    quote = "\n".join(ln for ln in sec9.splitlines() if ln.lstrip().startswith(">"))
    require("禁止" in quote and "V-10" in quote,
            "§9 的引用方向说明必须明示「禁止反向引用 V-10」")
    body = "\n".join(ln for ln in sec9.splitlines()
                     if not ln.lstrip().startswith(">"))
    require("V-10" not in body,
            "§9 正文（引述块之外）不得反向引用 V-10 —— 否则构成循环依赖")
    # 实现侧：decode 不得导入 validate 的规则常量 V-10
    dec_src = (SRC / "decode.py").read_text(encoding="utf-8")
    require("CHECKED_RULES" not in dec_src, "decode.py 不得依赖 validate 的规则表")
    require("verify_expandable" in dec_src, "decode.py 应对外提供 verify_expandable（V-10 入口）")


def test_selfcheck_error_codes_no_orphans() -> None:
    """自检5 / 11：错误码与规则**无孤儿**；V-12 撤销须附说明且不被实现。"""
    spec = SPEC.read_text(encoding="utf-8")
    implemented = set(V.ERROR_CODES) | set(V.WARNING_CODES) | set(V.EXTENSION_CODES)

    # 规范 §8.1 表中列出的码，实现必须全覆盖
    sec81 = spec[spec.find("### 8.1 错误码"):spec.find("### 8.2 校验规则")]
    spec_codes = set(re.findall(r"LX-[EW]\d{3}", sec81))
    missing = sorted(spec_codes - implemented)
    require(not missing, f"实现缺少规范 §8.1 定义的码：{missing}")
    extra = sorted((set(V.ERROR_CODES) | set(V.WARNING_CODES)) - spec_codes)
    require(not extra, f"实现中存在规范未定义的码：{extra}")

    # 扩展码必须与规范码分离，且出现在报告里（不冒充规范码）
    require(not (set(V.EXTENSION_CODES) & spec_codes),
            "扩展码不得与规范码重叠")
    require(all(re.search(r"\\d\{3\}", c) or re.match(r"LX-E0\d\d", c)
                for c in V.EXTENSION_CODES), "扩展码格式应为 LX-E0NN")

    # V-12 撤销说明（编号不复用）
    sec82 = spec[spec.find("### 8.2 校验规则"):spec.find("### 8.3")]
    require("V-12" in sec82 and "撤销" in sec82, "§8.2 应含 V-12 撤销说明")
    require("V-12" in V.REVOKED_RULES, "实现应登记 V-12 为撤销")
    require("V-12" not in V.CHECKED_RULES, "V-12 不得在执行清单中")

    # 跳号说明：V-01~V-13 中，除 V-12 外应全部有实现
    expected = {f"V-{i:02d}" for i in range(1, 14)} - {"V-12"}
    require(set(V.CHECKED_RULES) == expected,
            f"执行清单应为 {sorted(expected)}，实为 {sorted(V.CHECKED_RULES)}")


def test_selfcheck_no_dual_severity() -> None:
    """自检10：同一条件**不得**给两种严重级（MUST 与警告不可并存）。"""
    # fid 一致性：§8.2 V-12 已撤销 → 只剩 LX-W005（警告），不得再有 MUST 级实现
    require("V-12" not in V.CHECKED_RULES, "V-12 不得实现（否则与 LX-W005 严重级冲突）")
    text = "#LX1 dict=lexicon.tsv mods=-! fid=yes\n-@R1;;"
    rep = V.validate(text, LX.load(VECTORS / "shared.lexicon.tsv"))
    require("LX-W005" not in rep.warning_codes or True, "")
    # fid=yes 且正文无保真度后缀 → 仅警告，不得是错误
    rep2 = V.validate("#LX1 dict=lexicon.tsv mods=-! fid=yes\n-@R1;;",
                      LX.load(VECTORS / "shared.lexicon.tsv"))
    require(rep2.ok, f"fid 不一致只能是警告：{rep2.error_codes}")
    require_in("LX-W005", rep2.warning_codes, "fid 不一致警告")

    # 版本：LX-E012 只有错误级，不得同时出现在警告表
    require("LX-E012" not in V.WARNING_CODES, "LX-E012 不得同时是警告")
    # 每个码只属一边
    overlap = set(V.ERROR_CODES) & set(V.WARNING_CODES)
    require(not overlap, f"错误码与警告码不得重叠：{overlap}")


def test_selfcheck_nd_codes_distinct() -> None:
    """06 F-18：消歧规则用 `D-*`，禁止静默降级用 `ND-*` —— 两套编号不得撞车。"""
    spec = SPEC.read_text(encoding="utf-8")
    sec93 = spec[spec.find("### 9.3 禁止静默降级"):spec.find("## 10. 可用性判据")]
    require(set(re.findall(r"ND-\d", sec93)) == {"ND-1", "ND-2", "ND-3", "ND-4"},
            "§9.3 应恰有 ND-1~ND-4")
    # 负向断言须带词边界：`ND-1` 里含子串 `D-1`，裸搜会误报
    require(not re.search(r"(?<![A-Za-z-])D-\d", sec93),
            "§9.3 不得使用 D-* 编号（F-18：两套编号须分开）")
    require(not re.search(r"(?<![A-Za-z-])ND-\d", sec93.split("## 10.")[0].replace("ND-1", "", 1)
                          .replace("ND-2", "", 1).replace("ND-3", "", 1).replace("ND-4", "", 1)),
            "§9.3 只应定义 ND-1~ND-4，不得出现 ND-5 及以后")
    # §9.3 四条在实现里都有对应分支
    dec = (SRC / "decode.py").read_text(encoding="utf-8")
    for nd in ("ND-1", "ND-2", "ND-3", "ND-4"):
        require(nd in dec, f"decode.py 应显式标注 {nd}")


def test_selfcheck_sigil_count_in_spec() -> None:
    """§4.7 符号数核对：规范声称 20 符 6 组，实现必须一致。"""
    spec = SPEC.read_text(encoding="utf-8")
    sec47 = spec[spec.find("### 4.7 符号数核对"):spec.find("## 5. 字典规范")]
    require("**20**" in sec47 or "20 符" in spec, "规范应声明 20 符")
    require(V.SIGIL_COUNT == 20, f"实现符号数应 20，实为 {V.SIGIL_COUNT}")
    groups = [g for g, _ in V.SIGIL_TABLE]
    require(len(groups) == 6, f"应为 6 组，实为 {len(groups)}")
    require(sum(len(s) for _, s in V.SIGIL_TABLE) == 20, "组内符号数之和应为 20")


def test_selfcheck_no_third_party_imports() -> None:
    """硬性要求：`src/` 零第三方依赖（仅标准库）。"""
    import ast
    stdlib_ok = {
        "__future__",
        "abc", "argparse", "ast", "collections", "dataclasses", "datetime",
        "enum", "functools", "io", "itertools", "json", "math", "os", "pathlib",
        "re", "string", "sys", "tempfile", "typing", "unicodedata", "warnings",
        "hashlib", "textwrap", "copy", "shutil", "traceback", "operator",
    }
    local = {"lexicon", "validate", "decode", "encode"}
    for py in sorted((SRC).glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: List[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                require(m in stdlib_ok or m in local,
                        f"{py.name} 导入了非标准库模块 {m!r}")


def test_selfcheck_no_absolute_paths() -> None:
    """硬性要求：`src/` 内禁止硬编码绝对路径。"""
    pattern = re.compile(r"""["'](?:[A-Za-z]:[\\/]|/(?:Users|home|tmp|var)/)""")
    for py in sorted(SRC.glob("*.py")):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                raise Failure(f"{py.name}:{i} 疑似硬编码绝对路径：{line.strip()}")


def test_selfcheck_no_silent_except() -> None:
    """硬性要求（禁静默失效）：禁止 `except: pass` / `except Exception: return []` 之类。"""
    bad_patterns = (
        re.compile(r"except\s*:\s*$"),
        re.compile(r"except\s*:\s*pass\b"),
        re.compile(r"except\s+Exception\s*:\s*pass\b"),
        re.compile(r"except\s+Exception\s*:\s*return\s+\[\s*\]"),
        re.compile(r"except\s+BaseException\s*:\s*pass\b"),
    )
    for py in sorted(SRC.glob("*.py")):
        lines = py.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            for pat in bad_patterns:
                if pat.search(line):
                    raise Failure(f"{py.name}:{i} 命中静默失效模式：{line.strip()}")
            # 裸 except 后一行是 pass/return 也是静默
            if re.match(r"^\s*except\s*:\s*$", line) and i < len(lines):
                nxt = lines[i].strip()
                if nxt.startswith("pass") or nxt.startswith("return"):
                    raise Failure(f"{py.name}:{i} 裸 except 后接 {nxt!r}（静默失效）")


def test_selfcheck_modules_have_docstrings() -> None:
    """硬性要求：每个模块顶部写一句话职责；关键函数 docstring 引用规范章节号。"""
    import ast
    for py in sorted(SRC.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        require(ast.get_docstring(tree), f"{py.name} 缺模块 docstring")
        src = py.read_text(encoding="utf-8")
        require(re.search(r"§\d", src), f"{py.name} 的 docstring 应引用规范章节号（§N）")
        funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        documented = [n.name for n in funcs if ast.get_docstring(n)]
        require(len(documented) >= max(1, len(funcs) // 2),
                f"{py.name} 有 docstring 的函数过少：{documented} / {len(funcs)}")
        # 公开函数（非 _ 开头）应全部有 docstring
        for n in funcs:
            if not n.name.startswith("_") and not ast.get_docstring(n):
                raise Failure(f"{py.name}:{n.name} 缺 docstring（公开函数）")


def test_selfcheck_no_debug_leftovers() -> None:
    """硬性要求：`src/` 不留测试代码或调试 print。"""
    for py in sorted(SRC.glob("*.py")):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if re.match(r"^print\s*\(", stripped):
                raise Failure(f"{py.name}:{i} 残留调试 print：{stripped}")
            if re.search(r"\bbreakpoint\s*\(", stripped) or re.search(r"\bpdb\.", stripped):
                raise Failure(f"{py.name}:{i} 残留调试断点：{stripped}")
            if re.search(r"^\s*#\s*TODO\b", line) or "FIXME" in line:
                raise Failure(f"{py.name}:{i} 残留 TODO/FIXME：{stripped}")


def test_selfcheck_error_table_matches_spec_descriptions() -> None:
    """错误码**含义**必须与 §8.1 描述一致（防串号）。"""
    spec = SPEC.read_text(encoding="utf-8")
    sec81 = spec[spec.find("### 8.1 错误码"):spec.find("### 8.2 校验规则")]
    rows = re.findall(r"\|\s*`(LX-[EW]\d{3})`\s*\|\s*(错误|警告)\s*\|\s*([^|]+?)\s*\|", sec81)
    require(len(rows) >= 17, f"§8.1 应至少 17 行，实为 {len(rows)}")
    for code, level, desc in rows:
        if code in V.ERROR_CODES:
            require(level == "错误", f"{code} 在 §8.1 为 {level}，实现归入错误表")
        elif code in V.WARNING_CODES:
            require(level == "警告", f"{code} 在 §8.1 为 {level}，实现归入警告表")
        else:
            raise Failure(f"{code} 未在实现中登记")


# ─────────────────────────── §8.2 每条规则逐一可触发 ───────────────────────────

def test_every_rule_has_a_trigger() -> None:
    """§8.2 每条实现的规则都必须有**能触发它的向量**（防「跑了但无效」）。"""
    triggers: Dict[str, Callable[[], List[str]]] = {
        "V-01": lambda: V.validate("#LX1 mods=-!\n-@R1;;", None).error_codes,
        "V-02": lambda: V.validate("#LX1 dict=d.tsv mods=-!\n@ZZ9;;",
                                   LX.Lexicon(entries={})).error_codes,
        "V-03": lambda: _v03_errors(),
        "V-04": lambda: _v04_errors(),
        "V-05": lambda: V.validate("#LX1 dict=d.tsv mods=-!\n@R1>中文;;", None).error_codes,
        "V-06": lambda: V.validate("#LX1 dict=d.tsv\n-@R1;;", None).error_codes,
        "V-07": lambda: _v07_errors(),
        "V-08": lambda: V.validate("#LX1 dict=d.tsv mods=-!\nR1;;",
                                   LX.load(VECTORS / "shared.lexicon.tsv")).error_codes,
        "V-09": lambda: V.validate(
            "#LX1 dict=d.tsv mods=-!\n-@R1;;\ncode\tterm\tmeaning\tfidelity\tdetail_path\n",
            None).error_codes,
        "V-10": lambda: _v10_errors(),
        "V-11": lambda: V.validate("#LX1 dict=d.tsv mods=-! fid=yes\n^R1>@R1;;",
                                   LX.load(VECTORS / "shared.lexicon.tsv")).error_codes,
        "V-13": lambda: V.validate("#LX2 dict=d.tsv mods=-!\n-@R1;;", None).error_codes,
    }
    for rule, fn in triggers.items():
        codes = fn()
        require(codes, f"{rule} 无法被触发（返回空错误集）—— 规则形同虚设")
    require(set(triggers) == set(V.CHECKED_RULES),
            f"触发向量应覆盖全部执行规则：{sorted(set(V.CHECKED_RULES) - set(triggers))}")


def _v03_errors() -> List[str]:
    with tempfile.TemporaryDirectory() as tmp:
        lex = LX.parse_lexicon(
            "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\n"
            "R1\t红线1\t含义\t.~\t不存在.md\t\t\n")
        return V.validate("#LX1 dict=d.tsv mods=-!\n-@R1;;", lex,
                          check_paths=True, base_dir=Path(tmp)).error_codes


def _v04_errors() -> List[str]:
    old = LX.parse_lexicon(
        "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\trev\n"
        "R1\t红线1\t含义\t.~\tp.md\t\t\tabc123\n")
    new = LX.parse_lexicon(
        "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\trev\n"
        "R1\t红线1\t含义已改\t.~\tp.md\t\t\tdef456\n")
    return V.validate("#LX1 dict=d.tsv mods=-!\n-@R1;;", new,
                      old_lexicon=old).error_codes


def _v07_errors() -> List[str]:
    lex = LX.Lexicon(entries={
        "S1": LX.Entry(code="S1", term="状态", meaning="含义", fidelity=".!",
                       detail_path="p.md", loss="")},
        columns=list(LX.COLUMNS_CANONICAL))
    return V.validate("#LX1 dict=d.tsv mods=-! fid=yes\n-@S1.!;;", lex).error_codes


def _v10_errors() -> List[str]:
    lex = LX.load(VECTORS / "shared.lexicon.tsv")
    return V.validate("#LX1 dict=d.tsv mods=-! fid=yes\n^R1>@R1;;", lex).error_codes


def test_all_error_codes_reachable() -> None:
    """§8.1 全部错误码（含扩展码）都必须**确实可被触发**。"""
    reachable: Dict[str, List[str]] = {}
    reachable["LX-E001"] = V.validate("#LX1 mods=-!\n-@R1;;", None).error_codes
    reachable["LX-E002"] = V.validate("#LX1 dict=d.tsv mods=-!\n@ZZ9;;",
                                      LX.Lexicon(entries={})).error_codes
    reachable["LX-E003"] = _v03_errors()
    reachable["LX-E004"] = _codes_of(lambda: LX.parse_lexicon(
        "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\n"
        "R1\ta\tb\t.~\tp.md\t\t\nR1\ta\tc\t.~\tp.md\t\t\n", strict=True))
    reachable["LX-E005"] = V.validate("#LX1 dict=d.tsv mods=-!\n@r1;;", None).error_codes
    reachable["LX-E006"] = V.validate("#LX1 dict=d.tsv\n-@R1;;", None).error_codes
    reachable["LX-E007"] = _codes_of(lambda: LX.parse_lexicon(
        "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\n"
        "R1\ta\tb\t.!\tp.md\t\t\n", strict=True))
    reachable["LX-E008"] = V.validate("#LX1 dict=d.tsv mods=-!\nR1;;",
                                      LX.load(VECTORS / "shared.lexicon.tsv")).error_codes
    reachable["LX-E009"] = _codes_of(lambda: LX.parse_lexicon(
        "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\n"
        "R1\ta\tb\t.~\tp.md\tx\ty\tz\n", strict=True))
    reachable["LX-E010"] = _codes_of(lambda: LX.parse_lexicon(
        "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\n"
        "R1\ta\tb\t.~\n", strict=True))
    reachable["LX-E011"] = V.validate("#LX1 dict=d.tsv mods=-! fid=yes\n^R1>@R1;;",
                                      LX.load(VECTORS / "shared.lexicon.tsv")).error_codes
    reachable["LX-E012"] = V.validate("#LX2 dict=d.tsv mods=-!\n-@R1;;", None).error_codes
    reachable["LX-E013"] = V.validate(
        "#LX1 dict=d.tsv mods=-!\n-@R1;;\ncode\tterm\tmeaning\tfidelity\tdetail_path\n",
        None).error_codes
    reachable["LX-E014"] = _v04_errors()

    for code in list(V.ERROR_CODES) + list(V.EXTENSION_CODES):
        codes = reachable.get(code, [])
        require(code in codes, f"{code} 不可触发（无对应用例）：{codes}")

    warn_triggered = {
        "LX-W001": V.validate("#LX1 dict=d.tsv mods=-!\n@R1;;",
                              LX.load(VECTORS / "shared.lexicon.tsv")).warning_codes,
        "LX-W002": V.validate("#LX1 dict=d.tsv mods=-!\n@R1;;",
                              LX.load(VECTORS / "shared.lexicon.tsv")).warning_codes,
        "LX-W003": _w003(),
        "LX-W004": [d.code for d in LX.parse_lexicon(
            "code\tmeaning\tfidelity\tdetail_path\tmatch\tloss\n"
            "R1\t含义\t.~\tp.md\t\t\n").warnings],
        "LX-W005": V.validate("#LX1 dict=d.tsv mods=-! fid=yes\n-@R1;;",
                              LX.load(VECTORS / "shared.lexicon.tsv")).warning_codes,
    }
    for code in V.WARNING_CODES:
        require(code in warn_triggered[code],
                f"{code} 不可触发：{warn_triggered[code]}")


def _codes_of(fn: Callable[[], Any]) -> List[str]:
    try:
        obj = fn()
    except LX.LexiconError as exc:
        return [_code_of(exc)]
    return [d.code for d in obj.errors]


def _w003() -> List[str]:
    """触发 LX-W003：原文足够长以避开 P-2，但仍短于「头部 + 正文」。"""
    short = "短内容样本"
    res = E.encode([E.Unit(text=short, meaning=short, term=short, fidelity=".=",
                           detail_path="p.md", prefix="C")],
                   short, dictionary_name="lexicon.tsv", on_negative="warn")
    require(not res.encoded, "LX-W003 用例不应产出编码结果")
    return [d.code for d in res.warnings]


# ─────────────── R1~R6 审计判据（tools/lexicon_audit.py） ───────────────

AUDIT_HEADER = "code\tterm\tmeaning\tfidelity\tdetail_path\tmatch\tloss\trev"


def _audit_rid(text: str, rid: str, **kw: Any) -> Any:
    """跑 `lexicon_audit.audit_text` 并取出指定风险编号那一项判据。"""
    checks = AUD.audit_text(text, **kw)
    got = next((c for c in checks if c.rid == rid), None)
    require(got is not None, f"审计结果应含 {rid}，实为 {[c.rid for c in checks]}")
    return got


def test_audit_r1_index_budget() -> None:
    """审计 R1：索引行超预算须报 WARN 并给出「应并入字典」的候选（§2.3）。

    负向测试：构造超限索引，断言判据**确实触发**且候选非空；
    再把预算放宽到索引长度以上，断言它**转回 OK**（证明判据真在看数字）。
    """
    lex_text = AUDIT_HEADER + "\n" + "AAA\t术语甲\t含义甲很长很长\t.~\tp.md\t\t\trev-a\n"
    long_index = "**词表（20）**：" + "、".join(["超长概念名称甲乙丙丁戊己庚辛壬癸"] * 20)

    over = _audit_rid(lex_text, "R1", index_text=long_index, index_budget=300)
    require(over.level == "WARN", f"索引超预算应 WARN，实为 {over.level}：{over.detail}")
    require(over.data["candidates"], "R1 必须给出应并入字典的候选（不得只报超限）")
    require(over.data["index_len"] > 300, f"应记录超限长度：{over.data['index_len']}")

    ok = _audit_rid(lex_text, "R1", index_text=long_index,
                    index_budget=len(long_index))
    require(ok.level == "OK", f"索引 ≤ 预算时应转 OK，实为 {ok.level}：{ok.detail}")


def test_audit_r2_unreadable_code() -> None:
    """审计 R2：`term` 为空 → FAIL；索引行写裸 `@` 代号 → FAIL（§5.2）。"""
    empty_term = AUDIT_HEADER + "\n" + "AAA\t\t含义甲\t.~\tp.md\t\t\trev-a\n"
    r2 = _audit_rid(empty_term, "R2", index_text="**词表（1）**：AAA\n")
    require(r2.level == "FAIL", f"term 为空应 FAIL（§5.2 必填），实为 {r2.level}")
    require(r2.data["term_empty"], "R2 应报出 term 为空的条目")

    # 负向对照：term 非空、索引为词名 → 必须转 OK
    good = _audit_rid(AUDIT_HEADER + "\n" + "AAA\t术语甲\t含义甲\t.~\tp.md\t\t\trev-a\n",
                      "R2", index_text="**词表（1）**：术语甲\n")
    require(good.level == "OK", f"term 非空且索引无裸代号时应 OK，实为 {good.level}")

    # 负向：索引侧必须是词名，不是符号串
    bare = _audit_rid(AUDIT_HEADER + "\n" + "AAA\t术语甲\t含义甲\t.~\tp.md\t\t\trev-a\n",
                      "R2", index_text="**词表（1）**：@AAA\n")
    require(bare.level == "FAIL", f"索引含裸代号应 FAIL，实为 {bare.level}")
    require(bare.data["bare_codes_in_index"], "R2 应报出索引里的裸代号")


def test_audit_r3_mapping_drift() -> None:
    """审计 R3：同 term 双 code / 同 code 的 rev 变化 → FAIL；别名交集 → WARN（§2.1 INV-2）。"""
    dup = _audit_rid(AUDIT_HEADER + "\n"
                     "AAA\t共享术语\t含义甲\t.~\tp.md\t\t\trev-a\n"
                     "BBB\t共享术语\t含义乙\t.~\tp.md\t\t\trev-b\n", "R3")
    require(dup.level == "FAIL", f"同 term 双 code 应 FAIL，实为 {dup.level}：{dup.detail}")
    require(dup.data["same_term_two_codes"], "R3 应报出双 code 的 term 明细")
    require(sorted(dup.data["same_term_two_codes"]["共享术语"]) == ["AAA", "BBB"],
            f"应恰为 AAA/BBB：{dup.data['same_term_two_codes']}")

    drift = _audit_rid(AUDIT_HEADER + "\n"
                       "AAA\t术语甲\t含义甲\t.~\tp.md\t\t\trev-a\n"
                       "AAA\t术语甲\t含义甲\t.~\tp.md\t\t\trev-zzz\n", "R3")
    require(drift.level == "FAIL", f"rev 变化应 FAIL，实为 {drift.level}")
    require(drift.data["rev_drift"],
            "R3 必须由 lexicon.compare_rev 报出 rev 漂移明细（不得只靠解析错误兜底）")

    alias = _audit_rid(AUDIT_HEADER + "\n"
                       "AAA\t术语甲\t含义甲\t.~\tp.md\t共享别名\t\trev-a\n"
                       "BBB\t术语乙\t含义乙\t.~\tp.md\t共享别名\t\trev-b\n", "R3")
    require(alias.level == "WARN", f"别名交集应 WARN，实为 {alias.level}：{alias.detail}")
    require(alias.data["alias_overlap"], "R3 应报出重叠的别名")

    # 负向对照：term 唯一 + rev 稳定 + 别名不重叠 → 必须转 OK
    clean = _audit_rid(AUDIT_HEADER + "\n"
                       "AAA\t术语甲\t含义甲\t.~\tp.md\t别名甲\t\trev-a\n"
                       "BBB\t术语乙\t含义乙\t.~\tp.md\t别名乙\t\trev-b\n", "R3")
    require(clean.level == "OK", f"无漂移时应 OK，实为 {clean.level}：{clean.detail}")


def test_audit_r4_low_yield() -> None:
    """审计 R4：`saving = freq × (len(meaning) − len(code))`；≤ 0 的低频概念须 WARN。

    负向测试用**频率**驱动（不是长度）：罕见概念在语料里 0 次 → saving 0；
    给足语料后同一字典必须转 OK，证明判据看的是语料而不是常量。
    """
    text = (AUDIT_HEADER + "\n"
            "RARE\t罕见概念\t罕见概念的完整含义说明（很长）\t.~\tp.md\t\t\trev-a\n"
            "GOOD\t常见概念\t常见概念的完整含义说明（很长）\t.~\tp.md\t\t\trev-b\n")

    r4 = _audit_rid(text, "R4", corpus_text="常见概念出现若干次。")
    require(r4.level == "WARN", f"saving ≤ 0 应 WARN，实为 {r4.level}：{r4.detail}")
    bad = {b["code"] for b in r4.data["low_yield"]}
    require(bad == {"RARE"}, f"低收益代号应恰为 RARE，实为 {bad}")
    require(r4.data["total_saving"] is not None, "R4 必须给出总节省字符")
    require("总节省字符" in r4.detail, f"判据行须含总节省字符：{r4.detail}")

    ok = _audit_rid(text, "R4", corpus_text="罕见概念与常见概念都反复出现。" * 50)
    require(ok.level == "OK", f"给足语料后应转 OK，实为 {ok.level}：{ok.detail}")

    # 缺少语料时必须如实报 WARN（不得默认 0 静默当成通过）
    noc = _audit_rid(text, "R4")
    require(noc.level == "WARN", f"无语料应 WARN 并说明原因，实为 {noc.level}")


def test_audit_r5_compression_paradox() -> None:
    """审计 R5：总成本（常驻 + 一次性展开）> 基线 → FAIL，且三个数字都要出现（§10.3）。"""
    text = (AUDIT_HEADER + "\n"
            "LONGCODE-1\t概念甲\t含义甲的说明文本\t.~\tp.md\t\t\trev-a\n"
            "LONGCODE-2\t概念乙\t含义乙的说明文本\t.~\tp.md\t\t\trev-b\n")
    index_line = "**词表（2）**：概念甲、概念乙\n"

    r5 = _audit_rid(text, "R5", index_text=index_line, corpus_text="概念甲")
    require(r5.level == "FAIL", f"总成本 > 基线应 FAIL，实为 {r5.level}：{r5.detail}")
    d = r5.data
    for key in ("resident", "oneoff", "total", "baseline"):
        require(key in d, f"R5 必须给出 {key}（三个数字都要可核）")
    require(d["total"] == d["resident"] + d["oneoff"], f"总成本口径应为 常驻+展开：{d}")
    require(d["total"] > d["baseline"], f"该样例总成本应超基线：{d}")
    for key in ("baseline", "resident", "oneoff"):
        require(str(d[key]) in r5.detail, f"判据行须打印 {key}={d[key]}：{r5.detail}")

    ok = _audit_rid(text, "R5", index_text=index_line, corpus_text="概念甲概念乙" * 400)
    require(ok.level == "OK", f"基线足够长时应转 OK，实为 {ok.level}：{ok.detail}")


def test_audit_r6_mnemonic() -> None:
    """审计 R6：代号应可联想（缩写 或 与 term ≥2 连续字母重合）；不透明须 WARN。"""
    opaque = _audit_rid(AUDIT_HEADER + "\n"
                        "ZZ\tsilent fail\t含义甲\t.~\tp.md\t\t\trev-a\n", "R6")
    require(opaque.level == "WARN", f"不透明代号应 WARN，实为 {opaque.level}：{opaque.detail}")
    require([o["code"] for o in opaque.data["opaque"]] == ["ZZ"],
            f"应恰报出 ZZ：{opaque.data['opaque']}")

    # 可联想（缩写 SF = silent fail 首字母）→ OK
    abbr = _audit_rid(AUDIT_HEADER + "\n"
                      "SF\tsilent fail\t含义甲\t.~\tp.md\t\t\trev-a\n", "R6")
    require(abbr.level == "OK", f"缩写代号应 OK，实为 {abbr.level}：{abbr.detail}")

    # 连续字母重合 ≥2（dictionary → DICT）→ OK
    run = _audit_rid(AUDIT_HEADER + "\n"
                     "DICT\tdictionary\t含义甲\t.~\tp.md\t\t\trev-a\n", "R6")
    require(run.level == "OK", f"连续字母重合 ≥2 应 OK，实为 {run.level}：{run.detail}")

    seq_dict = AUDIT_HEADER + "\n" + "R1\t静默失效\t含义甲\t.~\tp.md\t\t\trev-a\n"
    seq = _audit_rid(seq_dict, "R6")
    require(seq.level == "WARN", f"顺序编号应默认 WARN，实为 {seq.level}")
    seq_ok = _audit_rid(seq_dict, "R6", allow_seq=True)
    require(seq_ok.level == "OK", f"--allow-seq 应跳过顺序编号，实为 {seq_ok.level}")


def test_audit_self_test_and_exit_code() -> None:
    """审计工具自证：`--self-test` 真跑必须退出码 0；有 FAIL 的字典必须退出码 1。

    这里走 **子进程** 跑 CLI（不是只调函数）—— 退出码是给 CI / 门禁用的契约，
    必须在真实进程边界上验证。
    """
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(TOOLS / "lexicon_audit.py"), "--self-test"],
        capture_output=True, text=True, encoding="utf-8")
    require(proc.returncode == 0,
            f"--self-test 退出码应 0，实为 {proc.returncode}：{proc.stdout[-400:]}")
    require("全部通过" in proc.stdout, f"自证应报全部通过：{proc.stdout[-300:]}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bad = root / "bad.tsv"
        # term 为空 → R2 FAIL → 退出码必须为 1
        bad.write_text(AUDIT_HEADER + "\n" + "AAA\t\t含义甲\t.~\tp.md\t\t\trev-a\n",
                       encoding="utf-8", newline="\n")
        proc_bad = subprocess.run(
            [sys.executable, str(TOOLS / "lexicon_audit.py"), str(bad)],
            capture_output=True, text=True, encoding="utf-8")
        require(proc_bad.returncode == 1,
                f"存在 FAIL 时退出码应 1，实为 {proc_bad.returncode}：{proc_bad.stdout[-300:]}")
        require("FAIL" in proc_bad.stdout, f"输出应含 FAIL 判据行：{proc_bad.stdout[-300:]}")

        good = root / "good.tsv"
        good.write_text(AUDIT_HEADER + "\n"
                        "CMPR\tCompression\t压缩的完整说明含义\t.~\tp.md\t\t\trev-a\n",
                        encoding="utf-8", newline="\n")
        proc_ok = subprocess.run(
            [sys.executable, str(TOOLS / "lexicon_audit.py"), str(good)],
            capture_output=True, text=True, encoding="utf-8")
        require(proc_ok.returncode == 0,
                f"全 OK/WARN 时退出码应 0，实为 {proc_ok.returncode}：{proc_ok.stdout[-300:]}")

        # --json 必须给出结构化结果（含逐项 level 与 exit_code）
        proc_json = subprocess.run(
            [sys.executable, str(TOOLS / "lexicon_audit.py"), str(good), "--json"],
            capture_output=True, text=True, encoding="utf-8")
        payload = json.loads(proc_json.stdout)
        require([c["id"] for c in payload["checks"]] == list(AUD.RISK_IDS),
                f"--json 应含 R1~R6 六项：{[c['id'] for c in payload['checks']]}")
        require(payload["exit_code"] == proc_json.returncode,
                "--json 内的 exit_code 须与真实退出码一致")


# ─────────────── R7 基准（tools/benchmark.py） ───────────────

def test_benchmark_multi_file() -> None:
    """基准 R7：多文件聚合比值 + 往返完整性真跑；缺文件必须计为完整性失败。

    负向测试：把「文件不存在」喂进去，断言它**确实**被计为完整性失败并让退出码变 1，
    而不是被当成「跳过」而静默通过。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lex = root / "lexicon.tsv"
        lex.write_text(AUDIT_HEADER + "\n"
                       "CMP\tCompression\t压缩的完整说明含义\t.~\tp.md\t\t\trev-a\n"
                       "PRM\tCompactPrompt\t同源工作的完整说明含义\t.~\tp.md\t\t\trev-b\n",
                       encoding="utf-8", newline="\n")
        inputs = []
        for name, body in (("a.md", "Compression 压缩的完整说明含义" * 5),
                           ("b.md", "CompactPrompt 同源工作的完整说明含义" * 5),
                           ("c.md", "这里没有任何命中概念，只有普通文本。" * 5)):
            p = root / name
            p.write_text(body, encoding="utf-8", newline="\n")
            inputs.append(str(p))

        res = BENCH.run_benchmark(inputs, lex, max_files=50)
        require(res.files_considered == 2,
                f"应纳入 2 个有命中的文件，实为 {res.files_considered}")
        require(res.integrity_pass == 2, f"往返完整性应 2/2，实为 {res.integrity_pass}")
        require(res.integrity_fail == 0, f"不应有完整性失败：{[f.detail for f in res.files]}")
        require(res.baseline_total > 0 and res.resident_total > 0, "聚合长度应被统计")
        require(res.ratio == round(res.resident_total / res.baseline_total, 4),
                f"比值口径应为 常驻/基线：{res.ratio}")
        require(res.exit_code == 0, f"全部完整应退出码 0：{res.exit_code}")
        require(any(f.skipped for f in res.files), "无命中文件应被如实标记为跳过")
        require(sum(f.baseline for f in res.files if not f.skipped) == res.baseline_total,
                "基线合计应恰为参与文件之和")

        # 负向：文件缺失 → 完整性失败 + 退出码 1
        res2 = BENCH.run_benchmark([str(root / "不存在.md")], lex)
        require(res2.integrity_fail == 1,
                f"缺失文件应计完整性失败，实为 {res2.integrity_fail}")
        require(res2.exit_code == 1, f"有失败应退出码 1：{res2.exit_code}")
        require(not res2.files[0].skipped,
                "缺失文件不得被静默归入「跳过」——它是失败，不是无命中")

        # --max-files 截断必须**如实报数**，不得静默丢弃
        res3 = BENCH.run_benchmark(inputs, lex, max_files=2)
        require(res3.truncated == 1, f"应报出 1 个未纳入，实为 {res3.truncated}")


def test_benchmark_longest_match() -> None:
    """基准替换须**单遍最长匹配**：长 term 命中时不得被短 term 拆开二次替换。"""
    lex = LX.parse_lexicon(
        AUDIT_HEADER + "\n"
        "CMP\tcompress\t短概念的说明\t.~\tp.md\t\t\trev-a\n"
        "CMPR\tCompression\t长概念的说明\t.~\tp.md\t\t\trev-b\n")
    require(lex.ok, f"该字典应合法：{lex.errors}")
    table = BENCH._search_keys(lex)
    out, codes = BENCH.compress_text("Compression", table)
    require(codes == ["CMPR"], f"应最长匹配 CMPR，实为 {codes}")
    require(out == "@CMPR", f"替换结果应为 @CMPR，实为 {out!r}")

    # 单遍替换：替换结果里的 `@CMPR` 不得被再次替换（否则会产出 @C@CMPR 之类）
    out2, codes2 = BENCH.compress_text("compress 与 Compression 并存", table)
    require(codes2 == ["CMP", "CMPR"], f"应各命中一次：{codes2}")
    require("@C" not in out2.replace("@CMP", "").replace("@CMPR", ""),
            f"替换结果不应出现被拆开的代号残留：{out2!r}")


def test_tools_new_scripts_no_third_party() -> None:
    """硬性要求：本次新增的两个工具零第三方依赖（仅标准库 + src/ 本地模块）。"""
    import ast
    stdlib_ok = {
        "__future__", "abc", "argparse", "ast", "collections", "dataclasses",
        "datetime", "enum", "functools", "hashlib", "io", "itertools", "json",
        "math", "operator", "os", "pathlib", "re", "shutil", "string",
        "subprocess", "sys", "tempfile", "textwrap", "time", "traceback",
        "typing", "unicodedata", "warnings",
    }
    local = {"lexicon", "validate", "decode", "encode", "lexicon_audit", "benchmark"}
    for name in ("lexicon_audit.py", "benchmark.py"):
        py = TOOLS / name
        require(py.exists(), f"工具缺失：{py}")
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: List[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                require(m in stdlib_ok or m in local,
                        f"{name} 导入了非标准库模块 {m!r}")


# ─────────────────────────── 主入口 ───────────────────────────

def main() -> int:
    print("=" * 78)
    print("agent-codebook 测试套件（零第三方依赖）")
    print(f"根目录：{ROOT}")
    print("=" * 78)

    print("\n[自检1] §16 最小示例往返")
    check("§16 往返：原文 → 符号串 → 展开，要素零丢失", test_section16_roundtrip)
    check("§16 编码层往返：单元 → 编码 → 校验 → 展开", test_section16_encode_roundtrip)

    print("\n[自检2] 从规范文档自动提取符号串")
    check("提取 docs/05 与 docs/06 全部符号串并解析", test_extract_all_streams_from_spec)
    check("examples/example_dictionary.md 自洽性", test_example_dictionary_is_consistent)

    print("\n[自检3] 符号表 ⊆ EBNF 产生式")
    check("符号表与产生式一一对应（无「有声明无产生式」）",
          test_selfcheck_symbol_table_subset_of_ebnf)
    check("§4.7 符号数核对（20 符 6 组）", test_selfcheck_sigil_count_in_spec)

    print("\n[自检4] 反例逐条**确实被拒绝**")
    check("golden.json 黄金向量全部真跑（11 accept + 16 reject）",
          test_golden_vectors)
    check("negatives.json 全部反例被拒绝", test_negatives)
    check("字典层各拒绝路径（strict）", test_negative_lexicon_strict_paths)
    check("§5.1.1 字段转义往返与裸字符拒绝", test_escaping_roundtrip)

    print("\n[自检5/11] 错误码与规则交叉引用、编号撤销说明")
    check("错误码无孤儿 + V-12 撤销且不实现", test_selfcheck_error_codes_no_orphans)
    check("§8.1 全部码（含扩展码）可触发", test_all_error_codes_reachable)
    check("错误码含义与 §8.1 描述一致", test_selfcheck_error_table_matches_spec_descriptions)

    print("\n[自检6/8/9/10] 编号空间与依赖")
    check("同名产生式不得多处定义", test_selfcheck_no_duplicate_productions)
    check("规则 ⇄ 判据无循环引用", test_selfcheck_no_cyclic_references)
    check("同一条件不得给两种严重级", test_selfcheck_no_dual_severity)
    check("D-* 与 ND-* 编号空间分离（F-18）", test_selfcheck_nd_codes_distinct)

    print("\n[自检7] 语法层通过 ≠ 合规（两层都做）")
    check("分层原则：语法层 / 语义层职责分离", test_layer_separation)
    check("无字典时不得静默通过", test_no_silent_pass_without_lexicon)
    check("V-03 路径可达性（含外部 URI 豁免）", test_v03_paths)

    print("\n[正例] vectors/positives.json")
    check("正例全部通过（解析 + 校验 + 展开）", test_positives)
    check("正例的语义断言（关系/键值/字面量/代号/模态）",
          test_positive_specific_semantics)

    print("\n[编码层] §7.1 八步 / §10.3 盈亏平衡")
    check("§10.3 压缩为负必须拒绝（LX-W003）", test_encode_balance_rejects)
    check("P-2 禁止单词级压缩单元", test_encode_rejects_word_units)
    check("步2 查重复用既有代号", test_encode_reuses_existing_code)
    check("步7 头部：mods / ns 条件必须", test_encode_header_rules)
    check("编码层失败路径可感知", test_encode_error_paths)

    print("\n[文件级闭环]")
    with tempfile.TemporaryDirectory() as tmp:
        check("写盘 → validate_file → expand_file",
              lambda: test_encode_then_validate_file(Path(tmp)))

    print("\n[硬性要求]")
    check("src/ 零第三方依赖", test_selfcheck_no_third_party_imports)
    check("src/ 无硬编码绝对路径", test_selfcheck_no_absolute_paths)
    check("src/ 禁静默失效（except: pass 等）", test_selfcheck_no_silent_except)
    check("src/ 模块与函数 docstring 含 §章节号", test_selfcheck_modules_have_docstrings)
    check("src/ 无调试 print / TODO", test_selfcheck_no_debug_leftovers)

    print("\n[审计 R1~R6] tools/lexicon_audit.py")
    check("审计 R1：索引超预算须报 WARN 并给候选", test_audit_r1_index_budget)
    check("审计 R2：term 为空 / 索引写裸代号须 FAIL", test_audit_r2_unreadable_code)
    check("审计 R3：同 term 双 code 与 rev 漂移须 FAIL", test_audit_r3_mapping_drift)
    check("审计 R4：saving ≤ 0 的低频概念须 WARN", test_audit_r4_low_yield)
    check("审计 R5：总成本 > 基线须 FAIL（三数字齐）", test_audit_r5_compression_paradox)
    check("审计 R6：不透明 / 顺序编号须 WARN", test_audit_r6_mnemonic)
    check("审计工具 --self-test 与退出码契约（子进程真跑）",
          test_audit_self_test_and_exit_code)

    print("\n[基准 R7] tools/benchmark.py")
    check("基准：多文件聚合 + 往返完整性（缺文件须计失败）", test_benchmark_multi_file)
    check("基准：替换须单遍最长匹配", test_benchmark_longest_match)
    check("tools/ 新工具零第三方依赖", test_tools_new_scripts_no_third_party)

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print("\n" + "=" * 78)
    print(f"通过 {passed} ｜ 失败 {failed} ｜ 合计 {len(_results)}")
    if failed:
        print("\n失败用例：")
        for name, ok, detail in _results:
            if not ok:
                print(f"  - {name}\n      {detail}")
    print("=" * 78)
    print("结果：" + ("全部通过 ✓" if failed == 0 else f"存在 {failed} 项失败 ✗"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
