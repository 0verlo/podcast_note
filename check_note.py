#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_note.py — 按 WORKFLOW.md 核对笔记：格式、切分、覆盖、压缩比、锚点。

面向模型运行：每条问题都带一句 `→` 修复指令，说明这一条该怎么处置。

用法:
    python3 check_note.py <笔记.md> [<笔记.md> ...]
    python3 check_note.py <笔记.md> --stage ledger    # 细切建账后的账本审计
    python3 check_note.py <笔记.md> <原文.txt>        # 显式指定原文
    python3 check_note.py <笔记.md> --json            # 机器可读
    python3 check_note.py --baseline <好笔记.md ...>  # 回算指标基底，供人工修订常数
    python3 check_note.py --rebreak <raw.txt>         # 校验 raw 的改行编辑（对 git 基线）
    python3 check_note.py --rebreak <新版.txt> <原版.txt>

原文缺省时在 <笔记目录>/raw/ 下自动匹配（同名 → 去 _vN 后缀同名 → 首段编号唯一匹配）。

两个阶段
--------
- `--stage ledger`：细切建账完成、答案未写时跑。查格式、切分、覆盖，以及深压缩 Q
  是否已申报锚点；不核算压缩比，不要求答案正文。
- 默认（full）：写作完成后跑。在 ledger 的基础上加压缩执行与锚点兑现。

五项检查
--------
1. 格式   账本行的语法。每条 `### **Q**:` 之下必须紧跟声明行，合法写法只有一种：

              *行 8–9 · 压缩至约 70%*

          只有三个数字可变，其余逐字符固定——连接号是 – (U+2013)，间隔号是 · (U+00B7)，
          空格位置照抄。多区间（`行 15–17、23–24`）、单行（`行 238`）、半角连字符 `-`
          一律报 ERROR。行范围还须正序、落在原文行数内；full 阶段答案不得为空。
          锚点行（深压缩 Q 必须，其余可选）紧跟声明行的下一行：

              *锚点：路易斯·萨斯、1936年、《家庭情结》*

          条目以 、 分隔，按笔记正文中将出现的写法书写。
2. 切分   单 Q 覆盖有效字数超过 Q_CAP → ERROR（压缩判断在这个尺寸上不可靠，拆条）；
          全文有效字 ÷ Q 数落在 DENSITY_BAND 之外 → WARN（切得过碎或漏切）。
3. 覆盖   原文每一行要么被某条 Q 的行范围覆盖，要么在头部"已舍弃内容"行登记。两样都
          没有 = 静默丢弃，报 ERROR。纯噪音行、实质字数 ≤30 的碎片自动豁免。
          头部登记的行号只认 `行 X` 与 `行 X–Y`，每个区间自带 `行` 前缀。
4. 压缩比 实际压缩比 = 答案字数 ÷ 该行范围剔除噪音后的原文字数。与声明值的偏差非对称
          处置：负偏（压得比声明狠，细节丢失方向）超 DEV_NEG → ERROR 打回；正偏超
          DEV_POS → WARN，单条不打回，总量由压缩带把关；声明 ≥HI_TIER 的高保留档负偏
          ≥HI_TIER_NEG → WARN。全文实际压缩比落在 TOTAL_BAND 之外 → ERROR，打回复评
          预算分配。
5. 锚点   声明 <ANCHOR_TIER 的深压缩 Q 必须申报锚点（ANCHOR_MIN–ANCHOR_MAX 条）；
          full 阶段逐条核对申报的锚点是否出现在对应答案中，缺失 → ERROR。

改行校验（--rebreak）
--------------------
细切时话题断在 raw 一行中间的，允许直接在 raw 的断点处插入换行。改行只许动换行，
不许增删改任何其他字符——`--rebreak` 比对当前 raw 与基线版本（缺省取 git HEAD 已提交
版本，也可显式传入原版文件），两者去除全部空白后必须逐字节相同，否则 FAIL 并指出
第一处分歧。行数减少（有行被合并）时报 WARN：原有行号整体前移，须重核对已写的行范围。

计字口径
--------
- 噪音行 = 转写工具插入的讲话人标号 / 时间戳行（`2` / `2号讲话人00:00:12` / `发言人2 02:31:23`），
  仅据行首形态识别，从原文字数中剔除。应和、口水话等语义噪音不由脚本判断，一律保留交给模型。
- 一律按非空白字符计。答案不计入：`>` 开头的行（EX 区块等用户手补内容）、`概念：` 行、
  声明行与锚点行、markdown 记号（`**`、列表符号、`[[链接]]`）。

指标基底
--------
文件头部常数区的阈值取自已提交笔记的实测分布。语料增长后用 `--baseline` 对全部已提交
好笔记回算，人工确认后修订常数与 WORKFLOW 中的对应描述，不自动更新。

退出码: 0 = PASS（可含 WARN）；1 = 存在 ERROR；2 = 用法或文件错误。
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from itertools import accumulate, pairwise
from pathlib import Path

# ── 指标基底（取自已提交笔记的实测分布，--baseline 回算后人工修订） ──────
TOTAL_BAND = (50, 70)  # 全文实际压缩比带（%）：低于 → 整体取舍过狠；高于 → 接近转述
DENSITY_BAND = (600, 900)   # 全文有效字 ÷ Q 数
Q_CAP = 1500           # 单 Q 覆盖有效字上限，超出即拆
DEV_NEG = 15           # 负偏差（压得比声明狠）超此值 → 打回
DEV_POS = 15           # 正偏差（写得比声明多）超此值 → WARN，总量由 TOTAL_BAND 把关
ANCHOR_TIER = 65       # 声明低于此值的 Q 属深压缩，必须申报锚点
ANCHOR_MIN, ANCHOR_MAX = 3, 8

# ── 其余阈值 ────────────────────────────────────────────────────────────
HI_TIER = 65           # 声明 ≥65% 即高保留档，等于承诺接近全文保留
HI_TIER_NEG = 10       # 高保留档负偏差达此值 → WARN
GAP_BUDGET = 30        # 未覆盖区间实质字数 ≤ 此值 → 自动豁免
OVERLAP_OK = 1         # 相邻 Q 允许共享的边界行数（原文一行是长段落，话题常在行中转折）
SYS_MEAN_DEV = -4      # 全文平均偏差低于此值，且多数条目偏狠 → 系统性偏压 WARN
SYS_NEG_FRAC = 0.75
SYS_MIN_Q = 10         # 条目太少时不判系统性倾向
SYS_NOTABLE = 3        # |偏差| 达此值才算"有倾向"，据此统计负偏占比

CHECKS = ('格式', '切分', '覆盖', '压缩比', '锚点')

# ── 词法 ────────────────────────────────────────────────────────────────
DASH = r'[–—\-~]'
Q_RE = re.compile(r'^###\s*\*\*Q\*\*\s*[:：]\s*(.*)$')

# 声明行只有一种合法写法：`*行 8–9 · 压缩至约 70%*`，只有三个数字可变。
META_RE = re.compile(r'^\*行 (\d+)–(\d+) · 压缩至约 (\d+)%\*$')
# 长得像声明行的：用来认出位置不对或写坏了的声明行，不参与解析。
META_SHAPE_RE = re.compile(r'^\*行.*%\*$')
META_SPEC = '*行 X–Y · 压缩至约 Z%*'

# 锚点行：紧跟声明行的下一行，条目以 、 分隔。
ANCHOR_RE = re.compile(r'^\*锚点[：:]\s*(.*?)\s*\*$')
ANCHOR_SHAPE_RE = re.compile(r'^\*锚点')
ANCHOR_SPEC = '*锚点：条目1、条目2、条目3*'

HEADING_RE = re.compile(r'^##\s+')
CONCEPT_RE = re.compile(r'^概念\s*[:：]')
RULE_RE = re.compile(r'^\s*([-*_])\1{2,}\s*$')

# 噪音行：转写插入的讲话人标号 / 时间戳，本身不含讲解内容。
NOISE_RE = re.compile(r'^\s*\d+\s*$'          # 单独一行的讲话人编号，如 `2`
                      r'|^\s*\d+号讲话人'      # `2号讲话人00:00:12`
                      r'|^\s*发言人\s*\d+')    # `发言人2   02:31:23`

# 头部"已舍弃内容"行里的行号：每个区间都要自带 `行` 前缀，写成 `行 1–7`、`行 427`。
DISCARD_RE = re.compile(rf'行\s*(\d+)(?:\s*{DASH}\s*(\d+))?')
# `行 250–261、427` 这种省略写法不予支持：427 到底是行号还是别的数，脚本无从判断。
DISCARD_ABBR_RE = re.compile(
    rf'行\s*\d+(?:\s*{DASH}\s*\d+)?((?:\s*[、,，]\s*\d+(?:\s*{DASH}\s*\d+)?)+)')


def nchars(s):
    """非空白字符数。全文计字一律走这个口径。"""
    return len(re.sub(r'\s', '', s))


def clean_text(line):
    """剔除 markdown 记号后的正文文本。答案计字与锚点匹配共用一个口径。

    记号本身不是内容，`**术语**` 与 `术语` 应当算同样的字数，否则加粗越多压缩比越虚高。
    """
    if RULE_RE.match(line):
        return ''
    s = re.sub(r'^\s*(?:[-+*]|\d+[.)])\s+', '', line)     # 列表符号
    s = re.sub(r'\[\[[^\]|]*\|([^\]]*)\]\]', r'\1', s)    # [[路径|别名]] → 别名
    s = re.sub(r'\[\[([^\]]*)\]\]', r'\1', s)             # [[概念]] → 概念
    s = re.sub(r'[*_`~#]', '', s)                         # 强调 / 代码 / 标题记号
    return s


def answer_chars(line):
    return nchars(clean_text(line))


def norm_match(s):
    """锚点匹配的归一化：小写、内部空白折叠。"""
    return re.sub(r'\s+', ' ', s).strip().lower()


# ── 数据模型 ────────────────────────────────────────────────────────────
@dataclass
class Issue:
    """一条待处置的问题。fix 是给模型的修复指令，报告里以 `→` 打印。"""
    level: str            # ERROR | WARN
    check: str            # CHECKS 之一
    msg: str
    fix: str = ''
    line: int = 0         # 笔记行号；0 表示不指向具体行


@dataclass
class Entry:
    """一条 Q 及其账本记录（声明行 + 可选的锚点行）。

    lo/hi/decl 为 None 表示这条 Q 不参与核算——声明行不合法，或行范围经校验后被判废。
    判废的写法就是把 lo/hi 清成 None，所以"能不能算"只由 lo 一个字段说了算。
    anchors 为 None 表示未申报锚点行。
    """
    q: str
    note_line: int
    lo: int = None
    hi: int = None
    decl: int = None
    ans: int = 0
    anchors: list = None
    body: list = field(default_factory=list)   # 清洗后的正文行，供锚点匹配

    @property
    def label(self):
        return self.q[:26]

    @property
    def usable(self):
        return self.lo is not None


class Source:
    """原文，附带噪音行标记与前缀和，供 O(1) 查询任意行区间的有效字数。"""

    def __init__(self, path):
        self.path = path
        self.lines = path.read_text(encoding='utf-8').splitlines()
        self.n = len(self.lines)
        self.noise = {i for i, l in enumerate(self.lines, 1) if NOISE_RE.match(l)}
        raw = [nchars(l) for l in self.lines]
        self._raw = [0] + list(accumulate(raw))
        self._noi = [0] + list(accumulate(
            c if i in self.noise else 0 for i, c in enumerate(raw, 1)))
        self.raw_chars = self._raw[self.n]       # 全文非空白字
        self.noise_chars = self._noi[self.n]     # 其中噪音行占掉的字

    def chars(self, lo=1, hi=None):
        """行区间 [lo, hi] 剔除噪音行后的有效字数。

        压缩比的分母、覆盖审计的"实质字数"、切分的尺寸都走这一个口径。
        """
        hi = self.n if hi is None else hi
        return ((self._raw[hi] - self._raw[lo - 1])
                - (self._noi[hi] - self._noi[lo - 1]))


@dataclass
class Note:
    path: Path
    entries: list
    discards: list                    # [(起, 止), ...]
    has_discard_line: bool
    issues: list = field(default_factory=list)   # 解析期发现的格式问题


# ── 解析 ────────────────────────────────────────────────────────────────
def parse_discards(lines):
    """从头部 `> …已舍弃内容…` 行抽出登记的行号区间。

    只认 `行 X` 与 `行 X–Y`：每个区间必须自带 `行` 前缀。承前省略的写法报 ERROR——
    省略号后的数字脱离了 `行` 标记，脚本没有把握它是行号，不能替作者猜。

    返回 (区间列表, 是否找到该行, 格式问题)。
    """
    for idx, s in enumerate(lines, 1):
        if s.lstrip().startswith('>') and '已舍弃内容' in s:
            issues = []
            for abbr in DISCARD_ABBR_RE.findall(s):
                nums = re.sub(r'^\s*[、,，]\s*', '', abbr).strip()
                issues.append(Issue('ERROR', '格式',
                                    f'"已舍弃内容"里的「{nums}」承前省略了 `行` 前缀，不予解析',
                                    f'每个区间都写全前缀，如 `行 250–261、行 427`', idx))
            ranges = [(int(a), int(b) if b else int(a)) for a, b in DISCARD_RE.findall(s)]
            return ranges, True, issues
    return [], False, []


def parse_note(path, require_answers=True):
    """顺序扫描笔记，抽出 Q 条目与头部舍弃登记，记录语法层面的格式问题。

    状态机：读到 `### **Q**:` 进入 want_meta，期待紧跟的声明行；拿到声明行后进入
    want_anchor，下一非空行若是锚点行则收进账本；随后进入 body，把正文累加为答案，
    直到下一个 `##` 标题或 `概念：` 行收尾。

    require_answers=False（账本审计阶段）时不要求答案正文。
    """
    lines = path.read_text(encoding='utf-8').splitlines()
    entries, issues = [], []
    cur = state = None

    def bad_meta(entry, found, line):
        """声明行不合法。不区分是缺失、写坏还是多区间——合法只有一种，其余都是这一条错。"""
        issues.append(Issue('ERROR', '格式',
                            f'Q「{entry.label}」声明行不合法，{found}',
                            f'唯一合法写法是 `{META_SPEC}`，只有 X / Y / Z 三个数字可变', line))

    def close():
        nonlocal cur, state
        if cur is None:
            return
        if state == 'want_meta':
            bad_meta(cur, '该 Q 下没有声明行', cur.note_line)
        elif require_answers and cur.ans == 0:
            issues.append(Issue('ERROR', '格式', f'Q「{cur.label}」没有答案正文',
                                '按声明的压缩比写出答案，或删掉这条 Q', cur.note_line))
        entries.append(cur)
        cur = state = None

    for idx, s in enumerate(lines, 1):
        stripped = s.strip()

        mq = Q_RE.match(s)
        if mq:
            close()
            cur, state = Entry(q=mq.group(1).strip(), note_line=idx), 'want_meta'
            continue

        if state == 'want_meta':
            if not stripped:
                continue
            m = META_RE.match(stripped)
            if m:
                cur.lo, cur.hi, cur.decl = (int(g) for g in m.groups())
                state = 'want_anchor'
            else:
                bad_meta(cur, f'实际是: {stripped[:48]}', idx)
                if not META_SHAPE_RE.match(stripped):
                    cur.ans += answer_chars(s)   # 压根没写声明行，这行是正文
                    cur.body.append(clean_text(s))
                state = 'body'
            continue

        if state == 'want_anchor':
            if not stripped:
                continue
            ma = ANCHOR_RE.match(stripped)
            if ma:
                items = [t.strip() for t in re.split(r'[、,，]', ma.group(1))]
                cur.anchors = [t for t in items if t]
                if not cur.anchors or len(cur.anchors) != len(items):
                    issues.append(Issue('ERROR', '格式',
                                        f'Q「{cur.label}」锚点行含空条目或没有条目',
                                        f'按 `{ANCHOR_SPEC}` 用 、 分隔，逐条写实', idx))
                state = 'body'
                continue
            state = 'body'   # 这条 Q 没有锚点行，本行按正文处理（落到下方分支）

        # `##` 标题与 `概念：` 行为当前 Q 收尾，它们本身不是答案；close() 会把 state
        # 清掉，所以下面的正文分支自然落空。
        if state == 'body' and (HEADING_RE.match(s) or CONCEPT_RE.match(stripped)):
            close()

        if META_SHAPE_RE.match(stripped):
            issues.append(Issue('ERROR', '格式', '游离的声明行（上方没有紧邻的 Q 标题）',
                                '移到所属 Q 标题正下方，或删除', idx))
        elif ANCHOR_SHAPE_RE.match(stripped):
            issues.append(Issue('ERROR', '格式', '锚点行写法不合法或位置不对',
                                f'唯一合法写法是 `{ANCHOR_SPEC}`，且只能紧跟声明行的下一行', idx))
        elif state == 'body' and not stripped.startswith('>'):
            cur.ans += answer_chars(s)        # `>` 是 EX 区块 / 引用，用户手补，不计入
            cur.body.append(clean_text(s))

    close()
    discards, has_line, discard_issues = parse_discards(lines)
    return Note(path, entries, discards, has_line, issues + discard_issues)


# ── 检查 1：格式 ────────────────────────────────────────────────────────
def check_format(note, src):
    """校验行范围本身：合法、有内容、不大面积重叠、与笔记顺序一致。

    原文一行是一整段口语，话题常在行中途转折，所以相邻 Q 共享一条边界行是正常的，不报。
    重叠超过一行才提醒：那几行的字数会被两条 Q 的分母各算一次，压缩比因此虚高。
    """
    issues = list(note.issues)      # 解析期的语法问题

    for e in note.entries:
        if e.lo is None:
            continue
        if e.lo > e.hi or e.lo < 1 or e.hi > src.n:
            issues.append(Issue('ERROR', '格式',
                                f'Q「{e.label}」行范围 {e.lo}–{e.hi} 非法（原文共 {src.n} 行）',
                                '改成正序、且落在原文行数内的区间', e.note_line))
            e.lo = e.hi = None      # 判废：后面的检查据此跳过这条
        elif src.chars(e.lo, e.hi) == 0:
            issues.append(Issue('ERROR', '格式',
                                f'Q「{e.label}」行范围 {e.lo}–{e.hi} 剔除噪音行后没有内容',
                                '重新核对行号，指向实际讲述这段内容的原文行', e.note_line))
            e.lo = e.hi = None

    spans = [e for e in note.entries if e.usable]

    for a, b in pairwise(sorted(spans, key=lambda e: (e.lo, e.hi))):
        shared = min(a.hi, b.hi) - b.lo + 1
        if shared > OVERLAP_OK:
            issues.append(Issue('WARN', '格式',
                                f'Q「{b.label}」行范围 {b.lo}–{b.hi} 与 Q「{a.label}」（{a.lo}–{a.hi}）'
                                f'重叠 {shared} 行（行 {b.lo}–{min(a.hi, b.hi)}），这几行的字数被两条各算一次，'
                                f'两条的实际压缩比都会偏高',
                                '把重叠段判给其中一条，另一条的边界让开', b.note_line))

    prev = None
    for e in spans:                 # 按笔记出现顺序
        if prev and e.lo < prev.lo:
            issues.append(Issue('WARN', '格式',
                                f'Q「{e.label}」行范围 {e.lo}–{e.hi} 早于上一条 Q（{prev.lo}–{prev.hi}），'
                                f'笔记顺序与原文顺序不一致',
                                '确认是有意回指；否则按原文顺序重排 Q', e.note_line))
        prev = e

    return issues


# ── 检查 2：切分 ────────────────────────────────────────────────────────
def check_split(note, src):
    """切分尺寸：单 Q 覆盖上限与全文密度带。

    切分自由造成的危害集中在尺寸维度，这里就是拦截点：chunk 过大则压缩判断不可靠，
    过碎或漏切则密度带外。
    """
    issues = []
    for e in note.entries:
        if not e.usable:
            continue
        eff = src.chars(e.lo, e.hi)
        if eff > Q_CAP:
            issues.append(Issue('ERROR', '切分',
                                f'Q「{e.label}」覆盖 {eff} 有效字，超过单 Q 上限 {Q_CAP}',
                                '拆成两条或更多 Q，各自声明压缩比与锚点', e.note_line))

    n = sum(1 for e in note.entries if e.usable)
    density = src.chars() / n if n else 0.0
    lo, hi = DENSITY_BAND
    if n and density < lo:
        issues.append(Issue('WARN', '切分',
                            f'密度 {density:.0f} 字/Q，低于 {lo}–{hi} 带，疑似切得过碎',
                            '复查是否把同一论证拆散了；相邻同话题的 Q 合并'))
    elif n and density > hi:
        issues.append(Issue('WARN', '切分',
                            f'密度 {density:.0f} 字/Q，高于 {lo}–{hi} 带，疑似漏切',
                            '复查是否有议题被并进大 Q；按话题再切一轮'))
    return issues, dict(n=n, density=density)


# ── 检查 3：覆盖 ────────────────────────────────────────────────────────
def merge_ranges(nums):
    """把零散行号合并成连续区间：{3,4,5,9,10} → [(3,5), (9,10)]。"""
    out = []
    for x in sorted(nums):
        if out and x == out[-1][1] + 1:
            out[-1][1] = x
        else:
            out.append([x, x])
    return [tuple(r) for r in out]


def check_coverage(note, src):
    """原文每一行要么被 Q 覆盖，要么在头部登记为舍弃；两样都没有就是静默丢弃。"""
    issues = []
    covered = set()
    for e in note.entries:
        if e.usable:
            covered.update(range(e.lo, e.hi + 1))

    declared = set()
    for a, b in note.discards:
        declared.update(range(a, b + 1))

    if not note.has_discard_line:
        issues.append(Issue('WARN', '覆盖', '头部没有"已舍弃内容"行',
                            '按 WORKFLOW 的输出格式补一行，登记开场、广告、告别等舍弃内容及其行号'))

    gaps, exempt = [], 0
    for a, b in merge_ranges(set(range(1, src.n + 1)) - covered):
        chars = src.chars(a, b)                 # 与压缩比同一个"实质字数"口径
        if chars <= GAP_BUDGET:                 # 纯噪音或碎片，自动豁免
            exempt += 1
            continue
        body = [i for i in range(a, b + 1) if i not in src.noise]
        missing = [i for i in body if i not in declared]
        gaps.append(dict(lo=a, hi=b, chars=chars, silent=bool(missing)))
        if missing:
            head = re.sub(r'\s', '', src.lines[missing[0] - 1])[:28]
            shown = ','.join(map(str, missing[:6])) + ('…' if len(missing) > 6 else '')
            issues.append(Issue('ERROR', '覆盖',
                                f'原文行 {a}–{b}（{chars} 字）静默丢弃：行 {shown} 既没被任何 Q 覆盖，'
                                f'也没在头部"已舍弃内容"登记。首行:「{head}…」',
                                '是论述内容 → 补一条 Q 覆盖它；是闲聊/过渡/广告 → '
                                '在头部"已舍弃内容"写明性质与行号'))

    stats = dict(covered=len(covered), total=src.n, exempt=exempt,
                 declared=len(declared), gaps=gaps)
    return issues, stats


# ── 检查 4：压缩比 ──────────────────────────────────────────────────────
def check_ratio(note, src):
    """按剔除噪音后的原文字数核算实际压缩比，与声明值比对；全文总量对压缩带。"""
    issues, rows, devs = [], [], []
    tot_ans = tot_eff = 0

    for e in note.entries:
        if not e.usable or e.ans == 0:
            continue
        eff = src.chars(e.lo, e.hi)
        actual = 100.0 * e.ans / eff
        dev = actual - e.decl
        devs.append(dev)
        tot_ans += e.ans
        tot_eff += eff

        flag = ''
        if dev < -DEV_NEG:
            flag = 'ERR'
            issues.append(Issue('ERROR', '压缩比',
                                f'Q「{e.label}」压得比声明狠 {-dev:.0f}pp（声明 {e.decl}% / '
                                f'实际 {actual:.0f}%），细节丢失方向，超出 {DEV_NEG}pp',
                                '先复评声明值本身是否判错，再对照原文补回被削掉的内容', e.note_line))
        elif dev > DEV_POS:
            flag = 'warn'
            issues.append(Issue('WARN', '压缩比',
                                f'Q「{e.label}」写得比声明多 {dev:.0f}pp（声明 {e.decl}% / '
                                f'实际 {actual:.0f}%）',
                                '单条不打回；全文压缩带超标时，优先从这类条目收', e.note_line))
        elif e.decl >= HI_TIER and dev <= -HI_TIER_NEG:
            flag = 'warn'
            issues.append(Issue('WARN', '压缩比',
                                f'Q「{e.label}」高保留档（声明 {e.decl}%）实际只有 {actual:.0f}%，'
                                f'承重内容疑似被删',
                                '对照原文找回缺口：要么补回例子/引文/对话，要么下修声明值', e.note_line))

        rows.append(dict(note_line=e.note_line, lo=e.lo, hi=e.hi, eff=eff, ans=e.ans,
                         decl=e.decl, actual=actual, dev=dev, flag=flag, q=e.q[:34]))

    # 一份形状固定的统计：没有可核算的条目时各项归零，消费方只看 n 决定要不要显示。
    notable = [d for d in devs if abs(d) >= SYS_NOTABLE]
    stats = dict(
        rows=rows,
        n=len(devs),
        total=100.0 * tot_ans / tot_eff if tot_eff else 0.0,
        mean=sum(devs) / len(devs) if devs else 0.0,
        mean_abs=sum(abs(d) for d in devs) / len(devs) if devs else 0.0,
        tight=sum(1 for d in devs if d < 0),                # 压得比声明狠
        loose=sum(1 for d in devs if d > 0),
        neg_frac=sum(1 for d in notable if d < 0) / len(notable) if notable else 0.0,
    )

    lo, hi = TOTAL_BAND
    if tot_eff and stats['total'] < lo:
        issues.append(Issue('ERROR', '压缩比',
                            f'全文实际压缩 {stats["total"]:.1f}%，低于压缩带 {lo}–{hi}%',
                            '整体取舍过狠：复评预算分配，上调松掉了内容的条目的声明并补回内容'))
    elif tot_eff and stats['total'] > hi:
        issues.append(Issue('ERROR', '压缩比',
                            f'全文实际压缩 {stats["total"]:.1f}%，高于压缩带 {lo}–{hi}%',
                            '接近转述：重新分配预算，压低松散段落的声明并按新值重写'))

    # 单条都在容差内，但整体一边倒地压得更狠——只有均值与占比同时越线才报。
    if (stats['n'] >= SYS_MIN_Q and stats['mean'] <= SYS_MEAN_DEV
            and stats['neg_frac'] >= SYS_NEG_FRAC):
        issues.append(Issue('WARN', '压缩比',
                            f'系统性偏压：平均偏差 {stats["mean"]:+.1f}pp，'
                            f'{stats["neg_frac"]:.0%} 的条目压得比声明狠',
                            '逐条复查高保留档的取舍，例子、引文、逐轮对话是否被整体削掉了'))
    return issues, stats


# ── 检查 5：锚点 ────────────────────────────────────────────────────────
def check_anchors(note, full):
    """深压缩 Q 的锚点申报（两个阶段都查）与兑现（仅 full 阶段查）。

    锚点是细切阶段写下的字面承诺：答案写成后这些细节必须出现。匹配对锚点与答案做同一
    归一化（小写、折叠空白），另试无空白形态，避免中英混排的空白差异造成漏判。
    """
    issues = []
    n_deep = n_decl = 0
    for e in note.entries:
        if e.anchors is not None:
            n_decl += 1
        if e.decl is not None and e.decl < ANCHOR_TIER:
            n_deep += 1
            if e.anchors is None:
                issues.append(Issue('ERROR', '锚点',
                                    f'Q「{e.label}」声明 {e.decl}% 属深压缩，未申报锚点',
                                    f'在声明行下一行补 `{ANCHOR_SPEC}`：{ANCHOR_MIN}–{ANCHOR_MAX} 个'
                                    f'承重细节，按笔记中将出现的写法', e.note_line))
        if not e.anchors:
            continue
        if not (ANCHOR_MIN <= len(e.anchors) <= ANCHOR_MAX):
            issues.append(Issue('WARN', '锚点',
                                f'Q「{e.label}」申报了 {len(e.anchors)} 个锚点，'
                                f'常规为 {ANCHOR_MIN}–{ANCHOR_MAX} 个',
                                '过少 → 补齐承重细节；过多 → 只留答案离开它就失真的那些', e.note_line))
        if full and e.ans:
            text = norm_match(' '.join(e.body))
            nows = re.sub(r'\s', '', text)
            for a in e.anchors:
                an = norm_match(a)
                if an not in text and re.sub(r'\s', '', an) not in nows:
                    issues.append(Issue('ERROR', '锚点',
                                        f'Q「{e.label}」申报的锚点「{a}」未出现在答案中',
                                        '补回该细节；确属取舍变更的，更新锚点行并复评声明压缩比',
                                        e.note_line))
    return issues, dict(deep=n_deep, declared=n_decl)


# ── 定位原文 ────────────────────────────────────────────────────────────
def find_source(note_path):
    """在 <笔记目录>/raw/ 下匹配原文：同名 → 去 _vN 后缀同名 → 首段编号唯一匹配。"""
    rawdir = note_path.parent / 'raw'
    if not rawdir.is_dir():
        die(f'{rawdir} 不存在，请显式传入原文路径')
    cands = sorted(rawdir.glob('*.txt'))
    stem = note_path.stem

    for c in cands:
        if c.stem == stem:
            return c
    base = re.sub(r'_v\d+$', '', stem)
    for c in cands:
        if c.stem == base:
            return c
    hits = [c for c in cands if c.stem.split('_')[0] == base.split('_')[0]]
    if len(hits) == 1:
        return hits[0]
    die(f'无法为 {note_path.name} 匹配原文（候选: {[c.name for c in (hits or cands)]}），'
        f'请显式传入原文路径')


def die(msg):
    print(f'[FATAL] {msg}', file=sys.stderr)
    sys.exit(2)


# ── 核对与报告 ──────────────────────────────────────────────────────────
def check(note_path, source_path, stage='full'):
    full = stage == 'full'
    note = parse_note(note_path, require_answers=full)
    src = Source(source_path)

    issues = check_format(note, src)          # 必须先跑：它标掉非法行范围
    sp_issues, sp = check_split(note, src)
    cov_issues, cov = check_coverage(note, src)
    issues += sp_issues + cov_issues
    if full:
        rat_issues, rat = check_ratio(note, src)
        issues += rat_issues
    else:
        rat = dict(rows=[], n=0, total=0.0, mean=0.0, mean_abs=0.0,
                   tight=0, loose=0, neg_frac=0.0)
    anc_issues, anc = check_anchors(note, full)
    issues += anc_issues

    return dict(note=note, src=src, issues=issues, stage=stage,
                split=sp, coverage=cov, ratio=rat, anchors=anc)


def verdict(issues, check=None):
    """某项检查（或全部）的结论：FAIL / WARN / PASS。"""
    sel = [i for i in issues if check is None or i.check == check]
    if any(i.level == 'ERROR' for i in sel):
        return 'FAIL'
    return 'WARN' if sel else 'PASS'


def render(res):
    """打印一份人读与模型读都成立的报告：先结论，再明细，最后逐条修复指令。"""
    note, src, issues = res['note'], res['src'], res['issues']
    sp, cov, rat, anc = res['split'], res['coverage'], res['ratio'], res['anchors']
    full = res['stage'] == 'full'

    n_noise = len(src.noise)
    tag = '' if full else '  ·  账本审计（--stage ledger）'
    print(f'笔记 {note.path}  ·  {len(note.entries)} 条 Q{tag}')
    print(f'原文 {src.path}  ·  {src.n} 行 / {src.raw_chars} 字'
          f'  ·  剔除噪音 {n_noise} 行 {src.noise_chars} 字后，有效 {src.chars()} 字')
    if n_noise:
        for i in sorted(src.noise)[:2]:
            print(f'       噪音行示例 → 行 {i}: {src.lines[i - 1].strip()[:40]}')
    print()

    gaps = cov['gaps']
    silent = sum(1 for g in gaps if g['silent'])
    print(f'[1/5] 格式    {verdict(issues, "格式"):<4}  '
          f'{sum(1 for i in issues if i.check == "格式")} 处问题')
    print(f'[2/5] 切分    {verdict(issues, "切分"):<4}  '
          f'{sp["n"]} 条可核算 Q · 密度 {sp["density"]:.0f} 字/Q'
          f'（带 {DENSITY_BAND[0]}–{DENSITY_BAND[1]}，单 Q 上限 {Q_CAP}）')
    print(f'[3/5] 覆盖    {verdict(issues, "覆盖"):<4}  '
          f'覆盖 {cov["covered"]}/{cov["total"]} 行；静默丢弃 {silent} 处，'
          f'已登记舍弃 {len(gaps) - silent} 处，碎片豁免 {cov["exempt"]} 处')
    if full and rat['n']:
        print(f'[4/5] 压缩比  {verdict(issues, "压缩比"):<4}  '
              f'总 {rat["total"]:.1f}%（带 {TOTAL_BAND[0]}–{TOTAL_BAND[1]}）；'
              f'平均偏差 {rat["mean"]:+.1f}pp（|偏差| {rat["mean_abs"]:.1f}pp）；'
              f'偏狠/偏松 {rat["tight"]}/{rat["loose"]}')
    elif full:
        print(f'[4/5] 压缩比  --    没有可核算的条目')
    else:
        print(f'[4/5] 压缩比  --    账本阶段不核算')
    anc_note = '' if full else '（兑现在成品审计时核对）'
    print(f'[5/5] 锚点    {verdict(issues, "锚点"):<4}  '
          f'深压缩 Q {anc["deep"]} 条 · 已申报 {anc["declared"]} 条{anc_note}')
    print()

    if rat['rows']:
        print('== 压缩比明细（实际值 = 答案字 ÷ 剔除噪音后的原文字） ==')
        print(f'{"笔记行":>6}{"原文行":>12}{"有效字":>8}{"答案字":>8}{"声明":>6}{"实际":>6}{"偏差":>7}  {"":<4} Q')
        for r in rat['rows']:
            span = '{}-{}'.format(r['lo'], r['hi'])
            print(f'{r["note_line"]:>6}{span:>12}{r["eff"]:>8}{r["ans"]:>8}'
                  f'{r["decl"]:>6}{r["actual"]:>6.0f}{r["dev"]:>+7.0f}  {r["flag"]:<4} {r["q"]}')
        print()

    if gaps:
        print('== 覆盖缺口 ==')
        for g in gaps:
            tag = '静默丢弃 [ERROR]' if g['silent'] else '已在"已舍弃内容"登记'
            print(f'  行 {g["lo"]}–{g["hi"]}（{g["chars"]} 字）: {tag}')
        print()

    if issues:
        print('== 待处理 ==')
        order = {'ERROR': 0, 'WARN': 1}
        for i in sorted(issues, key=lambda x: (order[x.level], x.check, x.line)):
            loc = f'笔记行 {i.line} · ' if i.line else ''
            print(f'[{i.level}] {i.check} · {loc}{i.msg}')
            if i.fix:
                print(f'        → {i.fix}')
        print()

    n_err = sum(1 for i in issues if i.level == 'ERROR')
    n_warn = len(issues) - n_err
    print(f'结论 {verdict(issues)} · {n_err} ERROR / {n_warn} WARN')


def as_json(res):
    note, src, issues = res['note'], res['src'], res['issues']
    return dict(
        note=str(note.path), source=str(src.path), stage=res['stage'],
        verdict=verdict(issues),
        checks={k: verdict(issues, k) for k in CHECKS},
        source_stats=dict(lines=src.n, chars=src.raw_chars,
                          noise_lines=len(src.noise), noise_chars=src.noise_chars,
                          effective_chars=src.chars()),
        split=res['split'], coverage=res['coverage'],
        ratio=res['ratio'], anchors=res['anchors'],
        issues=[dict(level=i.level, check=i.check, note_line=i.line, msg=i.msg, fix=i.fix)
                for i in issues],
    )


# ── 改行校验 ────────────────────────────────────────────────────────────
def run_rebreak(new_path, old_path):
    """校验 raw 的改行编辑只动了换行：新旧版本去除全部空白后必须逐字节相同。

    基线缺省取 git HEAD 里的已提交版本；raw 尚未入库、或要对任意旧版比对时，
    显式传入原版文件。守恒口径与计字口径同源（都按非空白字符），因此改行不影响
    任何有效字数的核算。
    """
    new_text = new_path.read_text(encoding='utf-8')
    if old_path is not None:
        old_text = old_path.read_text(encoding='utf-8')
        base = str(old_path)
    else:
        r = subprocess.run(['git', '-C', str(new_path.parent), 'show',
                            f'HEAD:./{new_path.name}'],
                           capture_output=True, text=True)
        if r.returncode != 0:
            die(f'{new_path.name} 没有已提交的基线版本（{r.stderr.strip()[:60]}）；'
                f'先提交 raw 入库，或显式传入原版: --rebreak <新版.txt> <原版.txt>')
        old_text = r.stdout
        base = f'HEAD:{new_path.name}'

    a = re.sub(r'\s', '', old_text)
    b = re.sub(r'\s', '', new_text)
    old_n = len(old_text.splitlines())
    new_n = len(new_text.splitlines())
    print(f'基线 {base}  ·  {old_n} 行 / {len(a)} 字')
    print(f'现版 {new_path}  ·  {new_n} 行 / {len(b)} 字')

    if a == b:
        print(f'[PASS] 内容守恒：去除空白后逐字节相同；行数 {old_n} → {new_n}（{new_n - old_n:+d}）')
        if new_n < old_n:
            print('[WARN] 行数减少，有行被合并：原有行号整体前移，重核对所有已写的行范围')
        return 0

    i = next((k for k, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    print(f'[FAIL] 内容不守恒：第 {i + 1} 个非空白字符起分歧')
    print(f'       基线: …{a[max(0, i - 20):i + 20]}…')
    print(f'       现版: …{b[max(0, i - 20):i + 20]}…')
    print('       → 回退 raw 到基线版本，重新做只插入换行的编辑')
    return 1


# ── 指标基底回算 ────────────────────────────────────────────────────────
def pctl(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))] if xs else 0


def run_baseline(paths, explicit):
    """对已提交的好笔记回算指标基底的实测分布，供人工修订常数。不自动更新。"""
    effs, decls, dens, tots = [], [], [], []
    print(f'{"笔记":<44}{"Q数":>4}{"有效字":>8}{"字/Q":>7}{"总压缩":>8}')
    for p in paths:
        res = check(p, explicit or find_source(p), 'full')
        rat, src = res['ratio'], res['src']
        if not rat['n']:
            print(f'{p.name[:42]:<44}  --  没有可核算的条目')
            continue
        density = src.chars() / rat['n']
        dens.append(density)
        tots.append(rat['total'])
        for r in rat['rows']:
            effs.append(r['eff'])
            decls.append(r['decl'])
        print(f'{p.name[:42]:<44}{rat["n"]:>4}{src.chars():>8}{density:>7.0f}{rat["total"]:>7.1f}%')
    if not effs:
        print('\n没有可核算的条目，无法回算。')
        return
    print()
    print(f'单 Q 有效字   p50={pctl(effs, 50)}  p75={pctl(effs, 75)}  p90={pctl(effs, 90)}  '
          f'max={max(effs)}    ← Q_CAP 当前 {Q_CAP}')
    print(f'密度（字/Q）  min={min(dens):.0f}  max={max(dens):.0f}    '
          f'← DENSITY_BAND 当前 {DENSITY_BAND}')
    print(f'总压缩（%）   min={min(tots):.1f}  max={max(tots):.1f}    '
          f'← TOTAL_BAND 当前 {TOTAL_BAND}')
    print(f'声明值（%）   p25={pctl(decls, 25)}  p50={pctl(decls, 50)}  p75={pctl(decls, 75)}    '
          f'← ANCHOR_TIER 当前 {ANCHOR_TIER}')
    print('\n以上为实测分布，确认后人工修订本脚本常数区与 WORKFLOW 中的对应描述。')


# ── 入口 ────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='check_note.py', add_help=True,
        description='按 WORKFLOW.md 核对笔记：格式 / 切分 / 覆盖 / 压缩比 / 锚点',
        epilog='原文缺省时在 <笔记目录>/raw/ 下自动匹配。')
    ap.add_argument('paths', nargs='+', metavar='笔记.md',
                    help='一个或多个笔记；末尾可跟一个 .txt 作为显式原文（仅单篇时有效）')
    ap.add_argument('--stage', choices=('full', 'ledger'), default='full',
                    help='ledger = 细切建账后的账本审计（不核算压缩比、不要求答案）')
    ap.add_argument('--json', action='store_true', help='输出 JSON，供程序消费')
    ap.add_argument('--baseline', action='store_true',
                    help='对给定笔记回算指标基底的实测分布，供人工修订常数')
    ap.add_argument('--rebreak', action='store_true',
                    help='校验 raw 的改行编辑：只许动换行。参数为 <新版.txt> [<原版.txt>]，'
                         '原版缺省取 git HEAD 版本')
    args = ap.parse_args(argv)

    paths = [Path(p) for p in args.paths]

    if args.rebreak:
        if not 1 <= len(paths) <= 2:
            die('--rebreak 需要 1 或 2 个参数: <新版.txt> [<原版.txt>]')
        for p in paths:
            if not p.is_file():
                die(f'文件不存在: {p}')
        return run_rebreak(paths[0], paths[1] if len(paths) == 2 else None)

    explicit = None
    if len(paths) == 2 and paths[1].suffix == '.txt':
        paths, explicit = paths[:1], paths[1]

    for p in paths + ([explicit] if explicit else []):
        if not p.is_file():
            die(f'文件不存在: {p}')

    if args.baseline:
        run_baseline(paths, explicit)
        return 0

    results, worst = [], 0
    for i, p in enumerate(paths):
        res = check(p, explicit or find_source(p), args.stage)
        if verdict(res['issues']) == 'FAIL':   # 退出码的口径与报告结论同源
            worst = 1
        if args.json:
            results.append(as_json(res))
        else:
            if i:
                print('\n' + '─' * 72 + '\n')
            render(res)

    if args.json:
        print(json.dumps(results if len(results) > 1 else results[0],
                         ensure_ascii=False, indent=2))
    return worst


if __name__ == '__main__':
    sys.exit(main())
