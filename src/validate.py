"""validate.py — 词法/语法解析（§3 / §4）+ 一致性校验（§8.2 的 V-01~V-13）。

规范来源：`docs/05_引索符系统规范.md`
  §3.1 EBNF（**唯一语法来源**）      §3.2 词法要求 L-1~L-9
  §3.3 头部（含相对路径解析基准）   §3.4 字面量与转义
  §3.5 消歧规则 D-1~D-5             §4 符号表（20 符 6 组）
  §8.1 错误码                       §8.2 校验规则 V-01~V-13（V-12 已撤销）
  §8.3 最小可用校验集               §8.4 语法层 / 语义层职责分离

分层（§8.4，**不可互相代替**）：
  * 语法层入口 `parse_stream` / `validate_syntax` —— 头部格式、词法、结构、转义、消歧。
    仅凭此层通过**不构成**「内容合规」。
  * 语义层入口 `validate(text, lexicon)` —— 追加代号已定义 / dict 可读 / 路径可达。
  * 最小集入口 `validate_minimal(text, lexicon)` —— §8.3 的 V-01 V-02 V-08 V-09。

零第三方依赖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from lexicon import (
    EXTERNAL_URI_PREFIXES, Entry, Lexicon, compare_rev,
)

# re-export：调用方从任一模块取 Diagnostic 都得到同一个类
__all__ = [
    "Diagnostic", "ValidationError", "Header", "Stream", "Unit", "Term",
    "Reference", "KeyValue", "Literal", "Report", "parse_stream", "parse_header",
    "validate", "validate_syntax", "validate_minimal", "validate_file",
    "SIGIL_TABLE", "SIGIL_COUNT", "SIGILS_RELATION_TABLE", "ERROR_CODES",
    "WARNING_CODES", "EXTENSION_CODES",
    "SUPPORTED_VERSIONS", "MINIMAL_RULES", "CHECKED_RULES", "REVOKED_RULES",
]
from lexicon import Diagnostic  # noqa: E402  （Diagnostic 定义在最低层 lexicon.py）

# ── §4 符号表（20 符 6 组）──
# 注：`>>` 语义上属「结构」，语法上由 `relation` 承载（§4.1 注）——
# 因此它**同时**出现在组 A 与「解析用关系符表」中，但在 §4.7 符号数核对里**只计一次**。
SIGIL_TABLE: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("A 结构", ("|", ":", ";;", ">>")),
    ("B 引用", ("@", "^")),
    ("C 模态", ("!", "-", "+", "~", "?")),
    ("D 关系", (">", "<", "&", "||", "=")),
    ("E 保真", (".=", ".~", ".!")),
    ("F 转义", ("\\",)),
)
SIGILS_STRUCTURE: Tuple[str, ...] = SIGIL_TABLE[0][1]
SIGILS_REFERENCE: Tuple[str, ...] = SIGIL_TABLE[1][1]
SIGILS_MODALITY: Tuple[str, ...] = SIGIL_TABLE[2][1]
# 解析用关系符表：含 `>>`（否则「从属」无产生式、功能不可用 —— §4.1 / 06 F-8）
# 顺序即最长匹配优先级（§3.2 L-4）：多字符项在前。
SIGILS_RELATION: Tuple[str, ...] = (">>", "||", ">", "<", "&", "=")
SIGILS_RELATION_TABLE: Tuple[str, ...] = SIGIL_TABLE[3][1]   # 计入 20 符的那 5 项
SIGILS_FIDELITY: Tuple[str, ...] = SIGIL_TABLE[4][1]
SIGILS_ESCAPE: Tuple[str, ...] = SIGIL_TABLE[5][1]
SIGIL_COUNT = sum(len(s) for _, s in SIGIL_TABLE)   # 4+2+5+5+3+1 = 20（§4.7）

# §3.1 safe-char（`/` 属字面量字符，非符号 —— §4.4）
SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~-/"
)
# §3.1 code = upper, {upper|digit}, { "-", 1*(upper|digit) }  → 连字符后可接数字（F-2）
CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*$")
# §3.1 key2 = lower, {lower|digit|"-"}（L-8；防 F-4 `127.0.0.1:5432` 误判键值）
KEY2_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# §3.3 头部属性
HEADER_KEYS: Tuple[str, ...] = ("dict", "mods", "ns", "fid")
HEADER_MAX = 120

# §13 E-3 / §8.2 V-13：实现支持的版本
SUPPORTED_VERSIONS: Tuple[int, ...] = (1,)
CURRENT_SPEC_VERSION = "0.5"   # 文档版本；线格式版本见 SUPPORTED_VERSIONS

# ── §8.1 错误码（严格照 §8.1 含义）──
ERROR_CODES: Dict[str, str] = {
    "LX-E001": "缺少头部或头部格式非法",
    "LX-E002": "使用了字典中不存在的代号（悬空引用）",
    "LX-E003": "字典 `detail_path` 不可达 / dict 不可读",
    "LX-E004": "代号重复定义",
    "LX-E005": "符号串内出现非 ASCII（界定字面量内除外）或未转义的引索符",
    "LX-E006": "使用了未在 `mods` 声明的模态符",
    "LX-E007": "`fidelity=.!` 但 `loss` 列缺失",
    "LX-E008": "裸代号出现于符号串",
    "LX-E009": "字典字段含裸 Tab / 换行（未按 §5.1.1 转义）",
    "LX-E010": "字典列数与列名行不符",
    "LX-E011": "`^code` 缺少先行 `@code`（复用了一个从未展开的代号）",
    "LX-E012": "头部版本号不受支持（实现无法按该版本解析）",
    "LX-E013": "字典与符号串位于**同一常驻文件**（违 INV-1 / V-09）",
    "LX-E014": "同一代号的 `rev` 发生变化，疑似语义被改（违 INV-2 / V-04）",
}
# ── §8.1 警告码 ──
WARNING_CODES: Dict[str, str] = {
    "LX-W001": "头部声明了未使用的符号",
    "LX-W002": "字典中存在从未被引用的代号（死词）",
    "LX-W003": "内容长度低于盈亏平衡点，压缩为负",
    "LX-W004": "`term` 列存在但某行值为空（缺列属必填列缺失，报 LX-E010）",
    "LX-W005": "头部 `fid` 声明与正文保真度后缀的实际使用不一致",
}
# ── 实现扩展码 ──
# 曾用于 `V-09` / `V-04`（§8.1 当时未分配错误码）。
# 规范 v0.4 已正式分配 `LX-E013` / `LX-E014`（§8.1），故此处**清空** ——
# 保留空字典以表明「本实现无任何非规范码」。
EXTENSION_CODES: Dict[str, str] = {}

# §8.2 全部校验规则（V-12 已撤销，按编号不复用原则保留空位且**不实现**）
CHECKED_RULES: Tuple[str, ...] = (
    "V-01", "V-02", "V-03", "V-04", "V-05", "V-06", "V-07", "V-08",
    "V-09", "V-10", "V-11", "V-13",
)
REVOKED_RULES: Tuple[str, ...] = ("V-12",)   # §8.2 注：已撤销，禁止再分配
# §8.3 最小可用校验集
MINIMAL_RULES: Tuple[str, ...] = ("V-01", "V-02", "V-08", "V-09")


class ValidationError(Exception):
    """语法 / 校验失败。`code` 为 §8.1 错误码（或扩展码），`line` 为 1 起行号。"""

    def __init__(self, code: str, message: str, line: int = 0) -> None:
        self.code = code
        self.message = message
        self.line = line
        loc = f" 行{line}" if line else ""
        super().__init__(f"{code}{loc}: {message}")


# ─────────────────────────── 抽象语法（§3.1） ───────────────────────────

@dataclass(frozen=True)
class Reference:
    """引用原子（§3.1 `reference` / §4.2）：`@` 必展开，`^` 复用。"""

    sigil: str          # "@" 或 "^"
    code: str


@dataclass(frozen=True)
class KeyValue:
    """键值原子（§3.1 `keyvalue`）：`key2 : atom1`。"""

    key: str
    value: str
    quoted: bool = False


@dataclass(frozen=True)
class Literal:
    """字面量原子（§3.4）：`quoted=True` 为界定字面量，否则为裸字面量。"""

    value: str
    quoted: bool = False


@dataclass
class Term:
    """项（§3.1 `term = [modality], atom, [fidelity]`）。"""

    atom: object                # Reference | KeyValue | Literal
    modality: str = ""
    fidelity: str = ""

    @property
    def code(self) -> str:
        return self.atom.code if isinstance(self.atom, Reference) else ""


@dataclass
class Unit:
    """语义单元（§3.1 `unit = term, {relation, term}`）。

    `terminator` 记录源文中跟随本单元的分隔符：`|`（并列，后有同级单元）或 `;;`（闭合）。
    见 `_parse_body`：`|` 分隔同级单元，`;;` 闭合整个运行段。
    """

    terms: List[Term] = field(default_factory=list)
    relations: List[str] = field(default_factory=list)   # len == len(terms) - 1
    terminator: str = ";;"
    line: int = 0


@dataclass
class Header:
    """头部（§3.3）：`#LX<version> <attr> <attr> ...`，必须位于文档首行、≤120 字符。"""

    version: int
    attrs: Dict[str, str] = field(default_factory=dict)
    raw: str = ""
    line: int = 1

    @property
    def dict_path(self) -> str:
        return self.attrs.get("dict", "")

    @property
    def mods(self) -> str:
        return self.attrs.get("mods", "")

    @property
    def ns(self) -> str:
        return self.attrs.get("ns", "")

    @property
    def fid(self) -> Optional[str]:
        return self.attrs.get("fid")

    @property
    def length(self) -> int:
        return len(self.raw)

    def resolve_dict(self, base_dir: Optional[Path] = None) -> Optional[Path]:
        """按 §3.3 解析 `dict`：相对路径相对**声明它的文件所在目录**；URI 返回 `None`。"""
        p = self.dict_path
        if not p or p.startswith(EXTERNAL_URI_PREFIXES):
            return None
        target = Path(p)
        if target.is_absolute():
            return target
        root = Path(base_dir) if base_dir is not None else Path.cwd()
        return root / target


@dataclass
class Stream:
    """解析结果：头部 + 单元序列。"""

    header: Header
    units: List[Unit] = field(default_factory=list)

    def all_refs(self) -> List[Tuple[Term, Reference]]:
        """按出现顺序产出全部引用（term, reference）。"""
        return [(t, t.atom) for u in self.units for t in u.terms
                if isinstance(t.atom, Reference)]

    def used_codes(self) -> Set[str]:
        return {r.code for _, r in self.all_refs()}

    def used_modalities(self) -> Set[str]:
        return {t.modality for u in self.units for t in u.terms if t.modality}

    def used_fidelities(self) -> Set[str]:
        return {t.fidelity for u in self.units for t in u.terms if t.fidelity}


# ─────────────────────────── 词法 / 语法（§3） ───────────────────────────

def _is_ascii(s: str) -> bool:
    return all(ord(c) < 0x80 for c in s)


def _strip_quoted(s: str) -> str:
    """把界定字面量整体替换为空，用于「非 ASCII 只许出现在界定字面量内」检查（L-1）。"""
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', s)


def parse_header(line: str) -> Header:
    """解析头部首行（§3.3）。格式非法报 `LX-E001`，非 ASCII 报 `LX-E005`。

    `dict` 缺失时报 `LX-E001`：§8.4 指出「头部缺少 dict」语法上合法、
    须由语义层负责（V-01）—— 但 V-01 的判据本身就是「含 dict」，
    故本实现把该判定统一放在 V-01，解析器以 `header.dict_path == ""` 暴露事实。
    """
    if "\t" in line:
        raise ValidationError("LX-E001", "头部不得含 Tab（§3.1 attribute 以 SP 分隔）", 1)
    if not _is_ascii(line):
        raise ValidationError("LX-E005", "头部出现非 ASCII（L-1：符号串须为纯 ASCII）", 1)
    if not line.startswith("#LX"):
        raise ValidationError("LX-E001", "首行必须以 `#LX` 起始（§3.3 头部必须位于文档首行）", 1)
    if len(line) > HEADER_MAX:
        raise ValidationError(
            "LX-E001", f"头部 {len(line)} 字符 > {HEADER_MAX}（§3.3 头部禁止超过 120 字符）", 1)

    rest = line[3:]
    # §3.1 `header = "#LX" , version , { SP , attribute }`：版本号后**必须**是
    # 空格再接属性（或行尾）。故 `#LX1dict=…` 非法 —— 这里用 `(?: (.*))?$` 强制该空格，
    # 而不是 `(.*)`（后者会把 `1dict=…` 切成 version=1 + tail=`dict=…` 而误放行）。
    m = re.match(r"^(\d+)(?: (.*))?$", rest)
    if not m:
        raise ValidationError(
            "LX-E001",
            "头部版本号后必须为空格再接属性"
            "（§3.1 header = \"#LX\", version, { SP, attribute }）；"
            f"收到 {rest!r}",
            1)
    version = int(m.group(1))
    tail = m.group(2) or ""

    attrs: Dict[str, str] = {}
    for tok in tail.split():
        if "=" not in tok:
            raise ValidationError(
                "LX-E001", f"属性 {tok!r} 缺少 `=`（§3.1 attribute）", 1)
        key, _, value = tok.partition("=")
        if key not in HEADER_KEYS:
            raise ValidationError(
                "LX-E001", f"未知头部属性 {key!r}；允许 {HEADER_KEYS}（§3.3）", 1)
        if not value:
            raise ValidationError(
                "LX-E001", f"属性 {key} 的值为空（§3.1 value = 1*(printable-ascii-except-space)）", 1)
        if key in attrs:
            raise ValidationError("LX-E001", f"属性 {key} 重复声明（§3.3）", 1)
        if key == "fid" and value != "yes":
            raise ValidationError("LX-E001", f"fid 取值须为 `yes`（§3.3），实为 {value!r}", 1)
        if key == "mods":
            bad = sorted(set(value) - set(SIGILS_MODALITY))
            if bad:
                raise ValidationError(
                    "LX-E001",
                    f"mods 含非模态符 {bad}；§4.3 模态符集合为 {SIGILS_MODALITY}", 1)
        attrs[key] = value

    return Header(version=version, attrs=attrs, raw=line, line=1)


def parse_stream(text: str) -> Stream:
    """解析符号串（§3.1 EBNF）。**纯语法层**，不查字典、不读文件系统（§8.4）。

    实现要点（逐条对应 06 负例库）：
      * `body` **必须**允许换行（L-9 / F-1）
      * `code` 连字符后**允许数字**（`RL-3` 合法，§3.1 / F-2）
      * 含空格或引索符的字面量**必须**用 `"..."`（L-6 / F-3）
      * 键名**必须**以小写字母起始（L-8 / F-4）
      * `/` 属字面量字符，**不是**关系符（§4.4 / F-5）
      * 关系符**禁止连写**，两侧**必须**各有原子（L-7 / F-6）
      * `>>` **必须**作为 `relation` 参与解析（§4.1 / F-8）
      * 多字符符号**最长匹配**：`>>` 先于 `>`，`||` 先于 `|`（L-4 / F-9）
      * `\\` **禁止**出现在界定字面量之外；界定字面量内只认 `\\"` 与 `\\\\`（§3.4）
        该口径与 §3.1 `escape = "\\" , printable-ascii` 的宽泛写法存在冲突，见交付报告。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() == "":
        raise ValidationError("LX-E001", "文档为空或首行为空，缺少头部（§3.3）", 1)

    header = parse_header(lines[0].strip())
    stream = Stream(header=header)

    body_lines: List[Tuple[int, str]] = []
    for lineno, raw in enumerate(lines[1:], start=2):
        line = raw.strip()
        if line == "":
            continue                                    # L-9：空白行必须被忽略
        if not _is_ascii(_strip_quoted(line)):
            raise ValidationError(
                "LX-E005",
                "符号串内出现非 ASCII（界定字面量内部除外）（L-1）", lineno)
        body_lines.append((lineno, line))

    stream.units = _parse_body(body_lines)
    return stream


def _parse_body(body_lines: Sequence[Tuple[int, str]]) -> List[Unit]:
    """解析整个 body（§3.1 `body = { NL | unit , ( "|" | ";;" ) , { NL } }`）。

    **分组模型**（`|` 分隔同级单元，`;;` 闭合整段）：`|` 与 `;;` 都是 §3.1 的终止符，
    但 §16.2 自家示例把收尾的 `;;` 单独写在一行、前面三条单元全部以 `|` 结尾 ——
    按 EBNF 逐字读会得到「悬空 `;;` 无单元可终止」而**无法解析**（06 负例库 F-1 同类）。
    为使 §16 可解析，本实现采用一致的读法：
      * `|`   切出同级单元（后面必须还有内容）
      * `;;`  闭合从上一个 `;;` 以来的全部同级单元（允许单独成行）
      * 文档末尾未闭合 → 报错（不得静默容忍）
    该读法与 §16.2 字符数（29 头部 + 14 正文 + 2 `;;` = 45）一致。

    报告项：本处分歧已记入交付报告「发现的规范问题 ①」。
    """
    units: List[Unit] = []
    run: List[Unit] = []            # 当前运行段（待 `;;` 闭合）
    buf: List[str] = []             # 当前单元的文本
    buf_line = 0

    def flush(piece_line: int) -> None:
        """把当前缓冲作为一个**同级单元**收进运行段。"""
        nonlocal buf
        piece = "".join(buf).strip()
        buf = []
        if piece != "":
            run.append(_parse_unit(piece, piece_line, "|"))

    for lineno, line in body_lines:
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            if not buf:
                buf_line = lineno

            if ch == '"':                              # 界定字面量：整体作为一个词元
                m = re.compile(r'"(?:[^"\\]|\\.)*"').match(line, i)
                if m is None:
                    raise ValidationError(
                        "LX-E005", f"界定字面量未闭合（§3.4）：{line[i:]!r}", lineno)
                buf.append(m.group(0))
                i = m.end()
                continue

            if line.startswith(";;", i):
                flush(buf_line or lineno)
                if not run:
                    raise ValidationError(
                        "LX-E005",
                        "空的语义单元：`;;` 前无任何单元（§3.1 body 要求 `unit` 先于终止符）",
                        lineno)
                for k, u in enumerate(run):
                    u.terminator = ";;" if k == len(run) - 1 else "|"
                units.extend(run)
                run = []
                i += 2
                continue

            if line.startswith("||", i):                # L-4 最长匹配：`||` 是关系符
                buf.append("||")
                i += 2
                continue

            if ch == "|":
                before = "".join(buf).strip()
                if before == "":
                    raise ValidationError(
                        "LX-E005", f"空的并列项（分隔符 `|` 前无内容）：{line!r}", lineno)
                flush(buf_line or lineno)
                i += 1
                continue

            buf.append(ch)
            i += 1

        # L-9：正文可以跨行 —— 换行同样分隔**同级单元**（与 `|` 等价）。
        # §16.2 与既有向量 A-10（`@R1\n@R2\n;;`）都依赖此读法；
        # 若把跨行内容拼接成一个单元，`@R1@R2` 会被误当成一个代号而错报 LX-E005。
        flush(buf_line or lineno)

    if buf:
        raise ValidationError(
            "LX-E005",
            f"单元 {''.join(buf).strip()!r} 未以 `|` 或 `;;` 终止（§3.1 body）",
            buf_line or (body_lines[-1][0] if body_lines else 1))
    if run:
        raise ValidationError(
            "LX-E005",
            f"运行段未以 `;;` 闭合（§3.1 body；首个未闭合单元起于行 {run[0].line}）",
            run[0].line)
    return units


def _parse_unit(piece: str, lineno: int, terminator: str) -> Unit:
    """解析单元 `term {relation term}`（§3.1 unit）。"""
    unit = Unit(terminator=terminator, line=lineno)
    tokens = _tokenize(piece, lineno)

    expect_term = True
    for kind, value in tokens:
        if expect_term:
            if kind == "rel":
                raise ValidationError(
                    "LX-E005",
                    f"关系符 {value!r} 缺少左侧原子 —— 关系符禁止连写（L-7）",
                    lineno)
            unit.terms.append(_parse_term(value, lineno))
            expect_term = False
        else:
            if kind == "term":
                raise ValidationError(
                    "LX-E005",
                    f"原子 {value!r} 前缺少关系符（§3.1 unit = term, {relation, term}）",
                    lineno)
            unit.relations.append(value)
            expect_term = True

    if not unit.terms:
        raise ValidationError("LX-E005", "单元内无任何原子（§3.1 unit）", lineno)
    if expect_term:
        raise ValidationError(
            "LX-E005", "结尾是关系符，缺少右侧原子 —— 关系符禁止连写（L-7）", lineno)
    return unit


def _tokenize(piece: str, lineno: int) -> List[Tuple[str, str]]:
    """把 `term rel term ...` 切成 `(kind, text)` 序列。

    最长匹配（L-4）：多字符符号（`>>` `||`）先于单字符（`>`）。
    保真度后缀（`.=` `.~` `.!`）**先于**关系符 `=` 识别 —— 否则 `@R1.=` 会被
    误切成 `@R1.` + `=`（`.=` 含 `=`，属最长匹配的延伸情形）。
    """
    out: List[Tuple[str, str]] = []
    buf: List[str] = []
    in_quote = False
    i = 0
    while i < len(piece):
        ch = piece[i]
        if in_quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(piece):
                buf.append(piece[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quote = False
            i += 1
            continue
        if ch == '"':
            in_quote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "\\":
            raise ValidationError(
                "LX-E005",
                "引索符 `\\` 只允许出现在界定字面量内部（§3.4；路径等无需转义，"
                "如 `mem/rules/rl.md` 应裸写）",
                lineno)
        fidelity = next((f for f in SIGILS_FIDELITY if piece.startswith(f, i)), "")
        if fidelity:
            buf.append(fidelity)
            i += len(fidelity)
            continue
        rel = next((r for r in SIGILS_RELATION if piece.startswith(r, i)), "")
        if rel:
            text = "".join(buf).strip()
            if text:
                out.append(("term", text))
            buf = []
            out.append(("rel", rel))
            i += len(rel)
            continue
        buf.append(ch)
        i += 1

    if in_quote:
        raise ValidationError("LX-E005", "界定字面量未闭合（§3.4）", lineno)
    text = "".join(buf).strip()
    if text:
        out.append(("term", text))
    return out


def _parse_term(text: str, lineno: int) -> Term:
    """解析 `term = [modality], atom, [fidelity]`（§3.1）。"""
    body = text
    modality = ""
    fidelity = ""

    for sym in SIGILS_MODALITY:
        if body.startswith(sym):
            modality = sym
            body = body[1:]
            break
    for sym in SIGILS_FIDELITY:
        if body.endswith(sym):
            fidelity = sym
            body = body[: -len(sym)]
            break

    if body == "":
        raise ValidationError(
            "LX-E005", f"项 {text!r} 只有模态 / 保真度而无原子（§3.1 term）", lineno)
    if modality and body[0] in SIGILS_MODALITY:
        raise ValidationError(
            "LX-E005", f"项 {text!r} 含多个模态符（§3.1 term 只允许一个）", lineno)
    return Term(atom=_parse_atom(body, lineno), modality=modality, fidelity=fidelity)


def _parse_atom(text: str, lineno: int) -> object:
    """解析原子，并按 §3.5 消歧规则 D-1~D-5 判定类型。"""
    s = text.strip()
    if s == "":
        raise ValidationError("LX-E005", "原子为空", lineno)

    # D-4：以 `@` / `^` 起始 → 引用
    if s[0] in SIGILS_REFERENCE:
        code = s[1:]
        if not CODE_RE.match(code):
            raise ValidationError(
                "LX-E005",
                f"引用 {s!r} 的代号不合法（§3.1 code：大写字母起始；"
                "连字符后可接字母或数字、且不得结尾）",
                lineno)
        return Reference(sigil=s[0], code=code)

    # D-3：以 `"` 起始 → 界定字面量
    if s[0] == '"':
        if len(s) < 2 or s[-1] != '"':
            raise ValidationError("LX-E005", f"界定字面量未闭合：{s!r}（§3.4）", lineno)
        return Literal(value=_unescape_quoted(s[1:-1], lineno), quoted=True)

    # D-1：小写字母起始且含 `:` 且 `:` 前为合法 key2 → 键值
    if ":" in s:
        key, _, value = s.partition(":")
        if KEY2_RE.match(key):
            if value == "":
                raise ValidationError(
                    "LX-E005", f"键值 {s!r} 缺少值（§3.1 keyvalue = key2, \":\", atom1）", lineno)
            if value[0] == '"':
                if len(value) < 2 or value[-1] != '"':
                    raise ValidationError(
                        "LX-E005", f"键值 {s!r} 的界定值未闭合（§3.4）", lineno)
                return KeyValue(key=key, value=_unescape_quoted(value[1:-1], lineno),
                                quoted=True)
            _assert_bare_literal(value, lineno, f"键值 {s!r} 的值")
            return KeyValue(key=key, value=value, quoted=False)

        # D-2：以数字开头（且含 `:`）**不是**键值 → 字面量
        if key and key[0].isdigit():
            # 规范缺口：`:` 不在 safe-char 内，故此类文本只能写成界定字面量
            raise ValidationError(
                "LX-E005",
                f"{s!r} 不是键值（D-2：数字开头），但它含 `:`，而 `:` 不在 safe-char 内 ——"
                " 须写作界定字面量（如 \"127.0.0.1:5432\"）。"
                "见交付报告「发现的规范问题」③",
                lineno)

    # D-5：其他 → 裸字面量（L-6 限安全字符集）
    if s[0] in SIGILS_RELATION or s[0] == "|" or s[0] == ":":
        raise ValidationError(
            "LX-E005",
            f"{s!r} 以符号起始，疑为关系符 / 分隔符连写或原子缺失（L-7、§3.1 unit）",
            lineno)
    _assert_bare_literal(s, lineno, f"裸字面量 {s!r}")
    return Literal(value=s, quoted=False)


def _assert_bare_literal(value: str, lineno: int, what: str) -> None:
    """校验裸字面量仅含 §3.1 `safe-char`（L-6 / §3.4）。"""
    bad = sorted({c for c in value if c not in SAFE_CHARS})
    if bad:
        raise ValidationError(
            "LX-E005",
            f"{what} 含非法字符 {bad}；含空格或引索符时**必须**界定：\"...\"（L-6 / §3.4）",
            lineno)


def _unescape_quoted(inner: str, lineno: int) -> str:
    """还原界定字面量内部转义（§3.4：仅 `\\"` 与 `\\\\`）。

    未知转义（如 `\\/`）**必须**报错，不得静默吞掉 —— 否则 06 负例库
    「路径不得转义」一条会误判为通过。
    """
    out: List[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\":
            if i + 1 >= len(inner):
                raise ValidationError(
                    "LX-E005",
                    "界定字面量以孤立 `\\` 结尾；§3.4 仅定义 \\\" 与 \\\\", lineno)
            nxt = inner[i + 1]
            if nxt not in ('"', "\\"):
                raise ValidationError(
                    "LX-E005",
                    f"界定字面量内非法转义 \\{nxt}；§3.4 仅定义 \\\" 与 \\\\"
                    "（路径应裸写，无需转义）",
                    lineno)
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ─────────────────────────── 校验（§8.2） ───────────────────────────

@dataclass
class Report:
    """校验报告：错误与警告分开，`checked` 记录实际执行的规则（§8.4 两层都做才合规）。"""

    errors: List[Diagnostic] = field(default_factory=list)
    warnings: List[Diagnostic] = field(default_factory=list)
    checked: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    layer: str = "syntax+semantics"

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def error_codes(self) -> List[str]:
        return [d.code for d in self.errors]

    @property
    def warning_codes(self) -> List[str]:
        return [d.code for d in self.warnings]

    def format(self) -> str:
        lines = [f"校验层：{self.layer} ｜ {self.summary()}"]
        lines += [str(d) for d in self.errors]
        lines += [str(d) for d in self.warnings]
        lines += [f"[提示] {n}" for n in self.notes]
        return "\n".join(lines)

    def summary(self) -> str:
        return (f"错误 {len(self.errors)} ｜ 警告 {len(self.warnings)} "
                f"｜ 已执行规则 {len(self.checked)}")


def _diag(code: str, message: str, line: int = 0, context: str = "") -> Diagnostic:
    return Diagnostic(code=code, message=message, line=line, context=context)


def _dedup(diags: List[Diagnostic]) -> List[Diagnostic]:
    """按 (码, 行, 消息) 去重并保持顺序。

    同一事实可能被多条规则各报一次（例：`^R1` 早于 `@R1` 同时被 V-11 与 V-10 报出）。
    重复诊断会淹没真实信号、让计数虚高，故在返回报告前统一去重 ——
    去重只影响**重复项**，不会掩盖任何不同事实。
    """
    seen: set[Tuple[str, int, str]] = set()
    out: List[Diagnostic] = []
    for d in diags:
        key = (d.code, d.line, d.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


@dataclass
class _Ctx:
    """校验上下文（各规则共享；`lexicon=None` 表示语义信息不可得）。"""

    lexicon: Optional[Lexicon] = None
    base_dir: Optional[Path] = None
    stream_path: Optional[Path] = None
    old_lexicon: Optional[Lexicon] = None
    original: Optional[str] = None
    check_paths: bool = False
    expand: Optional[object] = None


# ── 各规则实现（签名统一：(stream, rep, ctx) -> None）──
#    每条只在**能判定**时给出诊断；无法判定时**必须**显式记录，不得静默通过（§8.4）。

def _rule_v01(stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-01（§3.3）：头部存在且 ≤120 字符，含 `dict`。"""
    h = stream.header
    if h.length > HEADER_MAX:
        rep.errors.append(_diag(
            "LX-E001", f"头部 {h.length} 字符 > {HEADER_MAX}（§3.3）", h.line))
    if not h.dict_path:
        # §8.4 明列：头部缺少 dict 语法层拦不住，须由语义层负责
        rep.errors.append(_diag(
            "LX-E001", "头部缺少 `dict`（§3.3；§8.4 列为语义层项）", h.line))


def _rule_v02(stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-02（INV-3）：所有 `@` / `^` 引用的代号在字典中有定义。"""
    if ctx.lexicon is None:
        rep.errors.append(_diag(
            "LX-E002", "未提供字典，无法校验引用可解析；拒绝静默通过（§8.3 最小集含 V-02）"))
        return
    for _, ref in stream.all_refs():
        if ref.code not in ctx.lexicon.entries:
            rep.errors.append(_diag(
                "LX-E002", f"代号 {ref.code!r} 不在字典中（悬空引用）"))


def _rule_v03(stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-03（INV-3）：所有 `detail_path` 可达，或标为外部 URI。

    需访问文件系统，故仅在 `check_paths=True` 时执行（否则本规则**不登记进 checked**，
    避免「跑了但没跑」的假象）。相对路径按 §3.3 相对于记录它的字典文件所在目录解析。
    """
    if ctx.lexicon is None:
        rep.errors.append(_diag("LX-E003", "未提供字典，无法校验 detail_path（V-03）"))
        return
    for code, e in ctx.lexicon.entries.items():
        if not e.detail_path:
            rep.errors.append(_diag(
                "LX-E003", f"{code} 的 detail_path 为空（§5.2 必填）", e.line))
            continue
        if e.external:
            continue                        # §5.2：外部 URI 豁免本地可达性
        target = ctx.lexicon.resolve_detail_path(code, ctx.base_dir)
        if target is None or not target.exists():
            rep.errors.append(_diag(
                "LX-E003",
                f"{code} 的 detail_path 不可达：{e.detail_path}（解析为 {target}）",
                e.line))


def _rule_v04(stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-04（INV-2）：代号无重复定义；若字典含 `rev` 列，比对历史 `rev` 检测语义变更。"""
    if ctx.lexicon is None:
        rep.notes.append("V-04 备注：未提供字典，重复定义检测改由 parse_lexicon 侧承担")
        return
    for d in ctx.lexicon.errors:
        if d.code == "LX-E004":
            rep.errors.append(d)
    if ctx.old_lexicon is not None:
        rep.errors.extend(compare_rev(ctx.old_lexicon, ctx.lexicon))
    if all(not e.rev for e in ctx.lexicon.entries.values()) and ctx.lexicon.entries:
        rep.notes.append(
            "V-04 备注：字典无 `rev` 列 → INV-2 必须由版本控制保证（§5.2）")


def _rule_v05(text: str, stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-05（L-1）：**符号串**为纯 ASCII；未转义引索符数为零。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    body = "\n".join(lines[1:]) if len(lines) > 1 else ""
    masked = _strip_quoted(body)
    if not _is_ascii(masked):
        bad = sorted({c for c in masked if ord(c) >= 0x80})
        rep.errors.append(_diag(
            "LX-E005",
            f"符号串内出现非 ASCII {bad}（界定字面量内除外）（L-1；字典与语言包不受此限）"))
    if "\\" in masked:
        rep.errors.append(_diag(
            "LX-E005", "存在未转义引索符 `\\`（界定字面量之外）（L-1 / §3.4）"))


def _rule_v06(stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-06（§3.3）：正文出现的模态符 ⊆ 头部 `mods`。

    §8.4 表把本项归为「语义层（需对照头部）」，但头部与本文件**同文档**，
    故本实现在语法层即可判定 —— 属**严格更强**，规则本身未被跳过。
    """
    declared = set(stream.header.mods)
    used = stream.used_modalities()
    undeclared = sorted(used - declared)
    if undeclared:
        rep.errors.append(_diag(
            "LX-E006",
            f"模态符 {undeclared} 未在头部 `mods` 声明；已声明：{sorted(declared) or '（无）'}（§3.3）",
            stream.header.line))
    unused = sorted(declared - used)
    if unused:
        rep.warnings.append(_diag(
            "LX-W001", f"头部声明了未使用的模态符 {unused}（§8.1）"))


def _rule_v07(stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-07（§5.3）：每条 `.!` 项在字典有 `loss`。"""
    if ctx.lexicon is None:
        rep.notes.append("V-07 备注：未提供字典，无法核对 `.!` 项的 loss")
        return
    for d in ctx.lexicon.errors:
        if d.code == "LX-E007":
            rep.errors.append(d)
    for _, ref in stream.all_refs():
        e = ctx.lexicon.get(ref.code)
        if e is not None and e.fidelity == ".!" and not e.loss:
            rep.errors.append(_diag(
                "LX-E007", f"{ref.code} 为 .! 但字典缺 `loss`（§5.2 / §5.3）", e.line))


def _rule_v08(stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-08（§4.2）：无裸代号。

    §8.4 明列：字面量本就是合法原子，形状无法判定「这其实是代号」——
    只有拿到字典才能发现。故本规则**依赖字典**，`lexicon=None` 时无法判定。
    判定口径：裸字面量（`quoted=False`）的值命中字典代号集合，即视为裸代号。
    """
    if ctx.lexicon is None:
        rep.notes.append("V-08 备注：未提供字典，裸代号（形状合法）无法判定")
        return
    for u in stream.units:
        for t in u.terms:
            atom = t.atom
            if isinstance(atom, Literal) and not atom.quoted and atom.value in ctx.lexicon.entries:
                rep.errors.append(_diag(
                    "LX-E008",
                    f"{atom.value!r} 是字典中的代号却未加引用符（裸代号，§4.2）", u.line))
            elif isinstance(atom, KeyValue) and not atom.quoted and atom.value in ctx.lexicon.entries:
                rep.errors.append(_diag(
                    "LX-E008",
                    f"键值 {atom.key}:{atom.value} 中的 {atom.value!r} 是字典代号却未加引用符"
                    "（裸代号，§4.2）", u.line))


def _rule_v09(text: str, stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-09（INV-1）：字典与符号串**不在同一常驻文件**。

    文件级检查，不依赖 AST —— 故在 `_run` 中于解析**之前**执行（见 `_run` 注释）。

    §8.1 已分配错误码 `LX-E013`（规范 v0.4）。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for i, line in enumerate(normalized.split("\n"), 1):
        cells = line.split("\t")
        if len(cells) >= 3 and cells[0] == "code":
            rep.errors.append(_diag(
                "LX-E013",
                "同一文件内出现字典列名行（`code` + Tab）→ 字典被内嵌于符号串所在文件"
                "（违 INV-1 / V-09；§8.1 未分配错误码）", i))
            break
    if ctx.stream_path is not None and ctx.lexicon is not None and ctx.lexicon.source:
        try:
            same = Path(ctx.stream_path).resolve() == Path(ctx.lexicon.source).resolve()
        except OSError:
            same = False
        if same:
            rep.errors.append(_diag(
                "LX-E013",
                f"字典（{ctx.lexicon.source}）与符号串（{ctx.stream_path}）是同一文件"
                "（违 INV-1 / V-09）"))


def _rule_v10(text: str, stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-10（§9.2）：编码结果**可展开** —— 这是本项目核心主张（机器可校验）。"""
    if ctx.lexicon is None:
        rep.errors.append(_diag(
            "LX-E002", "未提供字典，无法验证「编码结果可展开」（V-10 依赖 §9.1 步3）"))
        return
    checker = ctx.expand
    if checker is None:
        try:
            from decode import verify_expandable as checker  # 延迟导入，避免循环依赖
        except ImportError as exc:  # pragma: no cover - 同包内不应发生
            rep.errors.append(_diag(
                "LX-E002", f"无法加载展开器验证 V-10：{exc}（不静默通过）"))
            return
    rep.errors.extend(checker(text, ctx.lexicon))       # type: ignore[misc]


def _rule_v11(stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-11（§4.2）：每个 `^code` 必须存在**先行** `@code`（先展开、后复用）。"""
    seen: Set[str] = set()
    for _, ref in stream.all_refs():
        if ref.sigil == "@":
            seen.add(ref.code)
        elif ref.code not in seen:
            rep.errors.append(_diag(
                "LX-E011",
                f"^ {ref.code} 复用了一个从未展开的代号（须存在先行 @{ref.code}；"
                "§4.2 / §9.1 步4）"))


def _rule_v13(stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """V-13（§13 E-3）：头部版本号 ≤ 实现支持版本且语义兼容。"""
    v = stream.header.version
    if v not in SUPPORTED_VERSIONS:
        rep.errors.append(_diag(
            "LX-E012",
            f"头部版本 {v} 不受支持（实现支持 {SUPPORTED_VERSIONS}，规范 {CURRENT_SPEC_VERSION}）",
            stream.header.line))


def _rule_warnings(stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """§8.1 警告（无独立 V 编号）：LX-W002 死词 / LX-W003 盈亏 / LX-W005 fid 一致性。"""
    if ctx.lexicon is not None:
        dead = ctx.lexicon.dead_codes(stream.used_codes())
        if dead:
            rep.warnings.append(_diag(
                "LX-W002", f"字典中 {len(dead)} 个代号从未被引用（死词）：{dead}"))
    if ctx.original is not None:
        stream_len = len(text.replace("\r\n", "\n").rstrip("\n"))
        if not stream_len < len(ctx.original):
            rep.warnings.append(_diag(
                "LX-W003",
                f"内容长度低于盈亏平衡点：符号串 {stream_len} ≥ 原文 {len(ctx.original)}"
                "（§10.3）"))
    h = stream.header
    used_fid = stream.used_fidelities()
    if h.fid == "yes" and not used_fid:
        rep.warnings.append(_diag(
            "LX-W005", "头部声明 fid=yes 但正文未使用保真度后缀（§3.3）"))
    elif h.fid is None and used_fid:
        rep.warnings.append(_diag(
            "LX-W005",
            f"正文使用保真度后缀 {sorted(used_fid)} 但头部未声明 fid（§3.3；"
            "V-12 已撤销，该条件由本警告承担）"))


def _rule_lexicon_structure(stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """字典结构性错误（`LX-E009` / `LX-E010`）：§8.2 未分配 V 编号，单独登记。"""
    if ctx.lexicon is None:
        return
    for d in ctx.lexicon.errors:
        if d.code in ("LX-E009", "LX-E010"):
            rep.errors.append(d)


def _apply_rule(name: str, text: str, stream: Stream, rep: Report, ctx: _Ctx) -> None:
    """按规则名分派（`SKIP` 项不计入 `checked`）。"""
    if name == "V-03" and not ctx.check_paths:
        return
    rep.checked.append(name)
    {
        "V-01": lambda: _rule_v01(stream, rep, ctx),
        "V-02": lambda: _rule_v02(stream, rep, ctx),
        "V-03": lambda: _rule_v03(stream, rep, ctx),
        "V-04": lambda: _rule_v04(stream, rep, ctx),
        "V-05": lambda: _rule_v05(text, stream, rep, ctx),
        "V-06": lambda: _rule_v06(stream, rep, ctx),
        "V-07": lambda: _rule_v07(stream, rep, ctx),
        "V-08": lambda: _rule_v08(stream, rep, ctx),
        "V-09": lambda: _rule_v09(text, stream, rep, ctx),
        "V-10": lambda: _rule_v10(text, stream, rep, ctx),
        "V-11": lambda: _rule_v11(stream, rep, ctx),
        "V-13": lambda: _rule_v13(stream, rep, ctx),
        "WARN": lambda: _rule_warnings(stream, rep, ctx),
        "LEX": lambda: _rule_lexicon_structure(stream, rep, ctx),
    }[name]()


# 全量执行顺序（V-12 已撤销，不实现；编号不复用，§8.2 注）
_ALL_RULES: Tuple[str, ...] = (
    "V-01", "LEX", "V-02", "V-03", "V-04", "V-05", "V-06", "V-07",
    "V-08", "V-09", "V-10", "V-11", "V-13", "WARN",
)
# §8.3 最小集：覆盖最严重的失效 —— 语法层 V-01/V-09 + 语义层 V-02/V-08
_MINIMAL_RULE_SET: Tuple[str, ...] = MINIMAL_RULES


def _run(
    text: str,
    rules: Sequence[str],
    ctx: _Ctx,
    *,
    layer: str,
) -> Report:
    """公共驱动：先执行**不依赖 AST** 的文件级规则，再解析，再逐条执行其余规则。

    V-09（INV-1「字典与符号串不在同一文件」）是文件级判定，无需 AST ——
    因此它在解析**之前**执行：否则「文件里混了字典」会先以 `LX-E005` 中断，
    恰好掩盖掉它真正该报的 INV-1 违规。
    其余规则依赖 AST：语法解析失败时**显式停止**（附 note），
    不以「空报告」冒充合规（§8.4）。
    """
    rep = Report(layer=layer)
    if "V-09" in rules:
        rep.checked.append("V-09")
        _rule_v09(text, None, rep, ctx)          # type: ignore[arg-type]
    try:
        stream = parse_stream(text)
    except ValidationError as exc:
        rep.errors.append(_diag(exc.code, exc.message, exc.line))
        rep.notes.append(
            "语法层解析失败 → 依赖 AST 的语义层规则无法判定；"
            "此处显式停止，不以空报告冒充合规（§8.4）")
        rep.errors = _dedup(rep.errors)
        return rep

    for name in rules:
        if name == "V-09":
            continue                             # 已在上方执行
        _apply_rule(name, text, stream, rep, ctx)

    if "LEX" not in rules and ctx.lexicon is not None:
        for d in ctx.lexicon.errors:
            if d.code in ("LX-E009", "LX-E010"):
                rep.errors.append(d)

    rep.errors = _dedup(rep.errors)
    rep.warnings = _dedup(rep.warnings)
    return rep


def validate_syntax(text: str) -> Report:
    """**语法层**入口（§8.4）：只做词法 / 符号串结构 / 转义 / 消歧检查。

    执行 V-05 V-06 V-13（均可仅凭文档本身判定）与 V-09（文件级）。
    ⚠️ 本层通过**不构成**「内容合规」—— 仍可能引用不存在的代号（§8.4）。
    """
    return _run(text, ("V-05", "V-06", "V-13", "V-09"), _Ctx(),
                layer="语法层（syntax）")


def validate_minimal(
    text: str,
    lexicon: Optional[Lexicon] = None,
    **kw: object,
) -> Report:
    """**§8.3 最小可用校验集**入口：V-01 V-02 V-08 V-09。

    覆盖最严重的失效：无法解析字典 / 代号无定义 / 歧义 / 字典被一同截断。
    """
    return validate(text, lexicon, minimal=True, **kw)  # type: ignore[arg-type]


def validate(
    text: str,
    lexicon: Optional[Lexicon] = None,
    *,
    minimal: bool = False,
    check_paths: bool = False,
    base_dir: Optional[Path] = None,
    old_lexicon: Optional[Lexicon] = None,
    original: Optional[str] = None,
    stream_path: Optional[Path] = None,
    expand: Optional[object] = None,
) -> Report:
    """按 §8.2 执行校验（V-01~V-13；**V-12 已撤销，跳过且不实现**）。

    分层（§8.4，**两层不可互相代替**）：
      * 语法层：V-05 V-06 V-13（仅凭文档判定）
      * 语义层：V-01 V-02 V-03 V-04 V-07 V-08 V-09 V-10 V-11
        （需字典 / 文件系统 / 跨文件信息）

    参数：
      minimal     只跑 §8.3 最小集（V-01 V-02 V-08 V-09），不跑其余规则
      check_paths 是否执行 V-03（需访问文件系统；未开启时 V-03 **不登记**进 checked）
      base_dir    解析相对 `detail_path` 的基准目录（默认取字典文件所在目录，§3.3）
      old_lexicon 上一版字典，用于 V-04 的 `rev` 比对（检测语义被改，INV-2）
      original    改写前原文，用于 LX-W003 盈亏平衡判定（§10.3）
      stream_path 符号串文件路径，用于 V-09「同文件」判定
      expand      自定义展开器 `(text, lexicon) -> List[Diagnostic]`；默认用 `decode.verify_expandable`
    """
    ctx = _Ctx(
        lexicon=lexicon, base_dir=base_dir, stream_path=stream_path,
        old_lexicon=old_lexicon, original=original,
        check_paths=check_paths, expand=expand,
    )
    if minimal:
        rep = _run(text, _MINIMAL_RULE_SET, ctx,
                   layer="语法层 + 语义层（§8.3 最小集）")
    else:
        rep = _run(text, _ALL_RULES, ctx, layer="语法层 + 语义层（§8.2 全量）")
    return rep


def validate_file(
    stream_path: "str | Path",
    lexicon_path: Optional["str | Path"] = None,
    *,
    check_paths: bool = True,
    **kw: object,
) -> Report:
    """从文件校验：读符号串 → 按头部 `dict` 定位字典（§3.3 相对其所在目录）→ 校验。

    `lexicon_path` 省略时使用头部声明；两者都不存在时报 `LX-E001`。
    `base_dir` 默认为符号串所在目录（用于解析 `dict` 相对路径）。
    字典的 `detail_path` 按 §3.3 相对**字典文件**所在目录解析。
    """
    sp = Path(stream_path)
    if not sp.exists():
        rep = Report(layer="文件入口")
        rep.errors.append(_diag("LX-E001", f"符号串文件不存在：{sp}"))
        return rep
    text = sp.read_text(encoding="utf-8")

    lexicon: Optional[Lexicon] = None
    try:
        header = parse_header(text.replace("\r\n", "\n").split("\n")[0].strip())
    except ValidationError:
        return validate(text, None, stream_path=sp, **kw)  # type: ignore[arg-type]

    target = Path(lexicon_path) if lexicon_path is not None else header.resolve_dict(sp.parent)
    if target is not None and Path(target).exists():
        try:
            lexicon = _load_lexicon(target)
        except Exception as exc:                      # 明确外抛，不吞
            rep = Report(layer="文件入口")
            rep.errors.append(_diag(
                getattr(exc, "code", "LX-E003"),
                f"字典加载失败：{exc}",
                getattr(exc, "line", 0)))
            return validate(text, None, stream_path=sp, **kw)  # type: ignore[arg-type]

    return validate(
        text, lexicon, stream_path=sp,
        check_paths=check_paths,
        base_dir=(Path(lexicon.source).parent if lexicon and lexicon.source else sp.parent),
        **kw,  # type: ignore[arg-type]
    )


def _load_lexicon(path: Path) -> Lexicon:
    """延迟导入避免循环：从 lexicon.py 读字典（保留错误供上层报告）。"""
    import lexicon as _lex
    return _lex.load(path)
