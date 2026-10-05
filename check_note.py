#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 WORKFLOW.md 审计讲座笔记，并为各步骤准备输入。

子命令：
  check    自动判断笔记所处步骤，累计审计，给出下一步
  pack     输出带行号的 raw 切片 / 写作包 / 核验包
  verify   用全新上下文（claude -p）逐条核验答案，结果写回笔记
  show     人工复核：对照某条 Q 的 raw 与答案；或反查 raw 某行归属
  strip    删除已处置的核验标记
  links    检查 _concepts 与笔记模块的双向链接
  rebreak  校验 raw 改行只改变了空白
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = tomllib.loads((ROOT / 'thresholds.toml').read_text(encoding='utf-8'))
TIERS = {k: tuple(v) for k, v in CFG['tiers'].items()}
FORMS = {k: tuple(v) for k, v in CFG['forms'].items()}
HEADER_FIELDS = ['来源', '对谈人', '已舍弃内容', '内容存疑', '转写错误']
CONCEPT_ORDER = ['人物', '学派与方法', '核心概念', '精神分析']
LABELS = {'书目': 1, '脉络': 2, '概念': 3, '话题': 4}
STAGES = ['source', 'modules', 'ledger', 'draft', 'verify', 'concepts', 'left', 'complete']
STAGE_NAMES = {'source': '步骤 0 来源底稿', 'modules': '步骤 1 粗切', 'ledger': '步骤 2 建账',
               'draft': '步骤 3 提炼', 'verify': '步骤 4 核验', 'concepts': '步骤 5 概念链接',
               'left': "步骤 6 What's Left", 'complete': '完成'}

NOISE_RE = re.compile(r'^\s*(?:\d+|\d+号讲话人.*|发言人\s*\d+.*|\d{1,2}:\d{2}(?::\d{2})?)\s*$')
Q_RE = re.compile(r'^###\s+\*\*Q\*\*:(.*)$')
META_RE = re.compile(r'^\*行 (\d+)–(\d+) · (保留|标准|精简)\*$')
LEGACY_META_RE = re.compile(r'^\*行 (\d+)–(\d+) · 压缩至约 (\d+)%\*$')
KEY_RE = re.compile(r'^\*(?:关键词|锚点)：(.+)\*$')
MODULE_RE = re.compile(r'^##\s+(模块[^：:]+[：:].*)$')
MODULE_RANGE_RE = re.compile(r'^\*模块范围：行 (\d+)–(\d+)\*$')
CONCEPT_RE = re.compile(r'^概念：\s*(.*)$')
L_RE = re.compile(r"^##\s+L: What's Left\s*$")
VERIFY_RE = re.compile(r'^<!--\s*核验：\s*(\S+?)\s*(-->)?\s*$')
FORM_RE = re.compile(r'形式：(' + '|'.join(FORMS) + ')')
DISCARD_RE = re.compile(r'行\s*(\d+)(?:\s*–\s*(\d+))?')
LINK_RE = re.compile(r'^\[\[([^#\]|]+)(?:#([^\]|]+))?(?:\|[^\]]+)?\]\]$')
ANNOT_RE = re.compile(r'（[^（）]*(?:转写错误|内容存疑)：[^）]*）')
STOPWORDS = set('''the and that this with have from they what your about there their would which when were been
will just like know think yeah okay right going really because them then than also some very more into could
should here where those these only even much other well actually something people thing things kind sort mean
said says want time does doing make made being over such most many hello thank thanks'''.split())


@dataclass
class Issue:
    level: str
    check: str
    msg: str
    fix: str = ''
    line: int = 0


@dataclass
class Entry:
    idx: int
    q: str
    note_line: int
    module: str = ''
    lo: int | None = None
    hi: int | None = None
    tier: str | None = None
    legacy_pct: int | None = None
    keywords: list[str] = field(default_factory=list)
    meta_line: int = 0
    key_line: int = 0
    answer: list[str] = field(default_factory=list)
    verify: dict | None = None

    @property
    def usable(self):
        return self.lo is not None and self.hi is not None

    @property
    def answered(self):
        return any(chars(x) for x in self.answer)


@dataclass
class Note:
    path: Path
    lines: list[str]
    entries: list[Entry]
    modules: list[dict]
    discards: list[tuple[int, int]]
    issues: list[Issue] = field(default_factory=list)

    def header(self, name):
        for s in self.lines:
            m = re.match(r'^>\s*' + re.escape(name) + r'：\s*(.*)$', s)
            if m:
                return m.group(1).strip()
        return None

    @property
    def form(self):
        m = FORM_RE.search(self.header('来源') or '')
        return m.group(1) if m else None


class Source:
    def __init__(self, path: Path):
        self.path = path
        self.lines = path.read_text(encoding='utf-8').splitlines()
        self.n = len(self.lines)
        self.noise = {i for i, s in enumerate(self.lines, 1) if NOISE_RE.match(s)}
        self.ch = [0]
        for i, s in enumerate(self.lines, 1):
            self.ch.append(self.ch[-1] + (0 if i in self.noise else len(re.sub(r'\s', '', s))))

    def chars(self, lo=1, hi=None):
        hi = self.n if hi is None else hi
        if lo < 1 or hi > self.n or lo > hi:
            return 0
        return self.ch[hi] - self.ch[lo - 1]

    def line_chars(self, i):
        return self.ch[i] - self.ch[i - 1]


# ── 计数 ──────────────────────────────────────────────────────

def clean(s):
    st = s.strip()
    if st.startswith('>') or st.startswith('<!--') or CONCEPT_RE.match(st) or re.match(r'^[-*_]{3,}$', st):
        return ''
    s = ANNOT_RE.sub('', s)
    s = re.sub(r'^\s*(?:[-+*]|\d+[.)])\s+', '', s)
    s = re.sub(r'\[\[[^\]|]+\|([^\]]+)\]\]', r'\1', s)
    s = re.sub(r'\[\[([^\]]+)\]\]', r'\1', s)
    return re.sub(r'[*_`~#]', '', s)


def chars(s):
    return len(re.sub(r'\s', '', clean(s)))


def answer_text(e):
    return ''.join(clean(x) for x in e.answer)


def answer_chars(e):
    return len(re.sub(r'\s', '', answer_text(e)))


def compress_ranges(vals):
    out = []
    for x in sorted(vals):
        if out and x == out[-1][1] + 1:
            out[-1][1] = x
        else:
            out.append([x, x])
    return [tuple(x) for x in out]


def legacy_tier(pct):
    return '保留' if pct >= 70 else ('标准' if pct >= 55 else '精简')


# ── 解析 ──────────────────────────────────────────────────────

def parse_discards(lines):
    issues = []
    hits = [i for i, s in enumerate(lines, 1) if re.match(r'^>\s*已舍弃内容：', s)]
    if not hits:
        return [], issues
    s = lines[hits[0] - 1]
    if re.search(r'行\s*\d+(?:\s*–\s*\d+)?\s*[、,，]\s*\d', s):
        issues.append(Issue('ERROR', '格式', '“已舍弃内容”存在省略“行”前缀的区间', '每个区间都写成“行 X–Y”', hits[0]))
    if re.search(r'行\s*\d+\s*[-—~]\s*\d+', s):
        issues.append(Issue('ERROR', '格式', '“已舍弃内容”区间连接号不是 –（U+2013）', '改为“行 X–Y”', hits[0]))
    return [(int(a), int(b or a)) for a, b in DISCARD_RE.findall(s)], issues


def parse_note(path: Path) -> Note:
    lines = path.read_text(encoding='utf-8').splitlines()
    discards, issues = parse_discards(lines)
    entries, modules = [], []
    cur, state, mod = None, '', ''
    in_comment = None   # 正在读的多行注释：所属 Entry（核验块）或 'other'

    def close():
        nonlocal cur, state
        if cur is not None and state == 'meta':
            issues.append(Issue('ERROR', '格式', f'Q{cur.idx}“{cur.q[:20]}”缺少声明行', 'Q 标题下空一行写 *行 X–Y · 标准*', cur.note_line))
        elif cur is not None and state == 'keys':
            issues.append(Issue('ERROR', '格式', f'Q{cur.idx}“{cur.q[:20]}”缺少关键词行', '紧跟声明行写 *关键词：a、b、c*', cur.meta_line))
        cur, state = None, ''

    for i, s in enumerate(lines, 1):
        st = s.strip()
        if in_comment is not None:
            if isinstance(in_comment, Entry):
                body = st[:-3].strip() if st.endswith('-->') else st
                if body:
                    in_comment.verify['items'].append(body)
                in_comment.verify['end'] = i
            if st.endswith('-->'):
                in_comment = None
            continue
        mv = VERIFY_RE.match(st)
        if mv and cur is not None and state in ('body', 'keys'):
            if state == 'keys':
                issues.append(Issue('ERROR', '格式', f'Q{cur.idx} 核验标记出现在关键词行之前', '核验标记放在关键词行之后', i))
            st_raw = mv.group(1)
            cur.verify = {'status': st_raw.replace('（复核）', ''), 'rechecked': '复核' in st_raw, 'line': i, 'end': i, 'items': []}
            state = 'body'
            if not mv.group(2):
                in_comment = cur
            continue
        if st.startswith('<!--'):
            if not st.endswith('-->'):
                in_comment = 'other'
            continue
        mm = MODULE_RE.match(s)
        if mm:
            close()
            mod = mm.group(1).strip()
            modules.append({'title': mod, 'line': i, 'range': None, 'concept': []})
            continue
        mr = MODULE_RANGE_RE.match(st)
        if mr and modules:
            modules[-1]['range'] = (int(mr.group(1)), int(mr.group(2)))
            continue
        mq = Q_RE.match(s)
        if mq:
            close()
            cur = Entry(len(entries) + 1, mq.group(1).strip(), i, module=mod)
            entries.append(cur)
            state = 'meta'
            continue
        if L_RE.match(s):
            close()
            state = 'tail'
            continue
        if CONCEPT_RE.match(st):
            close()
            if modules:
                modules[-1]['concept'].append((i, st))
            continue
        if cur is not None and state == 'meta' and st:
            m, lm = META_RE.match(st), LEGACY_META_RE.match(st)
            if m:
                cur.lo, cur.hi, cur.tier = int(m.group(1)), int(m.group(2)), m.group(3)
            elif lm:
                cur.lo, cur.hi, cur.legacy_pct = int(lm.group(1)), int(lm.group(2)), int(lm.group(3))
                cur.tier = legacy_tier(cur.legacy_pct)
            else:
                issues.append(Issue('ERROR', '格式', f'Q{cur.idx} 声明行不合法：{st[:30]}', '写成 *行 X–Y · 保留|标准|精简*（连接号 – U+2013，间隔号 · U+00B7）', i))
                state = 'body'
                continue
            cur.meta_line, state = i, 'keys'
            continue
        if cur is not None and state == 'keys' and st:
            m = KEY_RE.match(st)
            if m:
                cur.keywords = [x.strip() for x in re.split('[、,，]', m.group(1))]
                cur.key_line, state = i, 'body'
                continue
            issues.append(Issue('ERROR', '格式', f'Q{cur.idx} 缺少关键词行', '紧跟声明行写 *关键词：a、b、c*', i))
            state = 'body'
        if cur is not None and state == 'body' and st and not st.startswith('>'):
            cur.answer.append(s)
    close()
    return Note(path, lines, entries, modules, discards, issues)


# ── 各阶段检查 ────────────────────────────────────────────────

def check_source(note):
    issues, lines = [], note.lines
    if not lines or not re.match(r'^# 讲座笔记：.*\S', lines[0]):
        issues.append(Issue('ERROR', '来源', '第一行应为“# 讲座笔记：[标题]”', '补写一级标题', 1))
    pos = []
    for n in HEADER_FIELDS:
        hits = [i for i, s in enumerate(lines, 1) if re.match(r'^>\s*' + re.escape(n) + r'：', s)]
        if len(hits) != 1:
            issues.append(Issue('ERROR', '来源', f'头部字段“{n}”应恰好一次（实际 {len(hits)}）', '补齐或去重；没有内容写“无”', hits[0] if hits else 1))
        elif not re.sub(r'^>\s*' + re.escape(n) + r'：\s*', '', lines[hits[0] - 1]).strip():
            issues.append(Issue('ERROR', '来源', f'头部字段“{n}”为空', '没有内容时写“无”', hits[0]))
        pos.append(hits[0] if hits else 10 ** 9)
    if pos != sorted(pos):
        issues.append(Issue('ERROR', '来源', '头部字段顺序错误', '按 来源→对谈人→已舍弃内容→内容存疑→转写错误 排列'))
    if note.header('来源') is not None and not note.form:
        issues.append(Issue('ERROR', '来源', f'“来源”缺少“形式：{"|".join(FORMS)}”', '在来源行内写明，例如“……，形式：对谈，……”'))
    speaker = {}
    for label, name in re.findall(r'(\d+)号讲话人\s*[，,、]?\s*([^；;，,)）]+)', note.header('对谈人') or ''):
        if label in speaker and speaker[label] != name.strip():
            issues.append(Issue('ERROR', '来源', f'{label}号讲话人身份映射冲突', '统一同一标号的身份'))
        speaker[label] = name.strip()
    return issues


def check_raw_fresh(note, src):
    """raw 相对 HEAD 有改动、而笔记已有提交版本时，旧行号可能失效。"""
    def git(*args):
        return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True)
    try:
        if git('rev-parse').returncode:
            return []
        changed = git('diff', '--quiet', 'HEAD', '--', str(src.path.resolve())).returncode == 1
        note_tracked = git('cat-file', '-e', f'HEAD:{note.path.resolve().relative_to(ROOT).as_posix()}').returncode == 0
    except (OSError, ValueError):
        return []
    if changed and note_tracked:
        return [Issue('WARN', '原文', 'raw 相对 HEAD 有改动，已提交笔记的行号可能失效', '运行 rebreak 校验，并重核所有行号')]
    return []


def check_modules(note, src):
    issues, spans = [], []
    if not note.modules:
        return [Issue('ERROR', '切分', '没有模块标题', '添加“## 模块一：…”')]
    for m in note.modules:
        if not m['range']:
            issues.append(Issue('ERROR', '切分', f'模块“{m["title"]}”缺少模块范围', '模块标题下写 *模块范围：行 X–Y*', m['line']))
            continue
        a, b = m['range']
        spans.append((a, b, m))
        if a < 1 or b > src.n or a > b:
            issues.append(Issue('ERROR', '切分', f'模块范围 {a}–{b} 越界或倒序', f'改为 1–{src.n} 内的正序区间', m['line']))
    spans.sort(key=lambda x: x[0])
    for x, y in zip(spans, spans[1:]):
        if x[1] >= y[0]:
            issues.append(Issue('ERROR', '切分', f'模块范围 {x[0]}–{x[1]} 与 {y[0]}–{y[1]} 重叠', '调整模块边界', y[2]['line']))
    covered = set()
    for a, b, _ in spans:
        covered.update(range(max(1, a), min(src.n, b) + 1))
    for a, b in note.discards:
        covered.update(range(max(1, a), min(src.n, b) + 1))
    for a, b in compress_ranges(set(range(1, src.n + 1)) - covered - src.noise):
        n = src.chars(a, b)
        if n == 0:
            continue
        lv = 'WARN' if n <= CFG['ledger']['gap_budget'] else 'ERROR'
        issues.append(Issue(lv, '切分', f'raw 行 {a}–{b}（{n} 字）未落入任何模块或结构性舍弃', '扩展相邻模块范围，或登记进“已舍弃内容”'))
    return issues


def valid_entries(note, src):
    issues, usable = [], []
    for e in note.entries:
        if not e.usable:
            continue
        if e.lo < 1 or e.hi > src.n or e.lo > e.hi:
            issues.append(Issue('ERROR', '格式', f'Q{e.idx} 范围 {e.lo}–{e.hi} 越界或倒序', f'改为 1–{src.n} 内的正序区间', e.note_line))
            continue
        if src.chars(e.lo, e.hi) == 0:
            issues.append(Issue('ERROR', '格式', f'Q{e.idx} 范围内只有空行或讲话人标号', '核对行号', e.note_line))
            continue
        usable.append(e)
    for a, b in zip(usable, usable[1:]):
        if b.lo < a.lo:
            issues.append(Issue('ERROR', '切分', f'Q{b.idx} 起始行 {b.lo} 早于前一条 Q{a.idx} 的 {a.lo}', 'Q 按 raw 顺序排列', b.note_line))
    su = sorted(usable, key=lambda x: x.lo)
    for a, b in zip(su, su[1:]):
        ov = min(a.hi, b.hi) - b.lo + 1
        if ov > 1:
            issues.append(Issue('ERROR', '切分', f'Q{a.idx} 与 Q{b.idx} 范围重叠 {ov} 行（{b.lo}–{min(a.hi, b.hi)}）', '相邻 Q 最多共享 1 个边界行', b.note_line))
    return issues, usable


def check_ledger(note, src, final=True):
    L = CFG['ledger']
    issues = list(note.issues)
    x, usable = valid_entries(note, src)
    issues += x
    if not note.entries:
        issues.append(Issue('ERROR', '切分', '没有任何 Q', '按模块建立 Q 账本'))
    pending = [m for m in note.modules if m['range'] and not any(e.module == m['title'] for e in note.entries)]
    for m in note.modules:
        if m['range'] and m not in pending:
            issues.append(Issue('ERROR', '格式', f'模块“{m["title"]}”已建账，但模块范围行未删除', '删除该模块的 *模块范围：…* 行', m['line']))
    if pending and not final:
        issues.append(Issue('WARN', '进度', f'建账进行中：还有 {len(pending)} 个模块未建账（{"、".join(m["title"].split("：")[0] for m in pending)}）', '继续 pack <笔记> --module N 建账'))
    elif pending:
        issues.append(Issue('ERROR', '进度', f'还有 {len(pending)} 个模块未建账', '全部模块建账后才能写答案'))
    for e in note.entries:
        if e.legacy_pct is not None:
            issues.append(Issue('WARN', '格式', f'Q{e.idx} 使用旧格式“压缩至约 {e.legacy_pct}%”，按“{e.tier}”档检查', '改写为 *行 X–Y · 保留|标准|精简*', e.meta_line))
        if e.meta_line and (not L['keywords'][0] <= len(e.keywords) <= L['keywords'][1] or any(not k for k in e.keywords)):
            issues.append(Issue('ERROR', '关键词', f'Q{e.idx} 关键词 {len(e.keywords)} 个（要求 {L["keywords"][0]}–{L["keywords"][1]}）', '补齐或删减；用“、”分隔', e.key_line or e.note_line))
    for e in usable:
        eff = src.chars(e.lo, e.hi)
        if eff > L['q_cap']:
            issues.append(Issue('ERROR', '切分', f'Q{e.idx} 覆盖 {eff} 有效字，超过 {L["q_cap"]}', '拆成多条 Q', e.note_line))
    n = len(usable)
    density = src.chars() / n if n else 0
    if n and not pending and not L['density'][0] <= density <= L['density'][1]:
        issues.append(Issue('WARN', '切分', f'Q 密度 {density:.0f} 字/Q，参考带 {L["density"][0]}–{L["density"][1]}', '复查是否漏切或切得过碎；确认合理可继续'))
    covered_chars = sum(src.chars(e.lo, e.hi) for e in usable)
    keep = sum(src.chars(e.lo, e.hi) for e in usable if e.tier == '保留')
    if covered_chars and 100 * keep / covered_chars > L['keep_share_max']:
        issues.append(Issue('WARN', '档位', f'“保留”档占 Q 覆盖字数 {100 * keep / covered_chars:.0f}%（参考上限 {L["keep_share_max"]}%）', '只有引文、逐轮对话、密集论证才用“保留”；逐条复查'))
    if not any(re.match(r'^>\s*已舍弃内容：', s) for s in note.lines):
        issues.append(Issue('ERROR', '覆盖', '缺少“已舍弃内容”头部字段', '没有舍弃时写“无”'))
    declared, covered = set(), set()
    for a, b in note.discards:
        if a < 1 or b > src.n or a > b:
            issues.append(Issue('ERROR', '覆盖', f'已舍弃内容区间 {a}–{b} 越界或倒序', '改为 raw 范围内的正序区间'))
        declared.update(range(a, b + 1))
    for e in usable:
        covered.update(range(e.lo, e.hi + 1))
    for m in pending:
        covered.update(range(max(1, m['range'][0]), min(src.n, m['range'][1]) + 1))
    for a, b in compress_ranges(covered & declared):
        if src.chars(a, b) > L['gap_budget']:
            issues.append(Issue('WARN', '覆盖', f'raw 行 {a}–{b} 同时被 Q 覆盖和登记为舍弃', '二选一'))
    for a, b in compress_ranges(set(range(1, src.n + 1)) - covered - declared - src.noise):
        eff = src.chars(a, b)
        if eff == 0:
            continue
        lv = 'WARN' if eff <= L['gap_budget'] else 'ERROR'
        issues.append(Issue(lv, '覆盖', f'raw 行 {a}–{b}（{eff} 字）未被 Q 覆盖，也未登记舍弃', '扩展相邻 Q、新增 Q，或在“已舍弃内容”登记性质与行号'))
    stats = {'q': n, 'raw_chars': src.chars(), 'covered_chars': covered_chars, 'density': round(density),
             'max_q': max((src.chars(e.lo, e.hi) for e in usable), default=0),
             'tiers': {t: sum(1 for e in usable if e.tier == t) for t in TIERS}}
    return issues, stats, usable


def q_ratio(e, src):
    eff, ans = src.chars(e.lo, e.hi), answer_chars(e)
    return eff, ans, (100 * ans / eff if eff else 0)


def check_draft(note, src, usable):
    D = CFG['draft']
    issues, rows = [], []
    ta = te = expect = 0
    todo = [e for e in usable if not e.answered]
    if todo:
        issues.append(Issue('ERROR', '答案', f'还有 {len(todo)} 条 Q 没有答案：' + '、'.join(f'Q{e.idx}' for e in todo),
                            f'pack {note.path.name} Q{todo[0].idx}（或 --module N），然后写答案'))
    for e in usable:
        if not e.answered:
            continue
        eff, ans, actual = q_ratio(e, src)
        txt = re.sub(r'\s', '', answer_text(e).lower())
        for k in e.keywords:
            if k and re.sub(r'\s', '', k.lower()) not in txt:
                issues.append(Issue('ERROR', '关键词', f'Q{e.idx} 关键词“{k}”未在本答案出现', '补回该细节；查证后写法改变时同步改关键词写法', e.note_line))
        lo, hi = TIERS[e.tier]
        rows.append({'q': e.idx, 'tier': e.tier, 'eff': eff, 'ans': ans, 'actual': round(actual, 1)})
        ta, te, expect = ta + ans, te + eff, expect + eff * (lo + hi) / 2
        if not lo <= actual <= hi:
            if actual < lo:
                order = list(TIERS)
                lower = order[order.index(e.tier) + 1] if order.index(e.tier) + 1 < len(order) else None
                side, fix = '低于', '回到 raw 找被略掉的例子、限定语、对话轮次补回；raw 里找不到可补的内容时' + (f'改为“{lower}”档' if lower else '保持现状并在 WARN 中说明') + '。严禁为凑字数加入 raw 没有的话'
            else:
                side, fix = '高于', '删去复述性内容和口语铺垫'
            issues.append(Issue('WARN' if eff < D['small_q'] else 'ERROR', '压缩比',
                                f'Q{e.idx} 实际 {actual:.1f}%（{ans}/{eff} 字），{side}“{e.tier}”档 {lo}–{hi}%',
                                f'{fix}；本档目标 {eff * lo // 100}–{eff * hi // 100} 字', e.note_line))
    stats = {'answered': len(rows), 'rows': rows}
    if te:
        total, exp = 100 * ta / te, expect / te
        stats.update(total=round(total, 1), expected=round(exp, 1))
        if len(rows) == len(usable):
            if total - exp > D['bias_warn']:
                issues.append(Issue('WARN', '压缩比', f'全文实际 {total:.1f}%，比档位中值期望 {exp:.1f}% 高 {total - exp:.1f}pp，整体偏满', '复查是否在转述而非提炼'))
            elif exp - total > D['bias_warn']:
                issues.append(Issue('WARN', '压缩比', f'全文实际 {total:.1f}%，比档位中值期望 {exp:.1f}% 低 {exp - total:.1f}pp，整体偏简', '复查承重细节是否丢失'))
            band = FORMS.get(note.form)
            if band and not band[0] <= total <= band[1]:
                issues.append(Issue('WARN', '压缩比', f'全文实际 {total:.1f}%，不在“{note.form}”参考带 {band[0]}–{band[1]}%', '复查档位分布；合理则继续'))
    return issues, stats


def check_verify(note, usable):
    issues = []
    for e in usable:
        v = e.verify
        if v is None:
            issues.append(Issue('ERROR', '核验', f'Q{e.idx} 尚未核验', f'verify {note.path.name} Q{e.idx}', e.note_line))
        elif v['status'] == '待处置':
            n = sum(1 for x in v['items'] if x.startswith('-') and '→' not in x)
            tag = '已处置（复核）' if v['rechecked'] else '已处置'
            issues.append(Issue('ERROR', '核验', f'Q{e.idx} 有 {n} 条核验发现待处置', f'逐条处置：改答案或判为误报，在条目末尾写“→ 处置：…”，再把状态改为“{tag}”', v['line']))
        elif v['status'] == '已处置':
            bad = [x for x in v['items'] if x.startswith('-') and '→' not in x]
            if bad:
                issues.append(Issue('ERROR', '核验', f'Q{e.idx} 标为已处置，但有 {len(bad)} 条缺少“→ 处置：…”', '为每条补写处置说明', v['line']))
            elif not v['rechecked']:
                issues.append(Issue('ERROR', '核验', f'Q{e.idx} 已处置但尚未复核', f'verify {note.path.name} --recheck', v['line']))
        elif v['status'] == '失败':
            issues.append(Issue('ERROR', '核验', f'Q{e.idx} 核验调用失败', f'重跑 verify {note.path.name} Q{e.idx}', v['line']))
        elif v['status'] != '通过':
            issues.append(Issue('ERROR', '核验', f'Q{e.idx} 核验状态“{v["status"]}”无法识别', '状态只能是 通过 / 待处置 / 已处置', v['line']))
    return issues


def check_concepts(note):
    issues = []
    for mi, m in enumerate(note.modules):
        c = m['concept']
        if len(c) != 1:
            issues.append(Issue('ERROR', '概念', f'模块“{m["title"]}”概念行应恰好一行（实际 {len(c)}）', '模块末尾写“概念：[[分类#概念]], …”或“概念：无”', m['line']))
        if not c:
            continue
        end = note.modules[mi + 1]['line'] if mi + 1 < len(note.modules) else next(
            (i for i, s in enumerate(note.lines, 1) if L_RE.match(s)), len(note.lines) + 1)
        if any(s.strip() for s in note.lines[c[0][0]:end - 1]):
            issues.append(Issue('ERROR', '概念', f'模块“{m["title"]}”概念行之后还有内容', '概念行放在模块所有 Q&A 之后', c[0][0]))
        text = c[0][1]
        links = re.findall(r'\[\[([^\]]+)\]\]', text)
        if text.strip() != '概念：无' and not links:
            issues.append(Issue('ERROR', '概念', f'模块“{m["title"]}”概念行没有链接', '写 [[分类#概念]]，或“概念：无”', c[0][0]))
        seen = []
        for l in links:
            mm = LINK_RE.match('[[' + l + ']]')
            if not mm or not mm.group(2) or mm.group(1) not in CONCEPT_ORDER:
                issues.append(Issue('ERROR', '概念', f'概念链接不合法：[[{l}]]', f'写 [[分类#概念]]，分类为 {"/".join(CONCEPT_ORDER)}', c[0][0]))
                continue
            seen.append(CONCEPT_ORDER.index(mm.group(1)))
        if seen != sorted(seen):
            issues.append(Issue('ERROR', '概念', f'模块“{m["title"]}”概念分类顺序错误', '按 人物→学派与方法→核心概念→精神分析 排列', c[0][0]))
    return issues


def check_left(note):
    hits = [i for i, s in enumerate(note.lines, 1) if L_RE.match(s)]
    if len(hits) != 1:
        return [Issue('ERROR', "What's Left", f'“## L: What\'s Left”应恰好一次（实际 {len(hits)}）', '在文末保留唯一章节')]
    issues, seen = [], 0
    for i in range(hits[0] + 1, len(note.lines) + 1):
        s = note.lines[i - 1]
        if not s.strip():
            continue
        if s.startswith('#'):
            issues.append(Issue('ERROR', "What's Left", "What's Left 之后还有其他标题", "What's Left 必须位于文末", i))
            continue
        m = re.match(r'^- \[(书目|脉络|概念|话题)\] .+?\s?— \S.*$', s)
        if not m:
            issues.append(Issue('ERROR', "What's Left", '线索格式不合法', '写成“- [标签] 对象 — 深挖理由”（— 为 U+2014，后面有空格）', i))
            continue
        n = LABELS[m.group(1)]
        if n < seen:
            issues.append(Issue('ERROR', "What's Left", f'标签顺序倒退：{m.group(1)}', '按 书目→脉络→概念→话题 排列', i))
        seen = max(seen, n)
    return issues


# ── 阶段判断与 check ──────────────────────────────────────────

def detect_stage(note):
    if not note.entries and any(m['range'] for m in note.modules):
        return 'modules'
    if not note.entries:
        return 'modules' if note.modules else 'source'
    if not any(e.answered for e in note.entries):
        return 'ledger'
    if not all(e.answered for e in note.entries) or any(e.verify is None for e in note.entries):
        return 'draft'
    if any(e.verify['status'] not in ('通过', '已处置') for e in note.entries):
        return 'verify'
    if not all(m['concept'] for m in note.modules):
        return 'concepts' if any(m['concept'] for m in note.modules) else 'verify'
    if not any(L_RE.match(s) for s in note.lines):
        return 'left'
    return 'complete'


NEXT = {
    'source': '步骤 1：通读 raw（pack <笔记> --lines 1 200 分段读），写模块标题与 *模块范围：行 X–Y*。',
    'modules': '步骤 2：逐模块 pack <笔记> --module N，建立 Q 账本；全部建完后删除模块范围行。',
    'ledger': '步骤 3：逐条 pack <笔记> Qn（或 --module N）写答案，每写完一个模块跑一次 check。',
    'draft': '全部写完且无 ERROR 后，步骤 4：verify <笔记> --all，处置核验发现，再 verify <笔记> --recheck 复核一次。',
    'verify': '步骤 5：为每个模块写概念行并在 _concepts 回链，然后运行 links。',
    'concepts': "概念行齐全、links 通过后，步骤 6：在文末写 ## L: What's Left。",
    'left': "步骤 6：在文末写 ## L: What's Left。",
    'complete': '全部自动检查通过。按 WORKFLOW 收尾清单自查后交付。',
}


def run_check(path, raw, stage=None):
    note = parse_note(path)
    stage = stage or detect_stage(note)
    k = STAGES.index(stage)
    issues, stats, src = check_source(note), {}, None
    if stage != 'source':
        src = Source(raw or find_source(path))
        issues += check_raw_fresh(note, src)
    if stage == 'modules':
        issues += check_modules(note, src)
    elif stage != 'source':
        x, stats, usable = check_ledger(note, src, final=k > STAGES.index('ledger'))
        issues += x
        if k >= STAGES.index('draft'):
            x, stats['ratio'] = check_draft(note, src, usable)
            issues += x
        if k >= STAGES.index('verify'):
            issues += check_verify(note, usable)
        if k >= STAGES.index('concepts'):
            issues += check_concepts(note)
        if k >= STAGES.index('complete'):
            issues += check_left(note)
    return {'note': note, 'source': src, 'stage': stage, 'issues': issues, 'stats': stats}


def verdict(issues):
    return 'FAIL' if any(i.level == 'ERROR' for i in issues) else ('WARN' if issues else 'PASS')


def render(r):
    n, src, s = r['note'], r['source'], r['stats']
    print(f'笔记 {n.path} · 当前 {STAGE_NAMES[r["stage"]]} · Q={len(n.entries)} · 形式={n.form or "未写"}')
    if src:
        print(f'原文 {src.path} · {src.n} 行 · 有效字 {src.chars()}')
    if s.get('q'):
        print(f'账本 Q {s["q"]} 条 · 密度 {s["density"]} 字/Q · 最大 Q {s["max_q"]} 字 · 档位 {s["tiers"]}')
    rt = s.get('ratio', {})
    if rt.get('total') is not None:
        print(f'答案 已写 {rt["answered"]}/{s["q"]} · 全文实际 {rt["total"]}% · 档位中值期望 {rt["expected"]}%')
    for i in sorted(r['issues'], key=lambda x: (x.level != 'ERROR', x.line)):
        print(f'[{i.level}] {i.check}' + (f'（笔记行 {i.line}）' if i.line else '') + f' {i.msg}')
        if i.fix:
            print(f'    → {i.fix}')
    v = verdict(r['issues'])
    print(f'结论 {v} · {sum(i.level == "ERROR" for i in r["issues"])} ERROR / {sum(i.level == "WARN" for i in r["issues"])} WARN')
    if v != 'FAIL':
        nxt = '继续为未建账的模块建账（pack <笔记> --module N），全部建完后再跑 check。' if any(i.check == '进度' for i in r['issues']) else NEXT[r['stage']]
        print(f'下一步 {nxt}')


# ── pack / verify / show / strip ─────────────────────────────

def raw_block(src, lo, hi, ctx=0, cumulative=False):
    out, acc = [], 0
    for i in range(max(1, lo - ctx), min(src.n, hi + ctx) + 1):
        inside = lo <= i <= hi
        mark = ' ' if inside else '·'
        if cumulative:
            acc += src.line_chars(i) if inside else 0
            out.append(f'{i:>5}{mark}[{acc:>5}] {src.lines[i - 1]}')
        else:
            out.append(f'{i:>5}{mark} {src.lines[i - 1]}')
    return '\n'.join(out)


def anchors(src, lo, hi):
    text = '\n'.join(src.lines[lo - 1:hi])
    found = {}
    for pat in (r'《[^》]{1,30}》', r'“[^”]{2,12}”', r'(?<!\d)(?:1[5-9]|20)\d{2}(?!\d)', r'[A-Za-z][A-Za-z\'\-]{3,}(?: [A-Z][a-z]+)*'):
        for x in re.findall(pat, text):
            if x.lower() not in STOPWORDS:
                found[x] = found.get(x, 0) + 1
    return sorted(found, key=lambda k: -found[k])[:20]


def select(note, specs, module=None):
    if module is not None:
        if not 1 <= module <= len(note.modules):
            raise SystemExit(f'模块序号应在 1–{len(note.modules)}')
        title = note.modules[module - 1]['title']
        return [e for e in note.entries if e.module == title]
    out = []
    for sp in specs:
        m = re.fullmatch(r'Q(\d+)(?:-Q?(\d+))?', sp, re.I)
        if not m:
            raise SystemExit(f'无法识别 {sp}，用 Q12 或 Q12-Q15')
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        out += [e for e in note.entries if a <= e.idx <= b]
    return out


SPEAKER_RE = re.compile(r'^\s*(\d+号讲话人|发言人\s*\d+)')


def speaker_at(src, i):
    for j in range(i, 0, -1):
        m = SPEAKER_RE.match(src.lines[j - 1])
        if m:
            return f'{m.group(1)}（标号在第 {j} 行）'
    return '未标注'


def write_packet(e, src):
    eff = src.chars(e.lo, e.hi)
    lo, hi = TIERS[e.tier]
    return '\n'.join([
        f'━━ Q{e.idx} ━━ {e.q}',
        f'模块：{e.module}',
        f'范围：行 {e.lo}–{e.hi} · {e.tier}（{lo}–{hi}%）· raw 有效字 {eff} → 答案目标 {eff * lo // 100}–{eff * hi // 100} 字',
        f'关键词（必须逐字出现）：{"、".join(e.keywords)}',
        f'候选锚点（仅提示，可能是口水话或误转写）：{"、".join(anchors(src, e.lo, e.hi)) or "无"}',
        f'第 {e.lo} 行的发言人：{speaker_at(src, e.lo)}',
        '提醒：原文的限定语（可能、大概、应该、好像、似乎、类似于、某种程度上、我记得、我感觉）必须保留；只写范围内原文说过的话，不补附和、评语和推断。',
        '原文（· 为前后语境行，只帮助理解，不计入分母）：',
        raw_block(src, e.lo, e.hi, ctx=2),
    ])


VERIFY_PROMPT = '''你是讲座笔记的核验员。下面是一段讲座/对谈的原始转写（带行号）和笔记作者根据它写的答案。
你的唯一任务：找出答案中与原文不符的地方。不要评价文笔，不要提风格建议，不要改写答案。

只报告以下四类问题：
- [无依据]：答案陈述了原文没有的事实、例子、数字、推断或评价。作者按“转写错误”说明还原的人名、术语，以及为人物补的公认身份（如“法国哲学家”），不算。答案与原文字面不同、但可能是对口语误转写的还原、头部又没有登记时，报为 [无依据] 并在说明开头写“疑似转写还原：”
- [归属]：把 A 说的话写成 B 说的；或把讲者转述他人的观点写成讲者自己的观点，反之亦然
- [曲解]：意思被改变、因果被颠倒、讲者的限定或不确定语气被去掉
- [遗漏]：只在本 Q 行范围内被删掉的内容会让答案的结论变得错误或误导时报告；普通的压缩省略不算；问题标题涉及、但不在本 Q 行范围内的内容，属于别的 Q，不算遗漏

以下情况不报：
- 原文是口语转写，有错字、重复和口水话；作者按上下文把它们还原成通顺的书面语
- 作者为人物补写的外文原名或全名（人名身份由作者另行查证）
- 作者对讲话人使用的人称代词
- 答案中“（转写错误：…）”“（内容存疑：…）”这类作者有意标注的括注
- 答案用到了语境行（· 标记）里的内容，只要与原文一致
- 你拿不准的地方。如果你的说明里需要写“无大碍”“基本一致”“拿不准”“仅提示”，就不要报这一条

输出格式（严格遵守，不要输出任何其他文字）。没有问题时只输出一行：
结论：通过
有问题时：
结论：待处置
- [类别] 「答案中的原句片段」— 简要说明，原文第 N 行怎么说

========== 笔记头部（讲话人身份与已知转写错误）==========
{header}

========== 原文 行 {lo}–{hi}（· 为语境行；第 {lo} 行的发言人：{speaker}）==========
讲话人以原文中的“N号讲话人 / 发言人N”标号为准，标号之后到下一个标号之前都是同一人的发言。
{raw}

========== 问题 ==========
{q}

========== 答案 ==========
{answer}
'''


def verify_packet(note, e, src):
    header = '\n'.join(f'{k}：{note.header(k) or "无"}' for k in ('对谈人', '转写错误', '内容存疑'))
    answer = '\n'.join(x for x in e.answer if x.strip())
    return VERIFY_PROMPT.format(header=header, lo=e.lo, hi=e.hi, raw=raw_block(src, e.lo, e.hi, ctx=2), q=e.q, answer=answer, speaker=speaker_at(src, e.lo))


def run_claude(prompt, model):
    try:
        r = subprocess.run(['claude', '-p', '--model', model, '--tools', '', '--no-session-persistence'],
                           input=prompt, capture_output=True, text=True, timeout=300, cwd='/tmp')
    except (OSError, subprocess.TimeoutExpired) as ex:
        return None, str(ex)
    if r.returncode:
        return None, (r.stderr or r.stdout).strip()[:200]
    return r.stdout.strip(), ''


def parse_verdict(out):
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    items = [re.sub(r'^[-*]\s*', '- ', l) for l in lines if re.match(r'^[-*]\s*\[(无依据|归属|曲解|遗漏)\]', l)
             and not re.search(r'不成立|请忽略|可忽略|撤回|无误|没有问题|并无问题|不构成问题|不报|与原文一致|基本一致|无大碍|拿不准|仅提示', l)]
    uniq = {}
    for x in items:
        uniq.setdefault(quote_key(x), x)
    items = list(uniq.values())
    if items:
        return '待处置', items
    if any(re.match(r'^\**结论\s*[:：]', l) and ('通过' in l or '待处置' in l) for l in lines):
        return '通过', []   # 待处置条目全部是自我否定的提示时，按通过处理
    return None, []


def quote_key(item):
    m = re.search(r'「([^」]+)」', item)
    return m.group(1)[:30] if m else item[:40]


def write_markers(path, results, recheck=False):
    """results: {Q 序号: (状态, 条目)}；替换或插入核验标记。从后往前改，行号不漂移。"""
    note = parse_note(path)
    lines = note.lines[:]
    for e in sorted(note.entries, key=lambda x: -x.note_line):
        if e.idx not in results:
            continue
        status, items = results[e.idx]
        if recheck and e.verify:
            old = {quote_key(x) for x in e.verify['items']}
            new = [x for x in items if quote_key(x) not in old]
            items = e.verify['items'] + new
            if status == '待处置' and not new:
                status = '通过'
            status = ('待处置' if status == '待处置' else '已处置') + '（复核）' if status != '失败' else '已处置'
        block = [f'<!-- 核验：{status} -->'] if not items else [f'<!-- 核验：{status}', *items, '-->']
        if e.verify:
            lines[e.verify['line'] - 1:e.verify['end']] = block
        else:
            at = e.key_line or e.meta_line
            lines[at:at] = block
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def cmd_verify(a):
    path = Path(a.note)
    note, src = parse_note(path), Source(Path(a.raw) if a.raw else find_source(path))
    if a.recheck:
        targets = [e for e in note.entries if e.usable and e.verify and e.verify['status'] == '已处置' and not e.verify['rechecked']]
        bad = [e.idx for e in note.entries if e.verify and e.verify['status'] == '待处置']
        if bad:
            print(f'Q{bad} 仍待处置，先处置再复核')
            return 1
    else:
        targets = note.entries if a.all else select(note, a.qs, a.module)
        targets = [e for e in targets if e.usable and e.answered and (a.force or e.verify is None or e.verify['status'] == '失败')]
    if not targets:
        print('没有需要核验的 Q（已核验的用 --force 重跑）')
        return 0
    if a.dry_run:
        for e in targets:
            print(verify_packet(note, e, src))
        return 0
    print(f'核验 {len(targets)} 条 Q × {a.passes} 次（model={a.model}, jobs={a.jobs}）…', flush=True)

    def one(job):
        e, _ = job
        out, err = run_claude(verify_packet(note, e, src), a.model)
        if out is None:
            return e.idx, ('失败', [f'调用失败：{err}'])
        status, items = parse_verdict(out)
        if status is None:
            return e.idx, ('失败', ['输出无法解析：' + out.replace('\n', ' ')[:200]])
        return e.idx, (status, items)

    # 每条 Q 独立核验 passes 次，合并发现：单次核验的召回有限，多次采样取并集
    jobs = [(e, k) for e in targets for k in range(a.passes)]
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        raw_results = list(ex.map(one, jobs))
    results = {}
    for idx, (status, items) in raw_results:
        prev = results.get(idx)
        if prev is None or prev[0] == '失败':
            results[idx] = (status, items) if status != '失败' or prev is None else prev
            continue
        if status in ('失败', '通过'):
            continue
        seen = {quote_key(x) for x in prev[1]}
        merged = prev[1] + [x for x in items if quote_key(x) not in seen]
        results[idx] = ('待处置', merged)
    write_markers(path, results, recheck=a.recheck)
    for idx in sorted(results):
        status, items = results[idx]
        print(f'Q{idx}: {status}' + (f'（{len(items)} 条）' if items else ''))
        for x in items:
            print(f'    {x}')
    print(f'已写回 {path}。待处置 {sum(s == "待处置" for s, _ in results.values())} 条 Q，失败 {sum(s == "失败" for s, _ in results.values())} 条 Q。')
    return 0


def cmd_pack(a):
    path = Path(a.note)
    note = parse_note(path)
    src = Source(Path(a.raw) if a.raw else find_source(path))
    if a.lines:
        lo, hi = a.lines[0], min(a.lines[1], src.n)
        print(f'raw 行 {lo}–{hi} · 有效字 {src.chars(lo, hi)} · 全文 {src.n} 行（方括号为区间内累计有效字）')
        print(raw_block(src, lo, hi, cumulative=True))
        return 0
    if a.module is not None and not a.verify:
        m = note.modules[a.module - 1] if 1 <= a.module <= len(note.modules) else None
        if m is None:
            raise SystemExit(f'模块序号应在 1–{len(note.modules)}')
        if not any(e.module == m['title'] for e in note.entries):
            if not m['range']:
                raise SystemExit('该模块既没有模块范围也没有 Q，无法定位 raw')
            lo, hi = m['range']
            L = CFG['ledger']
            print(f'━━ {m["title"]} ━━ 行 {lo}–{hi} · 有效字 {src.chars(lo, hi)}')
            print(f'建账提示：单 Q ≤ {L["q_cap"]} 字；全文平均 {L["density"][0]}–{L["density"][1]} 字/Q。方括号为模块内累计有效字。')
            print(raw_block(src, lo, hi, cumulative=True))
            return 0
    for e in select(note, a.qs, a.module):
        if not e.usable:
            print(f'Q{e.idx} 没有可用的行范围，跳过')
            continue
        print(verify_packet(note, e, src) if a.verify else write_packet(e, src))
        print()
    return 0


def cmd_show(a):
    path = Path(a.note)
    note = parse_note(path)
    src = Source(Path(a.raw) if a.raw else find_source(path))
    if a.line:
        n = a.line
        hits = [f'Q{e.idx}「{e.q[:30]}」（{e.module}，行 {e.lo}–{e.hi}）' for e in note.entries if e.usable and e.lo <= n <= e.hi]
        hits += [f'已舍弃内容（行 {lo}–{hi}）' for lo, hi in note.discards if lo <= n <= hi]
        hits += [f'{m["title"]}（模块范围 {m["range"][0]}–{m["range"][1]}）' for m in note.modules if m['range'] and m['range'][0] <= n <= m['range'][1]]
        print(f'raw 行 {n}：{src.lines[n - 1][:80] if 1 <= n <= src.n else "（越界）"}')
        print('归属：' + ('；'.join(hits) if hits else '未被任何 Q 或舍弃区间覆盖' + ('（机械噪声）' if n in src.noise else '')))
        return 0
    for e in select(note, a.qs, a.module):
        if not e.usable:
            continue
        eff, ans, actual = q_ratio(e, src)
        lo, hi = TIERS[e.tier]
        print(f'━━ Q{e.idx} ━━ {e.q}')
        print(f'行 {e.lo}–{e.hi} · {e.tier} {lo}–{hi}% · 实际 {actual:.0f}%（{ans}/{eff}）· 关键词 {"、".join(e.keywords)}')
        print('── raw ──')
        print(raw_block(src, e.lo, e.hi))
        print('── 答案 ──')
        print('\n'.join(x for x in e.answer if x.strip()) or '（空）')
        if e.verify:
            print(f'── 核验：{e.verify["status"]} ──')
            print('\n'.join(e.verify['items']))
        print()
    return 0


def cmd_strip(a):
    path = Path(a.note)
    note = parse_note(path)
    bad = [e.idx for e in note.entries if e.verify and e.verify['status'] not in ('通过', '已处置')]
    if bad:
        print(f'Q{bad} 的核验尚未处置，拒绝清理')
        return 1
    drop = set()
    for e in note.entries:
        if e.verify:
            drop.update(range(e.verify['line'], e.verify['end'] + 1))
    path.write_text('\n'.join(s for i, s in enumerate(note.lines, 1) if i not in drop) + '\n', encoding='utf-8')
    print(f'已删除 {len(drop)} 行核验标记')
    return 0


# ── links：概念双向链接 ─────────────────────────────────────

def drop_links(note_arg):
    stem = Path(note_arg).resolve().relative_to(ROOT).with_suffix('').as_posix()
    total = 0
    for cf in sorted((ROOT / '_concepts').glob('*.md')):
        lines = cf.read_text(encoding='utf-8').splitlines()
        keep = [s for s in lines if not (s.lstrip().startswith('- [[') and f'[[{stem}#' in s)]
        if len(keep) != len(lines):
            total += len(lines) - len(keep)
            cf.write_text('\n'.join(keep) + '\n', encoding='utf-8')
    print(f'已从 _concepts 删除 {total} 条指向 {stem} 的回链（标题保留）')
    return 0


def cmd_links(a):
    if a.drop:
        return drop_links(a.drop)
    link = re.compile(r'\[\[([^#|\]]+)#([^|\]]+)(?:\|[^\]]*)?\]\]')
    concept_side, note_side, sections, problems = set(), set(), {}, []
    for cf in sorted((ROOT / '_concepts').glob('*.md')):
        cur = None
        for s in cf.read_text(encoding='utf-8').splitlines():
            m = re.match(r'^##\s+(.+)$', s)
            if m:
                cur = m.group(1).strip()
                if cf.stem == '人物' and '待核' in cur:
                    problems.append(f'[人物待核] _concepts/人物.md ## {cur}：待核人物不建条目')
            elif cur:
                for np, ns in link.findall(s):
                    concept_side.add((cf.stem, cur, np, ns.strip()))
    for nd in sorted(d for d in ROOT.iterdir() if d.is_dir() and not d.name.startswith(('.', '_'))):
        for nf in sorted(nd.rglob('*.md')):
            if 'raw' in nf.parts:
                continue
            stem = nf.relative_to(ROOT).with_suffix('').as_posix()
            sections[stem], cur = set(), None
            for s in nf.read_text(encoding='utf-8').splitlines():
                m = re.match(r'^##\s+(.+)$', s)
                if m:
                    cur = m.group(1).strip()
                    sections[stem].add(cur)
                elif cur and CONCEPT_RE.match(s.strip()):
                    for cf, cn in link.findall(s):
                        note_side.add((stem, cur, cf, cn.strip()))
    for cf, cn, np, ns in sorted(concept_side):
        if (np, ns, cf, cn) in note_side:
            continue
        why = f'{np}.md 不存在' if np not in sections else (f'{np}.md 没有 ## {ns}' if ns not in sections[np] else f'该模块概念行缺 [[{cf}#{cn}]]')
        problems.append(f'[概念→笔记] _concepts/{cf}.md ## {cn} → {np}#{ns}：{why}')
    names = {(cf, cn) for cf, cn, _, _ in concept_side}
    for np, ns, cf, cn in sorted(note_side):
        if (cf, cn, np, ns) in concept_side:
            continue
        why = f'_concepts/{cf}.md 没有 ## {cn}' if (cf, cn) not in names else f'_concepts/{cf}.md ## {cn} 下缺少指向本模块的链接'
        problems.append(f'[笔记→概念] {np}#{ns} → [[{cf}#{cn}]]：{why}')
    if a.note:
        stem = Path(a.note).resolve().relative_to(ROOT).with_suffix('').as_posix()
        problems = [p for p in problems if stem in p or p.startswith('[人物待核]')]
    for p in problems:
        print(p)
    print(f'概念侧 {len(concept_side)} 条 · 笔记侧 {len(note_side)} 条 · 问题 {len(problems)} 个' + (f'（只看 {a.note}）' if a.note else ''))
    return 1 if problems else 0


# ── rebreak / 原文定位 ───────────────────────────

def find_source(p: Path):
    d = p.parent / 'raw'
    if not d.is_dir():
        raise FileNotFoundError(f'{d} 不存在，请用 --raw 指定原文')
    cs = sorted(d.glob('*.txt'))
    base = re.sub(r'_v\d+$', '', p.stem)
    for stem in (p.stem, base):
        for c in cs:
            if c.stem == stem:
                return c
    hits = [c for c in cs if c.stem.split('_')[0] == base.split('_')[0]]
    if len(hits) == 1:
        return hits[0]
    raise FileNotFoundError(f'无法唯一匹配 {p.name} 的原文，请用 --raw 指定')


def cmd_rebreak(a):
    new = Path(a.new)
    nt = new.read_text(encoding='utf-8')
    if a.old:
        ot, base = Path(a.old).read_text(encoding='utf-8'), a.old
    else:
        r = subprocess.run(['git', '-C', str(new.parent), 'show', f'HEAD:./{new.name}'], capture_output=True, text=True)
        if r.returncode:
            raise FileNotFoundError('没有 git 基线，请显式传入原版文件')
        ot, base = r.stdout, f'HEAD:{new.name}'
    x, y = re.sub(r'\s', '', ot), re.sub(r'\s', '', nt)
    print(f'基线 {base} · {len(ot.splitlines())} 行；现版 {new} · {len(nt.splitlines())} 行')
    if x == y:
        print('[PASS] 去除空白后逐字一致')
        if len(nt.splitlines()) < len(ot.splitlines()):
            print('[WARN] 行数减少，有行被合并，须重核行号')
        return 0
    i = next((i for i, (p, q) in enumerate(zip(x, y)) if p != q), min(len(x), len(y)))
    print(f'[FAIL] 第 {i + 1} 个非空白字符处分歧：基线“{x[i:i + 10]}” / 现版“{y[i:i + 10]}”')
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description='按 WORKFLOW.md 审计讲座笔记')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('check', help='自动判断阶段并累计审计')
    p.add_argument('notes', nargs='+')
    p.add_argument('--raw')
    p.add_argument('--stage', choices=STAGES, help='手动指定阶段（默认自动判断）')
    p.add_argument('--json', action='store_true')

    p = sub.add_parser('pack', help='输出 raw 切片 / 写作包 / 核验包')
    p.add_argument('note')
    p.add_argument('qs', nargs='*', help='Q12 或 Q12-Q15')
    p.add_argument('--module', type=int, help='模块序号（从 1 起）')
    p.add_argument('--lines', type=int, nargs=2, metavar=('LO', 'HI'))
    p.add_argument('--verify', action='store_true', help='输出核验包')
    p.add_argument('--raw')

    p = sub.add_parser('verify', help='用 claude -p 全新上下文逐条核验')
    p.add_argument('note')
    p.add_argument('qs', nargs='*')
    p.add_argument('--module', type=int)
    p.add_argument('--all', action='store_true')
    p.add_argument('--force', action='store_true', help='重跑已核验的 Q')
    p.add_argument('--recheck', action='store_true', help='复核：对已处置的 Q 再核验一次，新发现追加在原记录后')
    p.add_argument('--model', default='sonnet')
    p.add_argument('--jobs', type=int, default=6)
    p.add_argument('--passes', type=int, default=2, help='每条 Q 独立核验次数，发现取并集')
    p.add_argument('--dry-run', action='store_true', help='只打印核验包')
    p.add_argument('--raw')

    p = sub.add_parser('show', help='人工复核')
    p.add_argument('note')
    p.add_argument('qs', nargs='*')
    p.add_argument('--module', type=int)
    p.add_argument('--line', type=int, help='反查 raw 某行属于哪条 Q')
    p.add_argument('--raw')

    p = sub.add_parser('strip', help='删除已处置的核验标记')
    p.add_argument('note')

    p = sub.add_parser('links', help='检查概念双向链接')
    p.add_argument('note', nargs='?', help='只报告与该笔记有关的问题')
    p.add_argument('--drop', metavar='笔记.md', help='删除 _concepts 中指向该笔记的全部回链（重做笔记时用）')

    p = sub.add_parser('rebreak', help='校验 raw 改行只改变空白')
    p.add_argument('new')
    p.add_argument('old', nargs='?')


    a = ap.parse_args(argv)
    try:
        if a.cmd == 'check':
            rs = [run_check(Path(x), Path(a.raw) if a.raw else None, a.stage) for x in a.notes]
            if a.json:
                out = [{'note': str(r['note'].path), 'stage': r['stage'], 'verdict': verdict(r['issues']),
                        'stats': r['stats'], 'issues': [i.__dict__ for i in r['issues']]} for r in rs]
                print(json.dumps(out if len(out) > 1 else out[0], ensure_ascii=False, indent=2))
            else:
                for k, r in enumerate(rs):
                    if k:
                        print('\n' + '─' * 60)
                    render(r)
            return 1 if any(verdict(r['issues']) == 'FAIL' for r in rs) else 0
        return {'pack': cmd_pack, 'verify': cmd_verify, 'show': cmd_show, 'strip': cmd_strip,
                'links': cmd_links, 'rebreak': cmd_rebreak}[a.cmd](a)
    except (FileNotFoundError, OSError) as ex:
        print(f'[FATAL] {ex}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
