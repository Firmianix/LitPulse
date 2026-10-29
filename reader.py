"""
reader.py —— LitPulse 的【单篇精读】模块
=========================================
四个能力：
  1. 导入 PDF：逐页提取文字并切块，每块都记住【来源页码】，索引缓存到本地
  2. 自动精读笔记：一句话总结 / 大纲 / 核心论点 / 关键术语
  3. 带页码出处的问答：先检索最相关的几个片段，再让模型"只能依据片段作答并标注页码"
  4. 导出 Markdown：笔记和问答都能存成 .md

检索方案：本版用零依赖的 BM25（关键词相关性，类似搜索引擎的老底子算法），
          不装任何额外库即可跑；第 3 周学 RAG 时可平滑换成向量(Embedding)检索。

用法：
  python3 reader.py notes 论文.pdf            # 生成精读笔记（同时建立索引）
  python3 reader.py ask   论文.pdf "你的问题"  # 带页码出处的问答
"""

import os
import re
import sys
import json
import math
import hashlib
import datetime
from collections import Counter

from pypdf import PdfReader

import embeddings
from common import load_config

# 惰性初始化：等真正调用模型时才读配置建 client，
# 这样没填 Key 的用户也能正常打开网页，在侧边栏补配置即可。
_client = None
_model_name = None


def _get_client():
    global _client, _model_name
    if _client is None:
        _client, _model_name = load_config()
    return _client, _model_name

# 是否启用语义向量（装了 sentence-transformers 才为 True，否则自动降级 BM25）
USE_VECTORS = embeddings.AVAILABLE
VECTOR_WEIGHT = 0.6      # 混合检索中"语义向量"的权重，其余 0.4 给 BM25 关键词

# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
CHUNK_CHARS = 700        # 每个文本块的目标长度（字符）
CHUNK_OVERLAP = 120      # 相邻块重叠长度，避免句子被切断后丢失上下文
TOP_K = 5                # 问答时检索多少个片段
NOTES_CHAR_LIMIT = 30000  # 全文小于该长度就一次性生成笔记，否则分段汇总

DATA_DIR = "data"
CHUNK_DIR = os.path.join(DATA_DIR, "chunks")   # 切块索引缓存
NOTES_DIR = "notes"                            # 导出的精读笔记
os.makedirs(CHUNK_DIR, exist_ok=True)
os.makedirs(NOTES_DIR, exist_ok=True)


def pdf_stem(pdf_path):
    return os.path.splitext(os.path.basename(pdf_path))[0]


# ----------------------------------------------------------------------
# 1. 导入 PDF：逐页提取文字
# ----------------------------------------------------------------------
def extract_pages(pdf_path):
    """返回 [(页码从1开始, 该页纯文本), ...]，跳过几乎没有文字的页（如图封面）。"""
    reader = PdfReader(pdf_path)
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        text = re.sub(r"[ \t]+", " ", text)
        if len(text) >= 20:          # 过滤空页/纯图页
            pages.append((i, text))
    return pages


def chunk_pages(pages):
    """把每页文本切成带重叠的小块，每块记录 page（出处的关键）。"""
    chunks = []
    cid = 0
    for page_num, text in pages:
        text = text.replace("\n", " ")
        start = 0
        while start < len(text):
            end = min(start + CHUNK_CHARS, len(text))
            piece = text[start:end].strip()
            if piece:
                chunks.append({"id": cid, "page": page_num, "text": piece})
                cid += 1
            if end >= len(text):
                break
            start = end - CHUNK_OVERLAP      # 回退一段形成重叠
    return chunks


def build_or_load_index(pdf_path):
    """切块结果缓存到 data/chunks/<名>.json：第二次提问不用重新解析 PDF。"""
    stem = pdf_stem(pdf_path)
    cache = os.path.join(CHUNK_DIR, stem + ".json")
    if os.path.exists(cache):
        return json.load(open(cache, encoding="utf-8"))

    pages = extract_pages(pdf_path)
    chunks = chunk_pages(pages)
    payload = {
        "pdf": os.path.basename(pdf_path),
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "n_pages": len(pages),
        "chunks": chunks,
    }
    json.dump(payload, open(cache, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"   已解析 {len(pages)} 页、切出 {len(chunks)} 个片段，索引缓存：{cache}")
    return payload


# ----------------------------------------------------------------------
# 2. BM25 检索（零依赖；理解关键词检索的经典算法）
#    思路：一个词在某片段出现越多、而在所有片段里越稀有，这个片段就越相关。
# ----------------------------------------------------------------------
def tokenize(text):
    # 英文/数字按整词，中文按单字（对论文里大量英文术语足够有效）
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.lower())


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.tokens = [tokenize(d) for d in docs]
        self.doc_len = [len(t) for t in self.tokens]
        self.avgdl = sum(self.doc_len) / max(len(self.doc_len), 1)
        self.tf = [Counter(t) for t in self.tokens]
        df = Counter()
        for t in self.tokens:
            for term in set(t):
                df[term] += 1
        self.df = df
        self.N = len(docs)
        self.idf = {
            term: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
            for term, n in df.items()
        }

    def score_all(self, query):
        """返回每个文档的 BM25 原始分（长度 N 的列表），供混合检索融合。"""
        q_terms = tokenize(query)
        scores = [0.0] * self.N
        for term in q_terms:
            if term not in self.idf:
                continue
            idf = self.idf[term]
            for i in range(self.N):
                f = self.tf[i].get(term, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / max(self.avgdl, 1))
                scores[i] += idf * (f * (self.k1 + 1)) / denom
        return scores

    def search(self, query, top_k=TOP_K):
        scores = self.score_all(query)
        ranked = sorted(range(self.N), key=lambda i: -scores[i])
        return [(i, scores[i]) for i in ranked[:top_k] if scores[i] > 0]


def make_bm25(index):
    return BM25([c["text"] for c in index["chunks"]])


def _minmax(vals):
    """把一组分数线性归一化到 [0,1]；全相等时返回全 0。"""
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return [0.0 for _ in vals]
    return [(v - lo) / (hi - lo) for v in vals]


def hybrid_search(index, query, top_k=TOP_K, vector_weight=VECTOR_WEIGHT, chunk_vecs=None):
    """
    混合检索：BM25（关键词精确匹配）+ 向量（语义相似）。
    两路各自归一化到 [0,1] 后加权求和，兼顾"术语精确"与"语义相关"。
    若没有向量（未装库/未建索引），自动退化为纯 BM25。
    返回 [(片段下标, 融合分, 各路明细)]。
    """
    bm25 = make_bm25(index)
    bm_scores = bm25.score_all(query)

    if chunk_vecs is None or not USE_VECTORS:
        ranked = sorted(range(len(bm_scores)), key=lambda i: -bm_scores[i])
        return [(i, bm_scores[i], {"bm25": round(bm_scores[i], 3), "vec": None})
                for i in ranked[:top_k] if bm_scores[i] > 0]

    # 向量相似度（余弦，已归一化向量→点积）
    q_vec = embeddings.encode([query])[0]
    vec_scores = embeddings.cosine_scores(chunk_vecs, q_vec).tolist()

    bm_n = _minmax(bm_scores)
    vec_n = _minmax(vec_scores)
    fused = [(vector_weight * vec_n[i] + (1 - vector_weight) * bm_n[i])
             for i in range(len(bm_scores))]

    ranked = sorted(range(len(fused)), key=lambda i: -fused[i])
    out = []
    for i in ranked[:top_k]:
        if fused[i] <= 0 and bm_scores[i] <= 0:
            continue
        out.append((i, fused[i],
                    {"bm25": round(bm_n[i], 3), "vec": round(vec_n[i], 3)}))
    return out


def retrieve(index, query, top_k=TOP_K, chunk_vecs=None):
    """统一检索入口：返回 (hits, mode)。hits=[(下标, 分, 明细)]。"""
    if chunk_vecs is not None and USE_VECTORS:
        return hybrid_search(index, query, top_k, chunk_vecs=chunk_vecs), "hybrid"
    bm = make_bm25(index)
    return [(i, s, {"bm25": round(s, 3), "vec": None}) for i, s in bm.search(query, top_k)], "bm25"


# ----------------------------------------------------------------------
# 3. 生成精读笔记
# ----------------------------------------------------------------------
NOTES_PROMPT = """你是一位严谨的科研阅读教练。请基于下面的论文/资料内容，输出一份【中文精读笔记】，结构严格如下：

# 《{title}》精读笔记

## 一句话总结
（用一句话讲清这篇资料到底解决了什么问题、怎么做的、结论是什么）

## 内容大纲
（用层级列表梳理全文结构与脉络，5-10 条）

## 核心论点
（列出 3-7 个最重要的观点/方法/结论，每条一句话并简要解释）

## 关键术语
（用 Markdown 表格：| 术语 | 含义 |，挑出 5-10 个专业术语）

要求：只依据所给内容，不要编造资料里没有的数据或结论；术语保留必要的英文原词。

资料内容：
<<<
{content}
>>>
"""


def llm(prompt, temperature=0.2):
    c, model_name = _get_client()
    resp = c.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    return resp.choices[0].message.content.strip()


def reset_client():
    """网页里保存新配置后调用，丢弃旧 client，下次调用重建。"""
    global _client, _model_name
    _client, _model_name = None, None


def generate_notes(pdf_path, index):
    title = index["pdf"]
    full_text = "\n".join(c["text"] for c in index["chunks"])

    if len(full_text) <= NOTES_CHAR_LIMIT:
        notes = llm(NOTES_PROMPT.format(title=title, content=full_text))
    else:
        # 长文档：分段抽取要点（map），再综合成最终笔记（reduce）
        print(f"   资料较长（{len(full_text)} 字符），采用分段摘要再汇总 ...")
        step = NOTES_CHAR_LIMIT
        partials = []
        for k in range(0, len(full_text), step):
            seg = full_text[k:k + step]
            partials.append(llm(
                f"请为以下论文片段提取中文要点（论点/方法/术语/结论），尽量精炼：\n<<<\n{seg}\n>>>"))
        joined = "\n\n".join(partials)
        notes = llm(NOTES_PROMPT.format(title=title, content=joined))

    out_path = os.path.join(NOTES_DIR, pdf_stem(pdf_path) + "_精读笔记.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(notes)
    return out_path, notes


# ----------------------------------------------------------------------
# 4. 带页码出处的问答（RAG 的最小形态）
# ----------------------------------------------------------------------
ANSWER_PROMPT = """你是一个严谨的论文问答助手。只能依据【检索到的原文片段】回答问题。

要求：
1. 先直接回答问题；
2. 每个关键结论后用【第X页】标注出处页码（页码来自片段标注）；
3. 如果片段中没有答案，明确回答"资料中没有相关内容"，绝对不要编造；
4. 回答用中文，简洁清楚。

问题：{question}

检索到的原文片段：
{context}
"""

# 全文通读模式：检索 0 命中时（常见于中文提问 × 英文论文且未装语义向量库），
# 把全文直接交给 AI，仍要求标页码；AI 能跨语言理解问题与原文。
FULLTEXT_PROMPT = """你是一个严谨的论文问答助手。用户会用任意语言提问，你拿到的是这篇资料的全部文本（每段前有页码标注）。

要求：
1. 先直接回答问题；
2. 每个关键结论后用【第X页】标注出处页码（页码来自每段开头的标注）；
3. 通读全文后确实没有答案时，明确回答"资料中没有相关内容"，绝对不要编造；
4. 回答用中文，简洁清楚。

问题：{question}

资料全文（带页码）：
{context}
"""

# 全文模式的长度上限（字符）：绝大多数论文全文都能放下；超长时截断
MAX_FULLTEXT_CHARS = 30000


def answer_question(index, question):
    hits, mode = retrieve(index, question, TOP_K, chunk_vecs=_get_vecs(index))
    if not hits:
        # 0 命中回退：全文通读模式（中文提问 × 英文论文、未装语义向量库时
        # 纯 BM25 一个词都对不上）。把全文交给 AI，仍带页码。
        parts = []
        used = 0
        for c in index["chunks"]:
            seg = f"【第{c['page']}页】{c['text']}"
            if used + len(seg) > MAX_FULLTEXT_CHARS:
                parts.append(f"【第{c['page']}页】（后面内容过长已省略）")
                break
            parts.append(seg)
            used += len(seg)
        context = "\n\n".join(parts)
        answer = llm(FULLTEXT_PROMPT.format(question=question, context=context),
                     temperature=0)
        cited = []
        for m in re.finditer(r"【第(\d+)页】", answer):
            p = int(m.group(1))
            if p not in cited:
                cited.append(p)
        return (sorted(cited) or None, [], answer, "fulltext")

    context_parts = []
    for i, score, detail in hits:
        c = index["chunks"][i]
        context_parts.append(f"【第{c['page']}页】{c['text']}")
    context = "\n\n".join(context_parts)

    answer = llm(ANSWER_PROMPT.format(question=question, context=context), temperature=0)
    cited_pages = sorted({index["chunks"][i]["page"] for i, _, _ in hits})
    sources = [(index["chunks"][i]["page"], round(score, 3),
                index["chunks"][i]["text"][:80], detail)
               for i, score, detail in hits]
    return cited_pages, sources, answer, mode


# 模块级向量缓存：{资料stem: np.ndarray}，一次进程内只构建一次
_VEC_CACHE = {}


def _get_vecs(index):
    """按资料懒加载语义向量；不可用/未装库时返回 None（自动降级 BM25）。"""
    if not USE_VECTORS:
        return None
    stem = pdf_stem(index.get("pdf", "paper.pdf"))
    if stem not in _VEC_CACHE:
        try:
            _VEC_CACHE[stem] = embeddings.build_or_load_vectors(index, stem)
            print(f"   🧬 已启用向量检索（{embeddings.MODEL_NAME}）")
        except Exception as e:
            print(f"   ⚠️ 向量构建失败，回退 BM25：{e}")
            _VEC_CACHE[stem] = None
    return _VEC_CACHE[stem]


# ----------------------------------------------------------------------
# 5. 思考题互动：出题 / 看参考答案 / 点评用户作答（苏格拉底式引导）
# ----------------------------------------------------------------------
QUIZ_PROMPT = """你是一位擅长启发式教学的论文导师。请基于下面的论文内容，出 {n} 道【思考题】，
用来检验读者是否真正理解（不是能在原文直接抄到的填空题，而是需要理解、推理、联系或批判的题）。

只输出一个 JSON 数组，不要任何额外文字，每个元素形如：
{{"q": "思考题题干（中文）", "keypoints": ["回答应覆盖的要点1", "要点2", "要点3"]}}
- 每题给 2-4 个评分要点，要点要具体、可判断是否答到
- 难度有层次：至少 1 题涉及方法/结论，1 题涉及批判或延伸思考

论文内容：
<<<
{content}
>>>
"""

REFERENCE_PROMPT = """你是严谨的论文导师。请依据【原文片段】，为下面这道思考题写一份"参考答案要点"。

要求：
1. 用要点形式列出完整、正确的回答；
2. 关键结论后用【第X页】标注出处（页码取自片段标注）；
3. 片段不足以支撑的内容不要编造。

思考题：{question}

原文片段：
{context}
"""

REVIEW_PROMPT = """你是一位擅长【苏格拉底式教学】的论文导师，正在和学生一对一讨论。学生针对一道思考题给出了自己的理解。
请依据【原文片段】点评学生的回答，目标是帮学生自己想通，而不是简单宣布对错或直接灌输答案。

严格按下面四段输出（用这些小标题）：
【答到的点】先肯定学生答对的部分（没有就说"这部分还比较薄弱"），具体引用他的表述。
【理解偏差/遗漏】指出错误、混淆或漏掉的关键点；关键结论后标注【第X页】。语气鼓励，不否定人。
【参考答案要点】简洁给出正确要点，标注【第X页】。
【再想一想】最后提一个引导性问题，启发他深入（不要在这一段给答案）。

如果学生的回答明显离题或空白，也要温和地先给一个提示性问题，再给参考要点。

思考题：{question}
学生的回答：{user_answer}

原文片段：
{context}
"""


def _context_for(index, question, top_k=TOP_K):
    """对某个问题做检索，返回 (拼接好的带页码片段, 命中页码列表)。"""
    hits, _mode = retrieve(index, question, top_k, chunk_vecs=_get_vecs(index))
    parts = [f"【第{index['chunks'][i]['page']}页】{index['chunks'][i]['text']}"
             for i, _, _ in hits]
    pages = sorted({index["chunks"][i]["page"] for i, _, _ in hits})
    return "\n\n".join(parts), pages


def generate_quiz(index, n=5):
    """基于论文内容出思考题，返回 [{q, keypoints:[...]}]。"""
    full_text = " ".join(c["text"] for c in index["chunks"])
    # 控制长度，避免超上下文/过慢
    content = full_text[:24000]
    raw = llm(QUIZ_PROMPT.format(n=n, content=content), temperature=0.4)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    questions = json.loads(raw)
    # 为每道题用 BM25 附上可靠的出处页码（不依赖模型猜页码）
    for item in questions:
        _, pages = _context_for(index, item["q"])
        item["pages"] = pages
    return questions


def answer_thinking(index, question):
    """给出思考题的参考答案要点（带页码）。"""
    context, pages = _context_for(index, question)
    if not context:
        return [], "资料中没有检索到与该题相关的内容。"
    reference = llm(REFERENCE_PROMPT.format(question=question, context=context), temperature=0)
    return pages, reference


def review_user_answer(index, question, user_answer):
    """点评用户作答：肯定 + 纠偏 + 要点 + 引导问题。"""
    context, pages = _context_for(index, question)
    if not context:
        return [], "资料中没有检索到与该题相关的内容，无法基于论文点评。"
    review = llm(REVIEW_PROMPT.format(
        question=question, user_answer=user_answer, context=context), temperature=0.3)
    return pages, review


# ----------------------------------------------------------------------
# 命令行入口
# ----------------------------------------------------------------------
def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return

    command, pdf_path = sys.argv[1], sys.argv[2]
    if not os.path.exists(pdf_path):
        print(f"找不到文件：{pdf_path}")
        return

    if command == "notes":
        print(f"① 解析并建立索引：{pdf_path}")
        index = build_or_load_index(pdf_path)
        print("② 生成精读笔记（需要一点时间）...")
        out_path, notes = generate_notes(pdf_path, index)
        print(f"\n✅ 精读笔记已导出：{out_path}\n")
        print(notes[:800] + ("\n......" if len(notes) > 800 else ""))

    elif command == "ask":
        if len(sys.argv) < 4:
            print('请提供问题，例如：python3 reader.py ask paper.pdf "什么是 agent？"')
            return
        question = sys.argv[3]
        index = build_or_load_index(pdf_path)
        pages, sources, answer, mode = answer_question(index, question)
        print(f"\n❓ 问题：{question}（检索模式：{mode}）\n")
        print("💬 " + answer)
        if pages:
            print(f"\n📖 出处页码：{pages}")
            print("🔍 命中片段（页码 / 融合分 / BM25|语义 / 开头）：")
            for pg, score, head, detail in sources:
                print(f"   第{pg}页 [{round(score,3)}] bm25={detail.get('bm25')} "
                      f"vec={detail.get('vec')} | {head}...")

        # 问答也可追加导出
        if "--save" in sys.argv:
            log = os.path.join(NOTES_DIR, pdf_stem(pdf_path) + "_问答记录.md")
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"\n\n## {datetime.datetime.now():%Y-%m-%d %H:%M}\n\n"
                        f"**Q：{question}**\n\n{answer}\n\n出处页码：{pages}")
            print(f"\n📝 已追加到：{log}")
    elif command == "quiz":
        index = build_or_load_index(pdf_path)
        print("正在生成思考题 ...")
        quiz = generate_quiz(index, n=5)
        for k, item in enumerate(quiz, 1):
            print(f"\n{'='*70}\n思考题 {k}：{item['q']}")
            print(f"（相关页码：{item.get('pages', [])}）")
            print("评分要点：" + "；".join(item.get("keypoints", [])))
        print(f"\n共 {len(quiz)} 题。交互作答与导师点评请在网页端 app.py 进行。")

    else:
        print("未知命令，支持：notes / ask / quiz")


if __name__ == "__main__":
    main()
