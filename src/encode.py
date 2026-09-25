"""encode.py — 编码层：按 §7.1 八步把概念单元改写为符号串，并执行 §10.3 盈亏平衡判据。

规范来源：`docs/05_引索符系统规范.md`
  §7.1 步骤与完成判据（步1~步8）   §7.2 禁止事项 P-1~P-7
  §10.3 盈亏平衡判据：`len(stream) + len(header) < len(original)`，否则**禁止编码**并报 `LX-W003`
  §2.2 压缩来源：**整条概念单元**，**禁止**以单词为单位替换（P-2）
  §6.2 命名空间：用类别前缀则**必须**声明 `ns`；纯序号可省略
  §4.3/§4.5 模态与保真度后缀       §5.2 字典必备列

零第三方依赖。失败一律以 `EncodeError`（带 §8.1 错误码）暴露，**不**静默丢弃单元：
`EncodeResult.skipped` 逐条记录未编码单元及其原因。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from lexicon import (
    COLUMNS_CANONICAL, Entry, FIDELITY_VALUES, Lexicon, escape_field,
)
from validate import (
    Diagnostic, HEADER_MAX, Report, SIGILS_MODALITY, SIGILS_RELATION,
    validate,
)

__all__ = [
    "EncodeError", "Unit", "EncodeResult", "encode", "build_header",
    "render_document", "dictionary_text", "save_dictionary",
]

# §3.3 头部属性顺序（仅影响可读性，不影响合法性）
HEADER_KEY_ORDER: Tuple[str, ...] = ("dict", "mods", "ns", "fid")
# §6.2 推荐类别前缀（不要求使用；纯序号可省略 ns）
RECOMMENDED_PREFIXES: Tuple[str, ...] = ("R", "S", "F", "D", "M")


class EncodeError(Exception):
    """编码失败。`code` 为 §8.1 错误码，或 §7.2 禁止项编号（如 P-2）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass
class Unit:
    """一个待编码的**概念单元**（§0.3：可被独立寻址的最小语义块）。

    `text` 为原文（**必须**是整条概念单元，不是单词 —— §2.2 / P-2）。
    `modality` / `relation` 承载 §4.3 / §4.4 的语义（P-4：禁止丢弃模态）。
    """

    text: str
    meaning: str = ""
    term: str = ""
    fidelity: str = ".~"
    detail_path: str = ""
    loss: str = ""
    match: str = ""
    modality: str = ""          # §4.3 模态符（可空）
    relation: str = ""          # §4.4 与**前一单元**的关系（可空）
    prefix: str = ""            # §6.2 类别前缀；空表示用纯序号 `C<n>`


@dataclass
class EncodeResult:
    """编码结果。

    `header` / `body` / `document` 三者关系：`document = header + NL + body`。
    §10.3 的 `stream` 指**去掉头部后的正文**（含终止符），头部单独计 ——
    故平衡判据写作 `len(body) + len(header) < len(original)`。
    """

    encoded: bool = False
    header: str = ""
    body: str = ""
    original: str = ""
    lexicon: Optional[Lexicon] = None
    units: List[Unit] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    errors: List[Diagnostic] = field(default_factory=list)
    warnings: List[Diagnostic] = field(default_factory=list)
    report: Optional[Report] = None
    notes: List[str] = field(default_factory=list)

    @property
    def document(self) -> str:
        """完整符号串（含头部），行尾带 LF。"""
        return f"{self.header}\n{self.body}\n" if self.header else self.body

    @property
    def header_len(self) -> int:
        return len(self.header)

    @property
    def body_len(self) -> int:
        """正文字符数（**不含换行**，含终止符）—— 与 §16.2 的计数口径一致。"""
        return len(self.body.replace("\n", ""))

    @property
    def stream_len(self) -> int:
        """符号串字符数（头部 + 正文，**不含换行**）—— 与 §16.2「45 字符」口径一致。"""
        return self.header_len + self.body_len

    @property
    def original_len(self) -> int:
        return len(self.original)

    @property
    def ratio(self) -> float:
        """压缩比（< 1 表示确实变短；§16.4 为 45/118 ≈ 0.38）。"""
        if self.original_len == 0:
            return 1.0
        return round(self.stream_len / self.original_len, 4)

    def format(self) -> str:
        lines = [f"编码：{'成功' if self.encoded else '未产出'}",
                 f"原文 {self.original_len} 字符 → 符号串 {self.stream_len} 字符"
                 f"（头部 {self.header_len} + 正文 {self.body_len}）",
                 f"比值 {self.ratio}"]
        lines += [f"  跳过：{s}" for s in self.skipped]
        lines += [f"  {d}" for d in self.errors]
        lines += [f"  {d}" for d in self.warnings]
        lines += [f"  提示：{n}" for n in self.notes]
        return "\n".join(lines)


# ─────────────────────────── 步7：头部（§3.3） ───────────────────────────

def build_header(
    attrs: Dict[str, str],
    *,
    version: str = "1",
) -> str:
    """生成头部 `#LX<version> <attr> ...`（§3.3）。

    头部**必须**位于首行、≤120 字符、含 `dict`；`mods` 与 `ns` 为条件必须。
    """
    if "dict" not in attrs or not attrs["dict"]:
        raise EncodeError("LX-E001", "头部必须声明 `dict`（§3.3）")
    parts = [f"#LX{version}"]
    for key in HEADER_KEY_ORDER:
        value = attrs.get(key)
        if value:
            parts.append(f"{key}={value}")
    header = " ".join(parts)
    if len(header) > HEADER_MAX:
        raise EncodeError(
            "LX-E001",
            f"头部 {len(header)} 字符 > {HEADER_MAX}（§3.3 头部是固定开销，须保持极小）")
    return header


# ─────────────────────────── 步1/步2：单元与查重 ───────────────────────────

def _lookup_existing(unit: Unit, entries: Dict[str, Entry]) -> Optional[Entry]:
    """步2 查重（**大小写敏感**）：原文或其别名命中既有条目则复用，**禁止**新建（P-6）。"""
    for e in entries.values():
        if unit.text == e.term:
            return e
        if unit.match and unit.text in e.aliases:
            return e
        if not unit.match and unit.text in e.aliases:
            return e
    return None


def _next_code(entries: Dict[str, Entry], prefix: str) -> str:
    """步4：分配代号。`prefix` 为空时用纯序号 `C<n>`（§6.2 允许省略 ns）。"""
    n = 1
    while f"{prefix or 'C'}{n}" in entries:
        n += 1
    return f"{prefix or 'C'}{n}"


def _is_word_level(text: str) -> bool:
    """P-2 启发式判据：单个「词」（无空白、无结构标点、极短）不构成概念单元。

    §0.3 判据是「可赋予独立代号，且该代号单独出现时含义确定」——
    单个短词几乎必然与正文里同形文本撞车，属「不可独立寻址」的典型形态。
    本函数仅用于**拒绝**明显违规；调用方可用 `allow_word_units=True` 覆盖。
    """
    if any(ch.isspace() for ch in text):
        return False
    stripped = text.strip("。，、；：！？.,;:!?")
    return len(stripped) <= 1


# ─────────────────────────── 主流程（§7.1 八步） ───────────────────────────

def encode(
    units: Sequence[Unit],
    original: str,
    *,
    dictionary_name: str = "lexicon.tsv",
    version: str = "1",
    existing: Optional[Lexicon] = None,
    validate_result: bool = True,
    on_negative: str = "reject",
    allow_word_units: bool = False,
) -> EncodeResult:
    """按 §7.1 八步编码。

    步1 划分概念单元（调用方给出 `units`；**禁止**单词切分 —— P-2，本函数复核）
    步2 查重（大小写敏感；有则复用，**禁止**新建同一概念）
    步3 评估收益（§10.3 盈亏平衡；未通过 → 报 `LX-W003`）
    步4 分配代号（§6.2：用前缀则调用方必须给 `ns`，否则报 `LX-E001`）
    步5 写入字典（必备列齐备，§5.2）
    步6 改写内容（`@code` + 模态 / 关系 / 保真符）
    步7 生成头部（`dict` 必声明；含模态符必须声明 `mods`，用前缀必须声明 `ns`）
    步8 校验（跑 §8.2；结果放在 `EncodeResult.report`）

    参数：
      original      改写前原文（§10.3 的分母；**必须**由调用方给出，不由本函数猜测）
      on_negative   `reject`（默认）：未通过 §10.3 即抛 `LX-W003`，**不产出**符号串；
                    `warn`：不抛异常，但 `encoded=False` 且 `warnings` 含 `LX-W003`
      validate_result  是否执行步8（§8.2 全量校验）
      allow_word_units 是否允许单词级单元（默认 `False`，即强制 P-2）

    任何失败都不静默：`skipped` 列出未编码单元及原因，`errors` / `warnings` 列出诊断。
    """
    res = EncodeResult(original=original)
    entries: Dict[str, Entry] = dict(existing.entries) if existing else {}
    cols = list(existing.columns) if existing and existing.columns else list(COLUMNS_CANONICAL)
    if "rev" in cols and "rev" not in COLUMNS_CANONICAL:
        cols = list(COLUMNS_CANONICAL) + ["rev"]

    used_prefix: set[str] = set()
    pending: List[Tuple[Unit, Entry]] = []

    for unit in units:
        # ── 步1：单元必须可独立寻址；禁止单词切分（P-2）──
        if not unit.text.strip():
            res.skipped.append("（空单元）")
            continue
        if not allow_word_units and _is_word_level(unit.text):
            res.skipped.append(
                f"{unit.text!r}：疑似单词级单元，不满足 §0.3「可独立寻址」判据（P-2）；"
                "确认无撞车风险可传 allow_word_units=True")
            continue
        if unit.modality and unit.modality not in SIGILS_MODALITY:
            raise EncodeError(
                "LX-E006",
                f"{unit.text[:20]!r} 的模态符 {unit.modality!r} 不在 §4.3 集合 {SIGILS_MODALITY}")
        if unit.relation and unit.relation not in SIGILS_RELATION:
            raise EncodeError(
                "LX-E010",
                f"{unit.text[:20]!r} 的关系符 {unit.relation!r} 不在 §4.4 集合 {SIGILS_RELATION}")
        if unit.fidelity not in FIDELITY_VALUES:
            raise EncodeError(
                "LX-E010", f"{unit.text[:20]!r} 的 fidelity {unit.fidelity!r} 非法（§5.2）")
        if unit.fidelity == ".!" and not unit.loss:
            # §4.5 / §5.2：标 .! 必须在字典 `loss` 列注明丢失类别
            raise EncodeError(
                "LX-E007", f"{unit.text[:20]!r} 标 .! 但未给 loss（§5.2 / §5.3）")
        if unit.prefix:
            used_prefix.add(unit.prefix)

        # ── 步2：查重（大小写敏感）──
        found = _lookup_existing(unit, entries)
        if found is not None:
            if found.fidelity != unit.fidelity:
                res.notes.append(
                    f"{unit.text[:20]!r} 复用既有代号 {found.code}，"
                    f"保真度沿用字典的 {found.fidelity}（调用方给的 {unit.fidelity} 未采用）")
            pending.append((unit, found))
            continue

        # ── 步4 / 步5：分配代号 + 写入字典 ──
        # 注：**不**在单元级预先判收益 —— §10.3 的判据是**整篇**的
        # `len(stream) + len(header) < len(original)`；单元级预检会把
        # 「短但正常」的单元误判为负收益，并把结论藏进 skipped。
        # 收益判定统一放下方「步3（整篇）」。
        code = _next_code(entries, unit.prefix)
        entry = Entry(
            code=code,
            term=unit.term or unit.text,
            meaning=unit.meaning or unit.text,
            fidelity=unit.fidelity,
            detail_path=unit.detail_path,
            match=unit.match,
            loss=unit.loss,
        )
        _assert_required(entry)
        entries[code] = entry
        pending.append((unit, entry))

    if not pending:
        res.skipped.append("（无任何可编码单元：全部未通过步1~步3）")

    # ── 步5：输出字典 ──
    lex = Lexicon(entries=entries, columns=cols, source="")
    res.lexicon = lex
    res.units = [u for u, _ in pending]

    if not pending:
        res.skipped.append("（无任何可编码单元：全部未通过步1 —— 原因见上方逐条记录）")
        res.notes.append(
            "无单元进入编码 → 不产出符号串（encoded=False）；"
            "原因全部登记在 skipped（不静默通过）")
        return res

    # ── 步7：生成头部 ──
    used_mods = sorted({u.modality for u, _ in pending if u.modality})
    attrs: Dict[str, str] = {"dict": dictionary_name}
    if used_mods:
        attrs["mods"] = "".join(used_mods)
    if used_prefix:
        # §3.3 / §6.2：使用类别前缀则**必须**声明 ns；纯序号（无前缀）方可省略
        ns_value = f"prefix:{''.join(sorted(used_prefix))}"
        attrs["ns"] = ns_value
        res.notes.append(
            f"使用类别前缀 {sorted(used_prefix)} → 按 §3.3/§6.2 自动声明 ns={ns_value}；"
            "注意 §16.2 自家示例用 R1/R2/R3 却未声明 ns（规范自相矛盾，见交付报告）")
    if any(u.fidelity for u, _ in pending):
        attrs["fid"] = "yes"
    res.header = build_header(attrs, version=version)

    # ── 步6：改写内容 ──
    res.body = _render_body(pending)

    # ── 步3（整篇）：§10.3 盈亏平衡判据 ──
    balance = res.stream_len < res.original_len
    if not balance:
        res.warnings.append(Diagnostic(
            "LX-W003",
            f"内容长度低于盈亏平衡点：符号串 {res.stream_len}（头部 {res.header_len} + "
            f"正文 {res.body_len}）≥ 原文 {res.original_len}（§10.3），压缩为负",
        ))
        if on_negative == "reject":
            raise EncodeError(
                "LX-W003",
                f"未通过 §10.3 盈亏平衡：符号串 {res.stream_len} ≥ 原文 {res.original_len} —— "
                f"**禁止编码**（提示：原始明细已记入 EncodeResult，请改用 on_negative='warn' 取回）",
            )
        if on_negative != "warn":
            raise EncodeError("LX-E010", f"on_negative 取值非法：{on_negative!r}")
        res.encoded = False
    else:
        res.encoded = True

    # ── 步8：校验（§8.2 全量）──
    if validate_result and res.encoded:
        rep = validate(res.document, lex, stream_path=None)
        res.report = rep
        res.errors.extend(rep.errors)
        res.warnings.extend(rep.warnings)
        if rep.errors:
            res.encoded = False
    return res


def _assert_required(e: Entry) -> None:
    """步5 断言必备列齐备（§5.2）—— 空值与失败必须可区分。"""
    missing = [name for name, value in (
        ("code", e.code), ("term", e.term), ("meaning", e.meaning),
        ("fidelity", e.fidelity), ("detail_path", e.detail_path),
    ) if not value]
    if missing:
        raise EncodeError(
            "LX-E010",
            f"{e.code or '(无代号)'}：必备列缺失 {missing}（§5.2；detail_path 缺失将使 V-03 失败）")


def _render_body(pending: Sequence[Tuple[Unit, Entry]]) -> str:
    """步6：渲染正文。`|` 并列同级项，单元末尾 `;;` 闭合（与 §16.2 一致）。"""
    if not pending:
        return ""
    pieces: List[str] = []
    for unit, entry in pending:
        seg = f"{unit.modality}@{entry.code}"
        if unit.fidelity:
            seg += unit.fidelity
        if unit.relation and pieces:
            seg = f"{unit.relation}{seg}"
        pieces.append(seg)
    return "|".join(pieces) + "\n;;"


def render_document(header: str, body: str) -> str:
    """拼装完整符号串（头部 + 正文），统一 LF 行尾（§3.2 L-2）。"""
    return f"{header}\n{body}\n"


# ─────────────────────────── 字典输出（§5.1） ───────────────────────────

def dictionary_text(lexicon: Lexicon, *, columns: Optional[Sequence[str]] = None) -> str:
    """把字典渲染为 TSV 文本（§5.1）：列名行 + 数据行，字段按 §5.1.1 转义。"""
    cols = list(columns or lexicon.columns or COLUMNS_CANONICAL)
    rows = ["\t".join(cols)]
    for e in lexicon.entries.values():
        raw = {
            "code": e.code, "term": e.term, "meaning": e.meaning,
            "fidelity": e.fidelity, "detail_path": e.detail_path,
            "match": e.match, "loss": e.loss, "rev": e.rev,
        }
        rows.append("\t".join(
            escape_field(raw.get(c, ""), is_match=(c == "match")) for c in cols))
    return "\n".join(rows) + "\n"


def save_dictionary(lexicon: Lexicon, path: "str | Path") -> None:
    """写字典文件（UTF-8、LF，§5.1）。"""
    Path(path).write_text(dictionary_text(lexicon), encoding="utf-8", newline="\n")
