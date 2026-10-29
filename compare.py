"""
compare.py —— LitPulse 的【多篇对比矩阵】模块
=============================================
做的事（一句话）：把 N 篇论文各自"检索 → 结构化抽取"，最后汇成一张
「方法 / 数据集 / 指标 / 结果 / 结论」对比表，每个字段都带可溯源页码。

关键设计：复用单篇精读 reader 的能力，不另造轮子
  1. reader.build_or_load_index()  —— 逐页切块、带页码、有缓存
  2. reader.retrieve()             —— BM25 + 语义向量混合检索
  3. reader.llm()                  —— 统一的模型调用 / 网关 / key
为每个"对比维度"用一句定向 query 去该篇里检索最相关的片段（而不是把整篇
塞给模型），合并去重后让模型只依据这些片段抽取 JSON，并强制页码必须取自
片段标注，资料里没有就留空——延续单篇问答"不编造、可溯源"的原则。

用法：
  python3 compare.py 论文A.pdf 论文B.pdf 论文C.pdf            # 终端打印对比表
  python3 compare.py a.pdf b.pdf --save                      # 同时导出 Markdown
"""

import os
import re
import json
import datetime

import reader

# ----------------------------------------------------------------------
# 对比维度定义：键 = 输出 JSON 的字段；(中文名, 定向检索 query)
# query 用英文学术常用词，因为 arXiv 论文正文多为英文（混合检索里 BM25 吃英文词，
# 向量负责语义），命中文献里"讲这个维度"的段落。
# ----------------------------------------------------------------------
FIELDS = {
    "problem":    ("要解决的问题", "what problem does this paper address motivation objective goal"),
    "method":     ("核心方法",     "proposed method approach model framework architecture algorithm we propose"),
    "dataset":    ("数据集/实验",  "dataset benchmark experimental setup training data evaluation corpus"),
    "metrics":    ("评价指标",     "evaluation metrics measures accuracy precision recall F1 BLEU score"),
    "results":    ("关键结果",     "main results experimental findings performance numbers improvement outperform"),
    "conclusion": ("结论/局限",    "conclusion takeaways limitations future work summary"),
}

TOP_K_PER_FIELD = 3          # 每个维度取几个片段
MAX_CONTEXT_CHARS = 9000     # 喂给模型的合并上下文上限，避免超上下文/过慢

EXTRACT_PROMPT = """你是严谨的科研信息抽取助手。下面给出一篇论文中按主题检索到的【带页码原文片段】。
请只依据这些片段，抽取信息并输出一个 JSON 对象（不要输出任何其它文字、不要 markdown 代码块）。

JSON 结构严格如下：
{{
  "title": "论文的简短标题（中文或英文，不超过30字）",
  "problem":    {{"text": "它要解决什么问题/动机，中文一句话", "pages": [页码]}},
  "method":     {{"text": "核心方法/模型/做法，中文一到两句", "pages": [页码]}},
  "dataset":    {{"text": "用了什么数据集/实验对象/基准", "pages": [页码]}},
  "metrics":    {{"text": "用哪些评价指标衡量", "pages": [页码]}},
  "results":    {{"text": "关键结果/数值/提升，尽量具体", "pages": [页码]}},
  "conclusion": {{"text": "结论与局限，中文一句话", "pages": [页码]}}
}}

硬性要求：
1. pages 里的页码只能取自片段开头的【第X页】标注，绝对不许编造；该维度片段里没有信息时，
   text 填"资料中未明确提及"，pages 留空数组 []。
2. 每个 text 精炼清楚，保留关键英文术语、模型名、数据集名与数值。
3. 只输出 JSON。

带页码原文片段：
<<<
{context}
>>>
"""


def _field_context(index):
    """
    对该篇按各维度做混合检索，合并去重片段，返回 (拼接上下文, 命中详情, mode)。
    命中详情 {field: [(page, head, score, detail), ...]} 供网页展示/溯源。
    """
    chosen = {}          # 片段下标 -> 该片段文本（去重）
    field_hits = {}
    modes = set()
    for field, (_zh, query) in FIELDS.items():
        hits, mode = reader.retrieve(
            index, query, TOP_K_PER_FIELD, chunk_vecs=reader._get_vecs(index))
        modes.add(mode)
        rows = []
        for i, score, detail in hits:
            ch = index["chunks"][i]
            chosen.setdefault(i, ch["text"])
            rows.append((ch["page"], ch["text"][:80], round(score, 3), detail))
        field_hits[field] = rows

    # 按片段在原文中的顺序拼接（下标即切块顺序），并附上页码标注
    parts = []
    total = 0
    for i in sorted(chosen):
        page = index["chunks"][i]["page"]
        text = chosen[i]
        if total + len(text) > MAX_CONTEXT_CHARS:
            text = text[: max(0, MAX_CONTEXT_CHARS - total)]
        parts.append(f"【第{page}页】{text}")
        total += len(text)
        if total >= MAX_CONTEXT_CHARS:
            break
    mode = "hybrid" if "hybrid" in modes else "bm25"
    return "\n\n".join(parts), field_hits, mode


def _parse_json_lenient(raw):
    """模型有时会包 ```json 或带前后缀，容错地取出第一个完整 JSON 对象。"""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.MULTILINE).strip()
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, flags=re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def extract_one(index):
    """
    对单篇：多路检索 + 一次模型调用，抽取结构化字段。
    返回 (record, field_hits, mode)。record 的每个字段为 {text, pages}。
    单篇失败时 record["_error"] 写明原因，不拖垮其它论文。
    """
    context, field_hits, mode = _field_context(index)
    record = {"title": index.get("pdf", "未命名论文")}
    try:
        raw = reader.llm(EXTRACT_PROMPT.format(context=context), temperature=0)
        data = _parse_json_lenient(raw)
        record["title"] = data.get("title") or record["title"]
        for field in FIELDS:
            cell = data.get(field) or {}
            text = str(cell.get("text") or "资料中未明确提及")
            pages = cell.get("pages") or []
            pages = [int(p) for p in pages if isinstance(p, (int, float)) or str(p).isdigit()]
            record[field] = {"text": text, "pages": sorted(set(pages))}
    except Exception as e:
        record["_error"] = f"{type(e).__name__}: {e}"
        for field in FIELDS:
            record.setdefault(field, {"text": "（抽取失败）", "pages": []})
    return record, field_hits, mode


def compare_papers(pdf_paths, progress=None):
    """
    主入口：对 N 篇逐篇抽取并汇总。
    progress: 可选回调 progress(done, total, name)，供网页显示进度条。
    返回 {
      rows: [ {file, name, record, hits, mode} ... ],
      fields: FIELDS,
    }
    """
    rows = []
    total = len(pdf_paths)
    for k, path in enumerate(pdf_paths, 1):
        name = os.path.basename(path)
        if progress:
            progress(k - 1, total, name)
        index = reader.build_or_load_index(path)
        record, hits, mode = extract_one(index)
        rows.append({"file": name, "record": record, "hits": hits, "mode": mode})
        if progress:
            progress(k, total, name)
    return {"rows": rows, "fields": FIELDS}


# ----------------------------------------------------------------------
# 整库综述：map-reduce 的 reduce 阶段
#   map（上面 compare_papers）已把每篇压成统一"证据卡"；
#   reduce 再把 N 张证据卡一次性综合，写出跨论文的演进/共识/分歧/建议。
#   关键论断要求内联标注来源 [n:pX]（n=第几篇，X=该篇页码），可点回原文。
# ----------------------------------------------------------------------
_EMPTY_TOKENS = ("资料中未明确提及", "（抽取失败）", "")


def build_evidence_brief(result):
    """把各篇证据卡压成编号简报喂给 reduce 模型，并返回 {编号: 文件名} 映射。"""
    blocks, ref_map = [], {}
    for n, r in enumerate(result["rows"], 1):
        ref_map[n] = r["file"]
        rec = r["record"]
        title = rec.get("title") or r["file"]
        lines = [f"[{n}] 《{title}》"]
        for field, (zh, _q) in result["fields"].items():
            cell = rec.get(field, {"text": "", "pages": []})
            text = (cell.get("text") or "").strip()
            pages = cell.get("pages") or []
            if text in _EMPTY_TOKENS:
                continue
            pg = ("(p" + ",p".join(str(p) for p in pages) + ")") if pages else ""
            lines.append(f"- {zh}{pg}：{text}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), ref_map


REVIEW_PROMPT = """你是严谨的科研综述作者。下面给出 {n} 篇同主题论文的【证据卡】，每张卡开头的 [编号] 对应一篇论文，
括号里的 (p页码) 是该条信息在该论文中的出处页码。

请综合这些证据，写出跨论文的主题综述（不是逐篇复述），严格输出一个 JSON 对象，不要任何额外文字或 markdown 代码块：
{{
  "evolution": ["该主题方法/思路的发展脉络，每条一句，可多条"],
  "consensus": ["多篇论文的共同认识/一致结论，每条一句"],
  "disagreements": ["它们在方法、结论或适用场景上的分歧/差异，每条一句；若确实没有实质分歧，就放一条说明'现有资料未呈现明显分歧'"],
  "recommendations": ["给读者的选型/实践建议，每条一句，必须有证据支撑"]
}}

引用规则（务必遵守）：
1. 每个关键论断后用 [编号:p页码] 标注来源，编号和页码只能取自证据卡，例如证据写 [2] ...(p15) 才能标 [2:p15]；
2. 一条论断综合多篇时可连续标注，如 [1:p6][2:p3]；
3. 绝对不许编造编号或页码；证据不足的论断宁可不写，也不要硬加引用；
4. 全部用中文，保留关键英文术语。

证据卡：
<<<
{brief}
>>>
"""

# 内联引用标记：支持单页 [1:p15] 与同篇多页 [1:p4,p6,p8]（真实模型常合并多页）
CITE_RE = re.compile(r"\[(\d+)\s*:\s*((?:p?\d+)(?:\s*,\s*p?\d+)*)\]")


def cite_pages(group2):
    """从引用标记的页码部分（如 'p4,p6,p8' 或 '4, 6'）解析出整数页码列表。"""
    return [int(x) for x in re.findall(r"\d+", group2)]


def synthesize_review(result, progress=None):
    """
    reduce：一次模型调用生成四段综述。返回 dict（含 evolution/consensus/
    disagreements/recommendations 四个字符串列表，字符串内带 [n:pX] 标记）。
    """
    if progress:
        progress(0.2, "汇总各篇证据卡 ...")
    brief, ref_map = build_evidence_brief(result)
    if progress:
        progress(0.5, "生成跨论文综述 ...")
    raw = reader.llm(REVIEW_PROMPT.format(n=len(result["rows"]), brief=brief),
                     temperature=0.2)
    data = _parse_json_lenient(raw)
    review = {}
    for key in ("evolution", "consensus", "disagreements", "recommendations"):
        items = data.get(key) or []
        review[key] = [str(x).strip() for x in items if str(x).strip()]
    review["_ref_map"] = {int(k): v for k, v in ref_map.items()}
    if progress:
        progress(1.0, "综述完成")
    return review


REVIEW_SECTIONS = [
    ("evolution", "方法演进脉络"),
    ("consensus", "共识"),
    ("disagreements", "分歧 / 差异"),
    ("recommendations", "选型与实践建议"),
]


def review_to_markdown(result, review):
    """把综述渲染成 Markdown（纯文本引用 [n:pX]，适合导出/终端）。"""
    out = ["# LitPulse 整库综述（map-reduce）",
           f"_综合 {len(result['rows'])} 篇 ｜ 生成时间："
           f"{datetime.datetime.now():%Y-%m-%d %H:%M}_", ""]
    for key, title in REVIEW_SECTIONS:
        out.append(f"## {title}")
        items = review.get(key) or []
        if not items:
            out.append("- （无）")
        for it in items:
            out.append(f"- {it}")
        out.append("")
    out.append("## 引用对照表")
    for n, r in enumerate(result["rows"], 1):
        out.append(f"- [{n}] {r['record'].get('title') or r['file']}（{r['file']}）")
    return "\n".join(out) + "\n"


def to_markdown(result):
    """把结果渲染成一张 Markdown 对比表（字段为行、论文为列）。"""
    rows = result["rows"]
    fields = result["fields"]
    titles = [r["record"].get("title") or r["file"] for r in rows]

    head = "| 对比维度 | " + " | ".join(titles) + " |"
    sep = "|" + "---|" * (len(rows) + 1)
    lines = [f"# LitPulse 多篇对比矩阵（{len(rows)} 篇）",
             f"_生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}_", "", head, sep]
    for field, (zh, _q) in fields.items():
        cells = []
        for r in rows:
            cell = r["record"].get(field, {"text": "", "pages": []})
            pg = ("（" + "、".join(f"第{p}页" for p in cell.get("pages", [])) + "）") \
                if cell.get("pages") else ""
            cells.append(cell.get("text", "").replace("|", "/") + pg)
        lines.append(f"| **{zh}** | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def save_markdown(result, out_path=None):
    if out_path is None:
        os.makedirs("reports", exist_ok=True)
        out_path = os.path.join(
            "reports", f"compare_{datetime.datetime.now():%Y%m%d_%H%M}.md")
    md = to_markdown(result)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    return out_path, md


def save_review(result, review, out_path=None):
    if out_path is None:
        os.makedirs("reports", exist_ok=True)
        out_path = os.path.join(
            "reports", f"review_{datetime.datetime.now():%Y%m%d_%H%M}.md")
    md = review_to_markdown(result, review)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    return out_path, md


def valid_citations(result, review):
    """检查综述里的 [n:pX] 引用是否都合法（编号存在、页码在该篇被抽取页码内）。
    返回 (合法标记集合, 非法字符串列表)，用于防编造自检。"""
    allowed = set()
    for n, r in enumerate(result["rows"], 1):
        for cell in r["record"].values():
            if isinstance(cell, dict):
                for p in cell.get("pages", []):
                    allowed.add((n, int(p)))
    text = " ".join(
        it for key, _ in REVIEW_SECTIONS for it in review.get(key, []))
    bad = []
    for m in CITE_RE.finditer(text):
        n = int(m.group(1))
        for p in cite_pages(m.group(2)):
            if (n, p) not in allowed:
                bad.append(m.group(0))
    return allowed, sorted(set(bad))


# ----------------------------------------------------------------------
# 命令行入口
# ----------------------------------------------------------------------
def _print_terminal(result):
    rows = result["rows"]
    for k, r in enumerate(rows, 1):
        rec = r["record"]
        print("\n" + "=" * 78)
        print(f"[{k}] {rec.get('title') or r['file']}   （文件：{r['file']}，检索：{r['mode']}）")
        if rec.get("_error"):
            print("   ⚠️ 抽取出错：", rec["_error"])
        for field, (zh, _q) in result["fields"].items():
            cell = rec.get(field, {"text": "", "pages": []})
            pg = (" " + "/".join(f"p{p}" for p in cell.get("pages", []))) if cell.get("pages") else ""
            print(f"   · {zh:<8}: {cell.get('text','')}{pg}")
    print("\n" + "=" * 78)


def main():
    import sys
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    do_save = "--save" in args
    do_review = "--review" in args
    pdfs = [a for a in args if not a.startswith("--")]
    missing = [p for p in pdfs if not os.path.exists(p)]
    if missing:
        print("找不到文件：", ", ".join(missing))
        return

    def progress(done, total, name=""):
        if name:
            print(f"[{done}/{total}] 处理 {name} ...")

    result = compare_papers(pdfs, progress=lambda d, t, n: progress(d, t, n))
    _print_terminal(result)
    if do_save:
        path, _ = save_markdown(result)
        print(f"\n📝 对比表已导出：{path}")

    if do_review:
        print("\n" + "━" * 78 + "\n整库综述（map-reduce 的 reduce 阶段）\n" + "━" * 78)
        review = synthesize_review(result, progress=lambda f, msg="": print(f"  …{msg}"))
        for key, title in REVIEW_SECTIONS:
            print(f"\n{title}")
            for it in review.get(key) or []:
                print(f"  • {it}")
        _allowed, bad = valid_citations(result, review)
        if bad:
            print("\n⚠️ 发现疑似越界引用（页码不在抽取范围内，已提示）：", " ".join(bad))
        else:
            print("\n✅ 所有 [n:pX] 引用均来自各篇实际抽取页码")
        print("\n引用对照表：")
        for n, r in enumerate(result["rows"], 1):
            print(f"  [{n}] {r['record'].get('title') or r['file']}")
        if do_save:
            path, _ = save_review(result, review)
            print(f"\n📝 整库综述已导出：{path}")


if __name__ == "__main__":
    main()
