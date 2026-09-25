"""lexicon.py — 字典层：TSV 读写、字段转义、列 / 代号 / 保真度校验。

规范来源：`docs/05_引索符系统规范.md`
  §5.1   格式（TSV / UTF-8 / 首行列名 / 列名禁止任何前缀）
  §5.1.1 字段转义（`\\t` `\\n` `\\r` `\\\\` `\\;`）与「先按 Tab 切列、再逐字段还原」顺序
  §5.2   列定义（必填 / 选填 / 条件必填）
  §5.3   丢失类别（`loss`）取值
  §3.3   `detail_path` 为相对路径时，须相对于「记录它的文件所在目录」解析
  §8.1   LX-E003 / LX-E004 / LX-E007 / LX-E009 / LX-E010、LX-W004

零第三方依赖（仅标准库）。本模块**不**解析符号串 —— 那是 `validate.py` 的职责（§8.4 分层）。
`Diagnostic` 定义在本层（依赖链最低层），供 §7 / §8 / §9 共用，避免循环导入。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ── §5.2 列定义 ──
# `term` 处的规范冲突：§5.2 把它列为「必填」，§8.1 却把「字典缺少 term 列」
# 定为**警告** `LX-W004`。本实现按 §8.1（机器可校验口径）处理为警告，
# 故它**不在**下面的「列存在性」必填集内；缺失时记 W004（见 parse_lexicon）。
COLUMNS_REQUIRED: Tuple[str, ...] = (
    "code", "meaning", "fidelity", "detail_path",
)
# 行级必备列（§7.1 步5「必备列齐备」）
ROW_REQUIRED: Tuple[str, ...] = (
    "code", "term", "meaning", "fidelity", "detail_path",
)
COLUMNS_OPTIONAL: Tuple[str, ...] = ("term", "match", "loss", "rev")
COLUMNS_KNOWN: Tuple[str, ...] = COLUMNS_REQUIRED + COLUMNS_OPTIONAL
# §5.1 列名行（规范示例列序；解析时不要求列序一致，但必须含全部必填列）
COLUMNS_CANONICAL: Tuple[str, ...] = (
    "code", "term", "meaning", "fidelity", "detail_path", "match", "loss",
)

# §4.5 保真度取值
FIDELITY_VALUES: Tuple[str, ...] = (".=", ".~", ".!")
# §5.3 丢失类别取值
LOSS_KINDS: Tuple[str, ...] = ("修饰", "举例", "推导", "细节", "样式")

# §3.1 code 产生式（与 validate.py 保持同一来源；此处于字典侧做独立复核）
CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*$")
# §3.3 / §5.2：视为「外部资源、无需本地可达」的 URI 前缀
EXTERNAL_URI_PREFIXES: Tuple[str, ...] = ("http://", "https://", "ftp://", "s3://")

# §5.1.1 转义表（写法 → 字面）
_ESCAPE_TO_CHAR: Dict[str, str] = {
    "t": "\t", "n": "\n", "r": "\r", "\\": "\\", ";": ";",
}
# 反斜杠本身、以及「必须转义」的字符（反斜杠必须在最前，避免二次转义）
_CHAR_TO_ESCAPE: Dict[str, str] = {
    "\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r",
}


@dataclass(frozen=True)
class Diagnostic:
    """一条诊断。`code` 取自 §8.1 错误码表（`LX-E*` 错误 / `LX-W*` 警告）。"""

    code: str
    message: str
    line: int = 0
    context: str = ""

    @property
    def is_error(self) -> bool:
        return self.code.startswith("LX-E")

    @property
    def is_warning(self) -> bool:
        return self.code.startswith("LX-W")

    def __str__(self) -> str:
        kind = "错误" if self.is_error else ("警告" if self.is_warning else "提示")
        loc = f" 行{self.line}" if self.line else ""
        ctx = f" [{self.context}]" if self.context else ""
        return f"[{kind}] {self.code}{loc}{ctx} {self.message}"


class LexiconError(Exception):
    """字典层不可继续解析时抛出（结构性问题）。`code` 为 §8.1 错误码。"""

    def __init__(self, code: str, message: str, line: int = 0, column: str = "") -> None:
        self.code = code
        self.message = message
        self.line = line
        self.column = column
        loc = f" 行{line}" if line else ""
        col = f" [{column}]" if column else ""
        super().__init__(f"{code}{loc}{col}: {message}")


# ─────────────────────────── 字段转义（§5.1.1） ───────────────────────────

def unescape_field(raw: str, *, report: Optional[List[str]] = None) -> str:
    """把字段写法还原为字面值（§5.1.1）。

    只认 §5.1.1 表中的五个转义（`\\t` `\\n` `\\r` `\\\\` `\\;`）。
    遇未知转义（如 `\\x`）时**不静默处理**：把原文 `\\x` 原样保留，
    并把说明追加到 `report`（调用方据此报 LX-E009）。
    """
    out: List[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt in _ESCAPE_TO_CHAR:
                out.append(_ESCAPE_TO_CHAR[nxt])
                i += 2
                continue
            if report is not None:
                report.append(f"未知转义 \\{nxt}（§5.1.1 仅定义 \\t \\n \\r \\\\ \\;）")
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def escape_field(value: str, *, is_match: bool = False) -> str:
    """把字面值写成合规字段（§5.1.1）。

    `is_match=True`（`match` 列）时额外把裸 `;` 转义为 `\\;`，
    因为该列以裸 `;` 作别名分隔符。

    实现在**单遍**中完成：先判别名分隔符，再做转义表映射。
    若先替换 `;` 再跑转义表，新引入的反斜杠会被二次转义，
    使 `\;` 变成 `\\;` —— 该缺陷由 `test_escaping_roundtrip` 捕获。
    """
    out: List[str] = []
    for ch in value:
        if ch == ";" and is_match:
            out.append("\\;")
            continue
        out.append(_CHAR_TO_ESCAPE.get(ch, ch))
    return "".join(out)


def split_aliases(raw_match: str) -> Tuple[str, ...]:
    """按**未转义**的 `;` 切分 `match` 列并逐段还原（§5.1.1 / §5.2）。

    切分必须在还原**之前**做 —— 否则 `\\;` 产生的字面 `;` 会被误当分隔符。
    """
    pieces: List[str] = []
    buf: List[str] = []
    i = 0
    while i < len(raw_match):
        ch = raw_match[i]
        if ch == "\\" and i + 1 < len(raw_match):
            buf.append(raw_match[i:i + 2])
            i += 2
            continue
        if ch == ";":
            pieces.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    pieces.append("".join(buf))
    return tuple(v for v in (unescape_field(p).strip() for p in pieces) if v)


# ─────────────────────────── 数据模型（§5.2） ───────────────────────────

@dataclass(frozen=True)
class Entry:
    """一条字典项（§0.3：代号 → 含义 / 保真度 / 详情路径 的映射）。"""

    code: str
    term: str
    meaning: str
    fidelity: str
    detail_path: str
    match: str = ""       # §5.2 `match` 列原文（未还原；别名见 aliases）
    loss: str = ""        # §5.2/§5.3：fidelity=.! 时必填
    rev: str = ""         # §5.2 可选：含义指纹（V-04 检测语义变更）
    line: int = 0         # 记录该行的行号（便于回报定位）

    @property
    def aliases(self) -> Tuple[str, ...]:
        """§5.2 `match` 列：供编码器匹配源文本的别名集（`;` 分隔）。"""
        return split_aliases(self.match)

    @property
    def lossy(self) -> bool:
        """§4.5：`.!` 为有损项，**禁止**作为最终依据。"""
        return self.fidelity == ".!"

    @property
    def external(self) -> bool:
        """§5.2：`detail_path` 为外部 URI 时无需本地可达（V-03 豁免）。"""
        return self.detail_path.startswith(EXTERNAL_URI_PREFIXES)


@dataclass
class Lexicon:
    """字典：代号 → Entry，附列信息与诊断（错误 / 警告分开，§8.4 两层都做）。"""

    entries: Dict[str, Entry] = field(default_factory=dict)
    columns: List[str] = field(default_factory=list)
    errors: List[Diagnostic] = field(default_factory=list)
    warnings: List[Diagnostic] = field(default_factory=list)
    source: str = ""

    def __contains__(self, code: str) -> bool:
        return code in self.entries

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def ok(self) -> bool:
        """字典是否可用：存在结构性行错误即不可用（不静默降级）。"""
        return not self.errors

    def get(self, code: str) -> Optional[Entry]:
        return self.entries.get(code)

    def codes(self) -> List[str]:
        return list(self.entries)

    def dead_codes(self, referenced: Iterable[str]) -> List[str]:
        """§8.1 `LX-W002`：字典中存在、但从未被引用的代号（死词）。"""
        used = set(referenced)
        return [c for c in self.entries if c not in used]

    def detail_path_for(self, code: str) -> str:
        e = self.entries.get(code)
        return e.detail_path if e else ""

    def resolve_detail_path(self, code: str, base_dir: Optional[Path] = None) -> Optional[Path]:
        """按 §3.3 解析 `detail_path`：相对路径相对**记录它的文件所在目录**。

        外部 URI 返回 `None`（§5.2：豁免本地可达性检查）。
        """
        e = self.entries.get(code)
        if e is None or e.external:
            return None
        p = Path(e.detail_path)
        if p.is_absolute():
            return p
        root = Path(base_dir) if base_dir is not None else (
            Path(self.source).parent if self.source else Path.cwd())
        return root / p


# ─────────────────────────── 解析（§5.1 / §5.1.1 / §5.2） ───────────────────────────

def _err(code: str, message: str, line: int = 0, context: str = "") -> Diagnostic:
    return Diagnostic(code=code, message=message, line=line, context=context)


def parse_lexicon(
    text: str,
    *,
    source: str = "",
    strict: bool = False,
) -> Lexicon:
    """解析 TSV 字典文本（§5.1）。

    分层（§8.4）：本函数只做**字典侧**语法/结构校验，不校验符号串。

    结构性失败（空文件 / 首行缺必填列 / 列名含前缀 / 非 UTF-8）→ 抛 `LexiconError`。
    行级失败（§8.1）→ 收集进 `Lexicon.errors`，并把该行**排除**出 `entries`
    （避免坏行参与查表），因此「空字典」与「失败字典」可由 `ok` 区分：
      * 空字典（有列名行、零数据行）→ `ok=True`, `len()==0`
      * 失败字典 → `ok=False`, `errors` 非空

    行级错误码：
      * `LX-E009` 列数多于列名行（字段内裸 Tab）／字段内含裸 CR／未知转义
      * `LX-E010` 列数少于列名行、fidelity 取值非法、代号为空
      * `LX-E004` 代号重复定义（后者不入表，先者保留）
      * `LX-E007` `fidelity=.!` 但 `loss` 为空（§5.2 / §5.3）
      * `LX-W004` 缺 `term` 列（§8.1 定为警告；注意与 §5.2「必填」冲突，见报告）

    `strict=True` 时任一错误立即抛 `LexiconError`（便于负例测试逐条断言码）。
    """
    diags: List[Diagnostic] = []
    warns: List[Diagnostic] = []
    lex = Lexicon(source=source)

    if not text.strip():
        raise LexiconError("LX-E010", "字典为空：无列名行（§5.1 首行必须为列名行）")

    raw_lines = text.replace("\r\n", "\n").split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()

    header_cells = raw_lines[0].split("\t")
    columns = [c.strip() for c in header_cells]
    for c in columns:
        if not c or c[0] in "#-*`":
            raise LexiconError(
                "LX-E010",
                f"列名 {c!r} 为空或含前缀；列名必须是字面列名（§5.1 / 06 F-12）",
                1,
            )
    unknown = [c for c in columns if c not in COLUMNS_KNOWN]
    if unknown:
        raise LexiconError("LX-E010", f"未知列 {unknown}；允许列见 §5.2", 1)
    if len(set(columns)) != len(columns):
        raise LexiconError("LX-E010", f"列名重复：{columns}（§5.1）", 1)
    missing = [c for c in COLUMNS_REQUIRED if c not in columns]
    if missing:
        raise LexiconError(
            "LX-E010", f"缺少必填列 {missing}（§5.2 必填列）", 1)

    lex.columns = columns
    has_match = "match" in columns
    has_loss = "loss" in columns

    # §8.1 LX-W004：缺 `term` 列（上引规范冲突；按 §8.1 定为警告而非错误）
    if "term" not in columns:
        warns.append(_err(
            "LX-W004",
            "字典缺少 `term` 列，人类无法审阅（§5.2 把它列为必填，§8.1 却定为警告 —— 冲突）",
            1,
        ))

    for lineno, raw in enumerate(raw_lines[1:], start=2):
        if raw.strip() == "":
            continue                      # 允许空行（不视为数据行）
        if raw.startswith("\ufeff"):
            raise LexiconError("LX-E010", "字典含 BOM；须为无 BOM 的 UTF-8（§5.1）", lineno)

        cells = raw.split("\t")          # §5.1.1：必须先按 Tab 切列
        if len(cells) > len(columns):
            diag = _err(
                "LX-E009",
                f"列数 {len(cells)} > 列名行 {len(columns)}：字段内裸 Tab 必须先写成 \\t（§5.1.1）",
                lineno,
            )
            diags.append(diag)
            if strict:
                raise LexiconError(diag.code, diag.message, lineno)
            continue
        if len(cells) < len(columns):
            diag = _err(
                "LX-E010",
                f"列数 {len(cells)} < 列名行 {len(columns)}；缺列须以空字段占位（§5.1）。"
                f"若字段内含裸 LF，行结构已被破坏（§5.1.1 要求先写 \\n）",
                lineno,
            )
            diags.append(diag)
            if strict:
                raise LexiconError(diag.code, diag.message, lineno)
            continue

        row: Dict[str, str] = {}
        row_ok = True
        for col, cell in zip(columns, cells):
            if "\r" in cell:
                diag = _err(
                    "LX-E009", f"字段 [{col}] 含裸 CR，必须写成 \\r（§5.1.1）", lineno)
                diags.append(diag)
                if strict:
                    raise LexiconError(diag.code, diag.message, lineno)
                row_ok = False
            undeclared: List[str] = []
            if col == "match":
                # `match` 列的 `;` 是别名分隔符，由 split_aliases 处理；此处只还原其余转义
                row[col] = cell
                unescape_field(cell, report=undeclared)
            else:
                row[col] = unescape_field(cell, report=undeclared)
            if undeclared:
                diag = _err(
                    "LX-E009",
                    f"字段 [{col}] " + "；".join(undeclared),
                    lineno,
                )
                diags.append(diag)
                if strict:
                    raise LexiconError(diag.code, diag.message, lineno)
                row_ok = False
        if not row_ok:
            continue

        code = row["code"]
        if not code:
            diag = _err("LX-E010", "`code` 列为空（§5.2 必填）", lineno, "code")
            diags.append(diag)
            if strict:
                raise LexiconError(diag.code, diag.message, lineno, "code")
            continue
        if not CODE_RE.match(code):
            diag = _err(
                "LX-E010",
                f"代号 {code!r} 不符合 §3.1 `code` 产生式（§6.1 语法唯一来源为 §3.1）",
                lineno, "code",
            )
            diags.append(diag)
            if strict:
                raise LexiconError(diag.code, diag.message, lineno, "code")
            continue
        if code in lex.entries:
            diag = _err(
                "LX-E004", f"代号 {code!r} 重复定义（§8.1；INV-2 要求全局唯一）",
                lineno, "code",
            )
            diags.append(diag)
            if strict:
                raise LexiconError(diag.code, diag.message, lineno, "code")
            continue

        fidelity = row["fidelity"]
        if fidelity not in FIDELITY_VALUES:
            diag = _err(
                "LX-E010",
                f"fidelity={fidelity!r} 非法；允许 {FIDELITY_VALUES}（§5.2）",
                lineno, "fidelity",
            )
            diags.append(diag)
            if strict:
                raise LexiconError(diag.code, diag.message, lineno, "fidelity")
            continue

        loss = row.get("loss", "")
        if fidelity == ".!" and not loss:
            diag = _err(
                "LX-E007",
                "fidelity=.! 但 loss 列缺失（§5.2 条件必填 / §5.3 须注明丢失类别）",
                lineno, "loss",
            )
            diags.append(diag)
            if strict:
                raise LexiconError(diag.code, diag.message, lineno, "loss")
            continue

        detail_path = row["detail_path"]
        if not detail_path:
            diag = _err("LX-E010", "`detail_path` 为空（§5.2 必填）", lineno, "detail_path")
            diags.append(diag)
            if strict:
                raise LexiconError(diag.code, diag.message, lineno, "detail_path")
            continue

        lex.entries[code] = Entry(
            code=code,
            term=row.get("term", ""),
            meaning=row["meaning"],
            fidelity=fidelity,
            detail_path=detail_path,
            match=row.get("match", "") if has_match else "",
            loss=loss if has_loss else "",
            rev=row.get("rev", ""),
            line=lineno,
        )

    lex.errors = diags
    lex.warnings = warns
    return lex


def load(path: "str | Path", *, strict: bool = False) -> Lexicon:
    """读取字典文件（UTF-8，§5.1）。文件不存在报 `LX-E003`（不返回空字典）。"""
    p = Path(path)
    if not p.exists():
        raise LexiconError("LX-E003", f"字典文件不存在或不可读：{p}", 0)
    if p.is_dir():
        raise LexiconError("LX-E003", f"字典路径是目录：{p}", 0)
    data = p.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LexiconError("LX-E010", f"字典不是合法 UTF-8（§5.1）：{exc}", 0) from exc
    return parse_lexicon(text, source=str(p), strict=strict)


# ─────────────────────────── 写回（§5.1） ───────────────────────────

def dump(lexicon: Lexicon) -> str:
    """把字典写回 TSV 文本：列序沿用 `columns`，字段按 §5.1.1 转义。"""
    cols = list(lexicon.columns)
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


def save(lexicon: Lexicon, path: "str | Path") -> None:
    """写入字典文件（UTF-8、LF，§5.1）。"""
    Path(path).write_text(dump(lexicon), encoding="utf-8", newline="\n")


def compare_rev(old: Lexicon, new: Lexicon) -> List[Diagnostic]:
    """§8.2 `V-04`：若字典含 `rev` 列，比对历史 `rev` 以检测「代号语义被改」（INV-2）。

    规范缺口：§8.2 要求此检测，但 §8.1 **未分配错误码**。
    本实现报 `LX-E014`（扩展码，见 `validate.EXTENSION_CODES`），并在报告中标注待规范补齐。
    """
    out: List[Diagnostic] = []
    for code, e_new in new.entries.items():
        e_old = old.entries.get(code)
        if e_old is None or not e_old.rev or not e_new.rev:
            continue
        if e_old.rev != e_new.rev:
            out.append(Diagnostic(
                "LX-E014",
                f"代号 {code} 的 rev 由 {e_old.rev!r} 变为 {e_new.rev!r}："
                f"疑似语义被改（违 INV-2；§8.2 V-04，规范未分配错误码）",
                e_new.line, "rev",
            ))
    return out
