#!/usr/bin/env python3
"""
双向链接检查脚本（模块级）

结构说明：
  _concepts/*.md：每个 ## 概念名 下列有指向笔记特定模块的链接
    格式：[[dir/filename#模块N：xxx|display]]
  笔记文件：每个 ## 模块N：xxx 末尾有回链
    格式：概念：[[概念文件#概念名]], ...

双向检查（精确到模块）：
  1. 概念侧 → 笔记侧：概念文件 ## 概念名 链接了 笔记#模块，
     则目标笔记与目标模块应存在，且该模块的 概念: 行中应包含 [[概念文件#概念名]]
  2. 笔记侧 → 概念侧：笔记 ## 模块 的 概念: 行中写了 [[概念文件#概念名]]，
     该概念文件 ## 概念名 下应包含指向 笔记#模块 的链接
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent

# 仅在这些黑名单目录之外的一级子目录中扫描笔记文件。
# 约定：
# - `_concepts/` 只存放“概念 -> 笔记模块”链接，不作为笔记目录扫描
# - `raw/`、`.git/`、`.obsidian/` 不参与双向链接检查
# - ROOT 下的一级 `.md` 文件也不视为笔记文件；只递归扫描一级子目录中的 `.md`
NOTE_DIR_BLACKLIST = {'_concepts', 'raw', '.git', '.obsidian'}

# ── 解析概念文件 ──────────────────────────────────────────────
# 返回: set of (cf_stem, concept_name, note_path, note_section)
#   note_path    形如 "bili/01_xxx"（无 .md）
#   note_section 形如 "模块一：欧洲批判理论的原初语境与预设读者"
def parse_concept_files():
    entries = set()
    heading_re = re.compile(r'^##\s+(.+)$')
    link_re    = re.compile(r'\[\[([^#|\]]+)#([^|\]]+)(?:\|[^\]]*)?\]\]')

    for cf in sorted((ROOT / '_concepts').glob('*.md')):
        cf_stem = cf.stem
        current_concept = None
        for line in cf.read_text(encoding='utf-8').splitlines():
            m = heading_re.match(line)
            if m:
                current_concept = m.group(1).strip()
                continue
            if current_concept is None:
                continue
            for note_path, note_section in link_re.findall(line):
                entries.add((cf_stem, current_concept, note_path, note_section.strip()))
    return entries

# ── 解析笔记文件 ──────────────────────────────────────────────
# 返回:
#   entries:
#     set of (note_path, note_section, cf_stem, concept_name)
#     表示“笔记模块 -> 概念”的回链条目
#   note_sections:
#     dict[note_path, set[note_section]]
#     收集每个笔记文件中出现过的所有二级标题，供“概念 -> 笔记”检查时区分
#     “目标笔记不存在 / 目标模块不存在 / 缺回链”
def parse_note_files():
    entries = set()
    heading_re      = re.compile(r'^##\s+(.+)$')
    concept_line_re = re.compile(r'概念[:：]')
    ref_re          = re.compile(r'\[\[([^#|\]]+)#([^|\]]+)(?:\|[^\]]*)?\]\]')
    note_sections   = {}

    note_dirs = [d for d in ROOT.iterdir()
                 if d.is_dir() and d.name not in NOTE_DIR_BLACKLIST]

    for nd in sorted(note_dirs):
        for nf in sorted(nd.rglob('*.md')):
            if 'raw' in nf.parts:
                continue
            note_stem = nf.relative_to(ROOT).with_suffix('').as_posix()
            note_sections.setdefault(note_stem, set())
            current_section = None
            for line in nf.read_text(encoding='utf-8').splitlines():
                m = heading_re.match(line)
                if m:
                    current_section = m.group(1).strip()
                    note_sections[note_stem].add(current_section)
                    continue
                if current_section is None:
                    continue
                if not concept_line_re.search(line):
                    continue
                for cf_stem, concept_name in ref_re.findall(line):
                    entries.add((note_stem, current_section, cf_stem, concept_name.strip()))
    return entries, note_sections

# ── 主检查逻辑 ────────────────────────────────────────────────
def main():
    concept_entries = parse_concept_files()
    note_entries, note_sections = parse_note_files()

    # 方便反查的索引
    # concept 侧：(cf_stem, concept_name, note_path) -> set of note_section
    concept_idx = {}
    for cf, cn, np, ns in concept_entries:
        concept_idx.setdefault((cf, cn, np), set()).add(ns)

    # note 侧：(note_path, note_section) -> set of (cf_stem, concept_name)
    note_idx = {}
    for np, ns, cf, cn in note_entries:
        note_idx.setdefault((np, ns), set()).add((cf, cn))

    print(f"概念文件共收录 {len(concept_entries)} 条（概念→笔记模块）链接")
    print(f"笔记文件共收录 {len(note_entries)} 条（笔记模块→概念）回链\n")

    count = 0

    # 1. 概念 → 笔记：概念文件中每条链接，目标笔记与目标模块是否存在，
    #    且对应笔记模块是否有回链？
    for (cf, cn, np, ns) in sorted(concept_entries):
        if (cf, cn) in note_idx.get((np, ns), set()):
            continue

        sections = note_sections.get(np)
        if sections is None:
            label = "目标笔记不存在"
            detail = f"{np}.md 不存在，或不在笔记扫描范围内"
        elif ns not in sections:
            label = "目标模块不存在"
            detail = f"{np}.md 中不存在 ## {ns}"
        else:
            label = "缺回链"
            detail = f"该模块的 概念: 行中缺 [[{cf}#{cn}]]"

        count += 1
        print(f"[{count}] [概念→笔记 {label}]\n"
              f"  _concepts/{cf}.md  ## {cn}\n"
              f"    → {np}.md  ## {ns}\n"
              f"  但 {detail}\n")

    # 2. 笔记 → 概念：笔记模块中每条回链，概念文件是否存在该概念条目，
    #    且是否链接了该精确模块？
    for (np, ns, cf, cn) in sorted(note_entries):
        if (cf, cn, np, ns) in concept_entries:
            continue

        sections    = concept_idx.get((cf, cn, np))
        any_concept = any(k[0] == cf and k[1] == cn for k in concept_idx)

        if not any_concept:
            detail = f"_concepts/{cf}.md 中不存在 ## {cn}"
            label  = "概念条目不存在"
        elif sections is None:
            detail = f"_concepts/{cf}.md ## {cn} 未列出该笔记"
            label  = "缺正链"
        else:
            detail = (f"_concepts/{cf}.md ## {cn} 链接了该笔记的其他模块：\n"
                      f"    {', '.join(sorted(sections))}")
            label  = "缺正链"

        count += 1
        print(f"[{count}] [笔记→概念 {label}]\n"
              f"  {np}.md  ## {ns}\n"
              f"    → [[{cf}#{cn}]]\n"
              f"  但 {detail}\n")

    if count == 0:
        print("✓ 所有双向链接均正确，没有发现问题。")
    else:
        print(f"共发现 {count} 个问题。")
    return count

if __name__ == '__main__':
    sys.exit(1 if main() else 0)
