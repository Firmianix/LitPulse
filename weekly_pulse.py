"""
LitPulse（研究脉搏）—— MVP 第 2 版：arXiv 追新 + 模型打分 + 长期记忆去重 + 周报
=================================================================================
相比第 1 版新增：
  1. 【长期记忆】data/seen.json 记录每篇见过的论文（去重 + 状态：new/read/useful）
  2. 见过的论文不再重复打分、不再重复推送（本期只处理"新论文"）
  3. 配置与密钥分离：优先读环境变量 / .env，开源时不会泄露 key
  4. 周报增加"记忆统计"

配套：
  - mark.py：给论文打标记（read 读过 / useful 有用 / skip 忽略）
  - .env.example：配置模板（复制成 .env 填你自己的 key）

运行：python3 weekly_pulse.py
"""

import os
import re
import json
import time
import datetime
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

import common
from openai import OpenAI

# ----------------------------------------------------------------------
# 0. 配置加载：统一复用 common（网页侧边栏 / .env / 环境变量 / 教学机回退）
# ----------------------------------------------------------------------
def load_config():
    """返回 (api_key, base_url, model)，等价于 common.get_raw_config。"""
    return common.get_raw_config()


# ----------------------------------------------------------------------
# 1. 课题配置：改成你自己的研究方向（开源后建议也挪进 .env）
# ----------------------------------------------------------------------
RESEARCH_TOPIC = ""      # v19.7 起不再内置默认课题：必须由用户在网页上手动填写
ARXIV_KEYWORDS = []      # 示例可参考网页输入框的 placeholder（retrieval, augmented, generation）
MAX_PAPERS = 20
MIN_SCORE = 7

# 课题可在网页上修改，持久化到 data/topic.json；没有该文件时用上面的默认值
# （DATA_DIR 常量在下方定义，这里直接用同一路径）
TOPIC_PATH = "data/topic.json"


def parse_keywords(text):
    """把用户输入的关键词拆成列表：支持空格、逗号、中文逗号、分号分隔。"""
    parts = re.split(r"[\s,，;；、]+", str(text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def load_topic():
    """返回 {'topic': ..., 'keywords': [...]}；优先读 data/topic.json，没写过则为空（等用户填写）。"""
    topic, keywords = RESEARCH_TOPIC, list(ARXIV_KEYWORDS)
    try:
        with open(TOPIC_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("topic"):
            topic = str(saved["topic"]).strip()
        kws = saved.get("keywords")
        if isinstance(kws, list) and kws:
            keywords = [str(k).strip() for k in kws if str(k).strip()]
        elif isinstance(kws, str):
            keywords = parse_keywords(kws)
    except (OSError, ValueError):
        pass
    return {"topic": topic, "keywords": keywords}


def save_topic(topic, keywords):
    """保存研究课题到 data/topic.json。keywords 可为列表或字符串。"""
    topic = str(topic or "").strip()
    if not topic:
        raise ValueError("研究课题不能为空")
    if isinstance(keywords, str):
        keywords = parse_keywords(keywords)
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not keywords:
        raise ValueError("至少需要一个 arXiv 关键词")
    os.makedirs(os.path.dirname(TOPIC_PATH) or ".", exist_ok=True)
    tmp = TOPIC_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"topic": topic, "keywords": keywords}, f,
                  ensure_ascii=False, indent=2)
    os.replace(tmp, TOPIC_PATH)
    return {"topic": topic, "keywords": keywords}

ARXIV_NS = {"a": "http://www.w3.org/2005/Atom"}
DATA_DIR = "data"
SEEN_PATH = os.path.join(DATA_DIR, "seen.json")

# 惰性初始化：导入时不建 client，未配置 Key 也能正常打开网页
_client = None
_model = None


def _get_client():
    global _client, _model
    if _client is None:
        api_key, base_url, _model = load_config()
        if not api_key:
            raise RuntimeError(
                "还没有配置模型 Key。请在网页左侧【模型设置】里填一次，或在 .env 中配置。")
        _client = OpenAI(base_url=base_url or None, api_key=api_key)
    return _client, _model


def reset_client():
    """网页保存新配置后调用，丢弃旧 client，下次调用重建。"""
    global _client, _model
    _client, _model = None, None


# ----------------------------------------------------------------------
# 2. 长期记忆：seen.json
#    结构：{ "论文ID": {title, first_seen, score, relation, status, times_pushed,
#                      stance, stance_reason, stance_confirmed} }
#    status: new(新见) / read(已读) / useful(有用) / skip(忽略)
#    stance: support/oppose/method/data/related（AI 初判，用户可改）
STANCES = {
    "support": ("支持", "支持我的课题假设/方向价值"),
    "oppose":  ("质疑", "指出局限或反例，构成挑战"),
    "method":  ("方法可借鉴", "可借鉴的方法/架构/工程技巧"),
    "data":    ("数据可复用", "可复用的数据集/基准/代码"),
    "related": ("仅相关", "话题相关，价值一般"),
}


class PaperMemory:
    def __init__(self, path=SEEN_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            self.data = json.load(open(path, encoding="utf-8"))
        else:
            self.data = {}          # 首次运行，记忆为空

    def has(self, arxiv_id):
        return arxiv_id in self.data

    def get(self, arxiv_id):
        return self.data.get(arxiv_id)

    def remember(self, arxiv_id, paper, score=None, relation=None,
                 pushed=False, stance=None, stance_reason=None):
        """登记一篇论文；已存在则不覆盖用户的 status 与已确认的立场。"""
        today = datetime.date.today().isoformat()
        if arxiv_id not in self.data:
            self.data[arxiv_id] = {
                "title": paper["title"],
                "link": paper.get("link", ""),
                "published": paper.get("published", ""),
                "first_seen": today,
                "score": score,
                "relation": relation,
                "status": "new",
                "times_pushed": 0,
                "stance": stance or "related",
                "stance_reason": stance_reason or "",
                "stance_confirmed": False,
            }
        rec = self.data[arxiv_id]
        if score is not None:
            rec["score"] = score
        if relation:
            rec["relation"] = relation
        # AI 立场只写入"尚未被用户确认"的论文，避免覆盖人的判断
        if stance and not rec.get("stance_confirmed", False):
            rec["stance"] = stance
            rec["stance_reason"] = stance_reason or rec.get("stance_reason", "")
        if pushed:
            rec["times_pushed"] += 1
            rec["last_pushed"] = today

    def set_status(self, arxiv_id, status):
        if arxiv_id not in self.data:
            return False
        self.data[arxiv_id]["status"] = status
        return True

    def set_stance(self, arxiv_id, stance):
        """用户人工确认/修改立场：标记 stance_confirmed=True，之后 AI 不再覆盖。"""
        if arxiv_id not in self.data or stance not in STANCES:
            return False
        self.data[arxiv_id]["stance"] = stance
        self.data[arxiv_id]["stance_confirmed"] = True
        return True

    def counts(self):
        c = {"total": len(self.data), "new": 0, "read": 0, "useful": 0, "skip": 0}
        for rec in self.data.values():
            c[rec.get("status", "new")] = c.get(rec.get("status", "new"), 0) + 1
        return c

    def stance_counts(self):
        """按立场统计（只统计被推送过、相关性达标的论文更有意义，这里统计全部）。"""
        c = {k: 0 for k in STANCES}
        for rec in self.data.values():
            c[rec.get("stance", "related")] = c.get(rec.get("stance", "related"), 0) + 1
        return c

    def by_stance(self, stance, only_confirmed=False):
        """取出某一立场下的论文列表（按相关性降序）。"""
        out = [(pid, r) for pid, r in self.data.items() if r.get("stance") == stance]
        if only_confirmed:
            out = [(pid, r) for pid, r in out if r.get("stance_confirmed")]
        out.sort(key=lambda kv: -(kv[1].get("score") or 0))
        return out

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)


def arxiv_id_of(link):
    """从 http://arxiv.org/abs/2610.29933v1 提取稳定 ID：2610.29933（去掉版本号）。"""
    m = re.search(r"abs/([\d.]+?)(v\d+)?$", link)
    return m.group(1) if m else link


# ----------------------------------------------------------------------
# 3. 调 arXiv API 拉最新论文（XML）
# ----------------------------------------------------------------------
def fetch_arxiv(keywords, max_results=20):
    query = " AND ".join(f"all:{w}" for w in keywords)
    params = {
        "search_query": query, "start": 0, "max_results": max_results,
        "sortBy": "submittedDate", "sortOrder": "descending",
    }
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "litpulse-tutorial/0.2"})
    # arXiv 官方限流：单 IP 每 3 秒 1 次。429/5xx 时指数退避重试，避免偶发限流直接报错。
    last_err = None
    for attempt in range(4):
        if attempt:
            time.sleep(3 * attempt)          # 第 2/3/4 次尝试前等待 3s / 6s / 9s
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                root = ET.fromstring(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                last_err = e
                continue                       # 限流/服务端抖动 → 退避后重试
            raise                              # 404 等其他错误不重试，直接抛
    else:
        raise RuntimeError(
            "arXiv 接口暂时限流（HTTP 429），已自动重试 3 次仍失败。"
            "请等待 1–2 分钟再点扫描——arXiv 免费接口限制单 IP 每 3 秒 1 次请求。")

    papers = []
    for entry in root.findall("a:entry", ARXIV_NS):
        link = entry.find("a:id", ARXIV_NS).text
        papers.append({
            "id": arxiv_id_of(link),
            "title": " ".join(entry.find("a:title", ARXIV_NS).text.split()),
            "summary": " ".join(entry.find("a:summary", ARXIV_NS).text.split()),
            "published": entry.find("a:published", ARXIV_NS).text[:10],
            "link": link,
            "authors": [a.find("a:name", ARXIV_NS).text
                        for a in entry.findall("a:author", ARXIV_NS)][:3],
        })
    return papers


# ----------------------------------------------------------------------
# 4. 模型给【新论文】打分（见过的不再打分，省时省钱）
# ----------------------------------------------------------------------
def score_papers(topic, papers):
    if not papers:
        return {}
    listing = ""
    for i, p in enumerate(papers):
        listing += f"\n[{i}] 标题：{p['title']}\n摘要：{p['summary'][:600]}\n"

    prompt = f"""我的研究课题是：{topic}

下面是候选论文（编号、标题、摘要）。逐篇判断它【与我课题的相关性】以及【对我的立场/价值】。
只输出一个 JSON 数组，不要任何额外文字，每个元素形如：
{{"id": 0, "score": 8, "relation": "一句话说明和我课题的关系（中文，≤50字）",
  "stance": "method", "stance_reason": "为什么是这个立场（中文，≤40字）"}}

字段要求：
- score：0~10 整数（10 必读，0 无关），必须为每篇都评分。
- stance 只能取以下五个值之一：
    support = 结论支持/印证我的课题假设或该方向的价值；
    oppose  = 指出该方向的局限、失败或与预期相反，构成挑战/反例；
    method  = 提出我可以借鉴的方法、架构、检索或工程技巧；
    data    = 提供我可以复用的数据集、评测基准或代码；
    related = 仅话题相关，价值一般。
- score < 4 的，stance 一般填 related。

候选论文：{listing}
"""
    _cli, model_name = _get_client()
    resp = _cli.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    text = resp.choices[0].message.content.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    return {item["id"]: item for item in json.loads(text)}


# ----------------------------------------------------------------------
# 5. 生成周报
# ----------------------------------------------------------------------
def build_report(topic, picked, n_candidate, n_new, memory):
    today = datetime.date.today().isoformat()
    counts = memory.counts()
    stance_counts = memory.stance_counts()
    lines = [
        f"# LitPulse 研究周报（{today}）", "",
        f"**研究课题**：{topic}", "",
        f"本期扫描候选 **{n_candidate}** 篇，其中新论文 **{n_new}** 篇，"
        f"筛出高相关新论文 **{len(picked)}** 篇（阈值 ≥ {MIN_SCORE}）。", "",
        "---", "",
    ]
    if not picked:
        lines.append("本周没有达到阈值的新高相关论文（可能已在上期推过，或本周确实没有）。")
    for rank, (score, p, relation, stance, stance_reason) in enumerate(picked, 1):
        authors = ", ".join(p["authors"]) + (" 等" if len(p["authors"]) == 3 else "")
        stance_label = STANCES.get(stance, STANCES["related"])[0]
        lines += [
            f"## {rank}. {p['title']}", "",
            f"- **相关性**：{score}/10 ｜ **立场**：{stance_label} ｜ **发布**：{p['published']}",
            f"- **作者**：{authors}",
            f"- **为什么相关**：{relation}",
            f"- **立场理由**：{stance_reason}",
            f"- **链接**：{p['link']}",
            f"- **论文 ID**：`{p['id']}`（`python3 mark.py {p['id']} useful` 标记；"
            f"`python3 mark.py {p['id']} stance method` 确认立场）",
            "",
        ]

    # ---- 跨周累积的"立场地图"：以我的课题为中心，看证据如何站队 ----
    lines += ["---", "", "## 立场地图（跨周累积，以你的课题为中心）", ""]
    order = ["support", "oppose", "method", "data", "related"]
    lines.append("| 立场 | 篇数 | 含义 |")
    lines.append("|---|---|---|")
    for k in order:
        label, desc = STANCES[k]
        lines.append(f"| {label} | {stance_counts.get(k, 0)} | {desc} |")
    lines.append("")

    # 最该看的两类：质疑（避免踩坑/反例）和 方法可借鉴（直接能用）
    for stance in ("oppose", "method"):
        items = memory.by_stance(stance, only_confirmed=False)
        if not items:
            continue
        label = STANCES[stance][0]
        lines.append(f"### {label}（{len(items)} 篇，值得优先看）")
        for pid, r in items[:8]:
            tick = "已确认" if r.get("stance_confirmed") else "AI"
            lines.append(f"- {tick} **{r['title'][:90]}**（{r.get('score','?')}/10）"
                         f" — {r.get('stance_reason','')}")
        lines.append("")

    lines += [
        "---", "",
        f"## 记忆库状态",
        f"累计追踪 **{counts['total']}** 篇 ｜ 待读 {counts['new']} ｜ "
        f"已读 {counts['read']} ｜ 标记有用 {counts['useful']}", "",
        "> AI=初判立场，已确认=你核对过的立场。请点链接核对原文后再精读；"
        "可用 `mark.py <ID> stance support|oppose|method|data|related` 修正立场。",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main():
    memory = PaperMemory()
    topic_cfg = load_topic()
    topic = topic_cfg["topic"]
    keywords = topic_cfg["keywords"]
    if not topic or not keywords:
        raise SystemExit("尚未设置研究课题：请先在网页「追新周报」填写课题与 arXiv 关键词并保存。")
    print("① 从 arXiv 拉取最新论文 ...")
    papers = fetch_arxiv(keywords, MAX_PAPERS)

    # 关键：用长期记忆区分"新论文"和"已见过的"
    new_papers = [p for p in papers if not memory.has(p["id"])]
    seen_again = len(papers) - len(new_papers)
    print(f"   候选 {len(papers)} 篇，其中新论文 {len(new_papers)} 篇，"
          f"已在记忆库 {seen_again} 篇（自动跳过）")

    print("② 只对新论文打分 ...")
    scores = score_papers(topic, new_papers)

    picked = []
    for i, p in enumerate(new_papers):
        s = scores.get(i, {})
        score = s.get("score", 0)
        relation = s.get("relation", "")
        stance = s.get("stance", "related")
        stance_reason = s.get("stance_reason", "")
        if stance not in STANCES:
            stance = "related"
        is_picked = score >= MIN_SCORE
        # 无论是否入选，都记入长期记忆；入选的累加推送次数
        memory.remember(p["id"], p, score=score, relation=relation,
                       pushed=is_picked, stance=stance, stance_reason=stance_reason)
        if is_picked:
            picked.append((score, p, relation, stance, stance_reason))
    picked.sort(key=lambda x: -x[0])

    print("③ 生成周报并保存记忆 ...")
    report = build_report(topic, picked, len(papers), len(new_papers), memory)
    os.makedirs("reports", exist_ok=True)
    report_path = f"reports/pulse_{datetime.date.today().isoformat()}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    memory.save()

    print(f"\n✅ 完成！新高相关论文 {len(picked)} 篇")
    print(f"   周报：{report_path}")
    print(f"   记忆：{SEEN_PATH}（累计 {memory.counts()['total']} 篇）")


if __name__ == "__main__":
    main()
