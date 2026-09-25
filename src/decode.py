"""decode.py — 展开（解码）层：按 §9.1 六步展开符号串，遵守 §9.3 ND-1~ND-4。

规范来源：`docs/05_引索符系统规范.md`
  §9.1 步骤与完成判据（每步均有判据与失败码）
  §9.2 成功判据（三条同时满足才算成功）
  §9.3 禁止静默降级 ND-1~ND-4
  §4.2 `@` 必展开 / `^` 复用（复用须存在**先行** `@code`）
  §4.5 保真度：`.!` 为有损项，**禁止**作为最终依据
  §5.2 展开落在 `meaning`（多数场景读到这层即足够）
  §3.3 `detail_path` 相对路径相对记录它的文件所在目录解析

引用方向（§9 头部注）：只允许 §8 → §9（`V-10` 引用 §9.2 判据），本节**禁止**反向引用 `V-10`。
`^` 的先行性由 §8.2 `V-11` 独立校验，**不属于**本节判据。

零第三方依赖。本模块**不**吞异常：任何失败都抛 `DecodeError`（带 §8.1 错误码）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from lexicon import Entry, Lexicon, Diagnostic
from validate import (
    KeyValue, Literal, Reference, Stream, Term, Unit, ValidationError, parse_stream,
)

__all__ = [
    "DecodeError", "Part", "Expanded", "Expansion", "decode", "expand_file",
    "verify_expandable", "SUCCESS_CRITERIA",
]

# §9.2 成功判据（逐条可核对）
SUCCESS_CRITERIA: Tuple[str, ...] = (
    "所有步骤完成",
    "无任何 LX-E* 级错误",
    "每个 @ 引用都被解析为具体含义（无跳过、无占位）",
)


class DecodeError(Exception):
    """展开失败。`code` 为 §8.1 错误码；`step` 为 §9.1 步骤号。"""

    def __init__(self, code: str, message: str, step: int = 0) -> None:
        self.code = code
        self.message = message
        self.step = step
        loc = f"§9.1 步{step} " if step else ""
        super().__init__(f"{code}: {loc}{message}")


@dataclass
class Part:
    """展开结果的一个片段（逐原子对应）。"""

    text: str
    kind: str                 # code（@/^ 引用）| keyvalue | literal
    code: str = ""
    fidelity: str = ""
    loss: str = ""
    modality: str = ""
    detail_path: str = ""
    lossy: bool = False       # fidelity == ".!" → §4.5 禁止作最终依据
    reused: bool = False      # True 表示来自 `^` 复用

    def __str__(self) -> str:
        return self.text


@dataclass
class Expanded:
    """一个单元（§3.1 `unit`）的展开结果。"""

    parts: List[Part] = field(default_factory=list)
    relations: List[str] = field(default_factory=list)
    terminator: str = ";;"
    line: int = 0
    placeholder: bool = False       # 必须恒为 False（ND-3）

    @property
    def modality(self) -> str:
        return self.parts[0].modality if self.parts else ""

    @property
    def lossy_parts(self) -> List[Part]:
        return [p for p in self.parts if p.lossy]

    @property
    def lossy(self) -> bool:
        """含 `.!` 项 —— 调用方**禁止**据此下最终结论（§4.5 / ND-4）。"""
        return any(p.lossy for p in self.parts)

    def text(self, sep: str = " ") -> str:
        """把各片段按关系符串起（无关系符时用 `sep`）。"""
        if not self.parts:
            return ""
        out = [self.parts[0].text]
        for i, rel in enumerate(self.relations, start=1):
            if i < len(self.parts):
                out.append(f"{rel}{self.parts[i].text}")
        return sep.join(out) if not self.relations else " ".join(out)


@dataclass
class Expansion:
    """一次完整展开的结果（§9.2）。"""

    units: List[Expanded] = field(default_factory=list)
    header_dict: str = ""
    steps_done: List[int] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)     # ND-4 提示等
    lexicon_source: str = ""

    @property
    def ok(self) -> bool:
        """§9.2 成功判据（3）：无占位、无跳过。前两条由「不抛异常」保证。"""
        return all(not u.placeholder for u in self.units)

    @property
    def lossy(self) -> bool:
        return any(u.lossy for u in self.units)

    @property
    def text(self) -> str:
        return "\n".join(u.text() for u in self.units)

    def meanings(self) -> List[str]:
        """全部 `@`/`^` 引用解析出的具体含义（**不含**代号本身 —— ND-3）。"""
        return [p.text for u in self.units for p in u.parts if p.kind == "code"]

    def final_evidence_parts(self) -> List[Part]:
        """可作为最终依据的片段（排除 `.!` 有损项，§4.5）。"""
        return [p for u in self.units for p in u.parts if not p.lossy]


def _resolve_detail(lex: Lexicon, code: str, base_dir: Optional[Path]) -> str:
    """§9.1 步6 的路径解析（§3.3：相对路径相对记录它的文件所在目录）。"""
    e = lex.get(code)
    if e is None or not e.detail_path:
        return ""
    if e.external:
        return e.detail_path
    p = lex.resolve_detail_path(code, base_dir)
    return str(p) if p is not None else e.detail_path


def decode(
    text: str,
    lexicon: Lexicon,
    *,
    base_dir: Optional[Path] = None,
    read_detail: bool = False,
) -> Expansion:
    """按 §9.1 六步展开符号串。

    步1 读头部、定位字典 → 头部合法且 `dict` 可读（失败 `LX-E001` / `LX-E003`）
    步2 词法分析       → 无词法错误（失败 `LX-E005`）
    步3 对每个 `@code` 查字典 → 每个代号取到 `meaning`（失败 `LX-E002`）
    步4 对 `^code` 复用上文   → 存在**先行** `@code`（失败 `LX-E011`）
    步5 遇 `fidelity=.!` 标注 → 产出「不可作最终依据」提示（失败 `LX-E007`）
    步6 按需读 `detail_path`  → 路径可达或为外部 URI（失败 `LX-E003`）

    §9.3 四条禁止静默降级全部生效（见 `ND_*` 常量与各分支注释）。
    `read_detail=True` 时执行步6 的可达性判定；`False` 时仅解析路径不判可达
    （登记在 `steps_done` 里的仍是 6，但可达性判定明确未做 —— 记为 note）。

    成功返回 `Expansion`；任何失败抛 `DecodeError`，**绝不**返回空结果（ND-1）。
    """
    if lexicon is None:
        # ND-1：字典不可读时**禁止**返回空结果
        raise DecodeError("LX-E003", "字典不可读：未提供字典（ND-1）", step=1)
    if not lexicon.ok and not lexicon.entries:
        # 字典结构性失败且无可用条目 —— 明确失败，不冒充实为「空字典」
        first = lexicon.errors[0] if lexicon.errors else None
        raise DecodeError(
            getattr(first, "code", "LX-E003"),
            f"字典不可用（存在结构性错误）：{first.message if first else '未知'}（ND-1）",
            step=1)

    steps: List[int] = []
    exp = Expansion(lexicon_source=lexicon.source)

    # ── 步1：读头部，定位字典 ──
    try:
        stream: Stream = parse_stream(text)
    except ValidationError as exc:
        # 步2 的词法失败也在此暴露（`LX-E005` 等）
        raise DecodeError(exc.code, exc.message, step=2) from exc

    header = stream.header
    if not header.dict_path:
        raise DecodeError("LX-E001", "头部缺少 `dict`，无法定位字典（§3.3）", step=1)
    exp.header_dict = header.dict_path
    steps.append(1)

    if lexicon.source and header.dict_path != Path(lexicon.source).name:
        exp.notes.append(
            f"头部声明 dict={header.dict_path}，实际使用 {lexicon.source}；"
            "路径以调用方提供的字典为准（§3.3）")
    steps.append(2)

    # ── 步3/4/5/6：逐单元展开 ──
    expanded_map: Dict[str, Entry] = {}      # 已展开的代号（供 `^` 复用；§4.2）

    for unit in stream.units:
        ex = Expanded(relations=list(unit.relations), terminator=unit.terminator,
                      line=unit.line)

        for term in unit.terms:
            atom = term.atom

            if isinstance(atom, Reference):
                if atom.sigil == "@":
                    # 步3：查字典取 meaning
                    entry = lexicon.get(atom.code)
                    if entry is None:
                        # ND-2：遇未定义代号**禁止**跳过
                        raise DecodeError(
                            "LX-E002",
                            f"代号 {atom.code!r} 未在字典中定义（ND-2：禁止跳过）", step=3)
                    expanded_map[atom.code] = entry
                    part = _entry_to_part(entry, term, base_dir)
                else:
                    # 步4：`^` 复用上文 —— 必须存在先行 `@code`
                    if atom.code not in expanded_map:
                        raise DecodeError(
                            "LX-E011",
                            f"^ {atom.code} 复用了一个从未展开的代号"
                            f"（须存在先行 @{atom.code}，§4.2 / §9.1 步4）", step=4)
                    part = _entry_to_part(expanded_map[atom.code], term, base_dir)
                    part.reused = True

                if not part.text:
                    # ND-3：**禁止**用代号本身充作含义
                    raise DecodeError(
                        "LX-E002",
                        f"代号 {atom.code} 的 meaning 为空 —— 禁止用代号充作含义（ND-3）",
                        step=3)
                if part.lossy:
                    # ND-4：必须提示
                    if not part.loss:                       # 与 LX-E007 呼应
                        raise DecodeError(
                            "LX-E007",
                            f"{atom.code} 为 .! 但未注明丢失类别（§5.3 / ND-4）", step=5)
                    exp.notes.append(
                        f"{atom.code}: 有损项（.!），丢失类别「{part.loss}」"
                        f" —— **禁止作为最终依据**（§4.5 / ND-4）")
                ex.parts.append(part)

            elif isinstance(atom, KeyValue):
                ex.parts.append(Part(
                    text=f"{atom.key}={atom.value}", kind="keyvalue",
                    modality=term.modality, fidelity=term.fidelity))

            elif isinstance(atom, Literal):
                # ND-3：字面量按原样保留（它**不是**代号，无需也无法展开）
                ex.parts.append(Part(
                    text=atom.value, kind="literal",
                    modality=term.modality, fidelity=term.fidelity))

            else:                                            # pragma: no cover
                raise DecodeError(
                    "LX-E005", f"未知原子类型 {type(atom).__name__}（不静默跳过）", step=2)

        exp.units.append(ex)

    steps.extend([3, 4, 5])

    # ── 步6：路径可达（按需）──
    if read_detail:
        for u in exp.units:
            for p in u.parts:
                if p.kind != "code" or not p.detail_path:
                    continue
                e = lexicon.get(p.code)
                if e is None or e.external:
                    continue                    # §5.2：外部 URI 豁免
                target = lexicon.resolve_detail_path(p.code, base_dir)
                if target is None or not target.exists():
                    raise DecodeError(
                        "LX-E003", f"{p.code} 的 detail_path 不可达：{p.detail_path}", step=6)
    else:
        exp.notes.append(
            "步6：`read_detail=False`，仅解析 detail_path 未判可达性；"
            "如需可达性判定请传 read_detail=True 或走 §8.2 V-03")
    steps.append(6)

    exp.steps_done = steps

    # §9.2：三条判据（1）步骤完成（2）无 LX-E*（不抛异常即满足）（3）无占位
    if not exp.ok:                                          # pragma: no cover
        raise DecodeError("LX-E002", "存在未解析的占位片段（§9.2 判据 3）", step=3)
    return exp


def _entry_to_part(entry: Entry, term: Term, base_dir: Optional[Path]) -> Part:
    """把字典项转为展开片段。展开落在 `meaning`（§5.2：多数场景读到这层即足够）。"""
    return Part(
        text=entry.meaning,
        kind="code",
        code=entry.code,
        fidelity=entry.fidelity,
        loss=entry.loss,
        modality=term.modality,
        detail_path=entry.detail_path,
        lossy=entry.lossy,
    )


def expand_file(
    stream_path: "str | Path",
    lexicon_path: Optional["str | Path"] = None,
    *,
    read_detail: bool = False,
) -> Expansion:
    """从文件展开：按头部 `dict` 定位字典（§3.3），再按 §9.1 六步展开。

    字典路径按 §3.3 相对于**符号串文件所在目录**解析。
    """
    import lexicon as _lex

    sp = Path(stream_path)
    if not sp.exists():
        raise DecodeError("LX-E003", f"符号串文件不存在：{sp}", step=1)
    text = sp.read_text(encoding="utf-8")

    try:
        hdr = parse_stream(text).header
    except ValidationError as exc:
        raise DecodeError(exc.code, exc.message, step=2) from exc

    if lexicon_path is not None:
        target = Path(lexicon_path)
    else:
        target = hdr.resolve_dict(sp.parent)
    if target is None:
        raise DecodeError(
            "LX-E003",
            f"头部 dict={hdr.dict_path!r} 为外部 URI，本实现无法读取（§9.1 步1）", step=1)
    if not target.exists():
        raise DecodeError("LX-E003", f"字典不可读：{target}（ND-1）", step=1)

    lex = _lex.load(target)
    return decode(text, lex, base_dir=target.parent, read_detail=read_detail)


def verify_expandable(
    text: str,
    lexicon: Lexicon,
    *,
    read_detail: bool = False,
) -> List[Diagnostic]:
    """§8.2 `V-10` 的校验入口：对符号串执行 §9.1 全部步骤，核对 §9.2 成功判据。

    这是本项目核心主张（**机器可校验**）的落点：V-10 通过**必须**蕴含「能展开」。
    返回空列表 = 可展开；否则返回一条或多条诊断（不抛异常，便于聚合进 `Report`）。
    """
    try:
        decode(text, lexicon, read_detail=read_detail)
    except DecodeError as exc:
        return [Diagnostic(
            exc.code,
            f"V-10 失败（不可展开）：{exc.message}",
            0,
            f"§9.1 步{exc.step}" if exc.step else "",
        )]
    return []
