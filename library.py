"""
library.py —— LitPulse 的【我的文库】：持久化的个人文献库
=============================================================
职责：
  1. 上传的每一篇 PDF 都拷贝进 library/ 长期保存（不再只是临时 uploads）；
  2. 自动提取并展示论文信息：标题 / 作者 / 日期 / 页数（本地元数据优先，
     缺失时可选地用模型从首页识别，没配模型也不影响入库与阅读）；
  3. 每篇可加【标注】：页码 + 原文摘录 + 我的批注，能一键跳回那一页；
  4. 元数据与标注集中存 data/library.json，PDF 原文存 library/<id>.pdf。

设计：纯标准库 + pypdf；AI 仅用于补全元数据（惰性调用 reader.llm）。
"""

import os
import re
import json
import uuid
import shutil
import datetime
import urllib.request
import urllib.error

from pypdf import PdfReader

LIBRARY_DIR = "library"
DATA_DIR = "data"
LIB_JSON = os.path.join(DATA_DIR, "library.json")

MAX_REMOTE_PDF = 200 * 1024 * 1024      # 远程 PDF 下载上限 200MB

os.makedirs(LIBRARY_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)


# ----------------------------------------------------------------------
# 存储：一个 JSON 文件记录全部条目
# ----------------------------------------------------------------------
def _load():
    if not os.path.exists(LIB_JSON):
        return {}
    try:
        return json.load(open(LIB_JSON, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data):
    tmp = LIB_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LIB_JSON)      # 原子替换，避免写一半损坏


def list_items():
    """返回全部文献，按加入时间倒序（新的在前）。"""
    items = list(_load().values())
    items.sort(key=lambda x: x.get("added_at", ""), reverse=True)
    return items


def get_item(item_id):
    return _load().get(item_id)


def _new_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]


def pdf_path(item_id):
    """文库里某篇 PDF 的磁盘路径。"""
    return os.path.join(LIBRARY_DIR, item_id + ".pdf")


# ----------------------------------------------------------------------
# 元数据：本地提取（pypdf 自带信息 + 首页文本启发式）
# ----------------------------------------------------------------------
def _parse_pdf_date(raw):
    """把 PDF 的 D:20250407142051Z 这类日期转成 'YYYY-MM-DD'，失败返回 ''。"""
    if not raw:
        return ""
    m = re.search(r"(\d{4})(\d{2})(\d{2})", str(raw))
    if m:
        y, mo, d = m.groups()
        try:
            return datetime.date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            return ""
    return ""


def _clean(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


# 作者单位/通讯作者常见上标；标题里出现这些通常是把作者行误并进了标题
_AFFIL_MARKS = "†‡§¶∗★☆•"

def _looks_like_author_block(s):
    """一段文本是否像『作者名单』：含邮箱/单位上标，或多个大写人名用逗号隔开。"""
    if not s:
        return False
    if "@" in s or re.search(r"[" + re.escape(_AFFIL_MARKS) + r"]", s):
        return True
    if s.count(",") >= 1 and len(set(re.findall(r"\b[A-Z][a-zA-Z]+", s))) >= 3:
        return True
    return False


def _title_is_dirty(t):
    """标题疑似把作者/首页噪声整块混了进来。"""
    t = _clean(t)
    if not t:
        return True
    if "@" in t or re.search(r"[" + re.escape(_AFFIL_MARKS) + r"]", t):
        return True
    if len(t) > 200:
        return True
    caps = set(re.findall(r"\b[A-Z][a-z]+\b", t))
    if t.count(",") >= 2 and len(caps) >= 4:  # Chris Cummins, Volker Seeker, ...
        return True
    return False


def clean_title(raw_title, authors=""):
    """
    清洗被污染的标题：
    1) 归一空白、去掉作者单位上标（†‡* 等）；
    2) 若标题尾部粘连了作者名单，用第一作者姓名做切点剥离；
    返回干净标题；若无法清理成可信标题，返回 ''（调用方应拒绝采用）。
    """
    t = _clean(raw_title)
    if not t:
        return ""
    original = t   # 保留上标的原文，用于识别“尾部是作者块”的硬信号

    first = re.split(r"[,;；、]", authors or "")[0].strip() if authors else ""
    candidates = []
    if first:
        candidates.append(first)            # Chris Cummins
        given = first.split()[0]
        if len(given) >= 3:
            candidates.append(given)       # Chris
    for name in candidates:
        start = original.find(name)
        while start != -1:
            head = original[:start].strip(" ,;·-—:")
            tail = original[start:].strip()
            # 硬信号：尾部（原文）带单位上标或邮箱；软信号：多个大写人名逗号串
            hard_signal = bool(
                "@" in tail or re.search(r"[" + re.escape(_AFFIL_MARKS) + r"]", tail))
            soft_author = _looks_like_author_block(tail) or tail.strip("†‡* ") == name
            position_ok = (head and len(head) >= 12
                           and (hard_signal or start >= len(original) * 0.5))
            if position_ok and (hard_signal or soft_author):
                t = head
                break
            start = original.find(name, start + 1)
        else:
            continue
        break

    # 剥离完成后，再清理标题头上的脚注/上标符号
    t = re.sub(r"[" + re.escape(_AFFIL_MARKS) + r"]+", "", t)
    t = re.sub(r"(?<![\w])[\*#]{1,2}(?=\s|$)", "", t)
    t = _clean(t).strip(" ,;·-—:")
    # 清理后仍像作者块/过短则判不可信
    if _title_is_dirty(t) or len(t) < 5:
        return ""
    return t[:200]


# 作者名后紧跟的“单位上标”：数字（1,12）、剑号/星号等（Ryan Koo1 / Chris Cummins †*）
# 姓名限定为「名 + 可选缩写首字母 + 单个姓」，避免把标题末尾的大写词吞进姓名。
_AUTHOR_AFFIX = re.compile(
    r"("
    r"[A-Z][\w'\-]+"                       # 名 First
    r"(?:\s+[A-Z]\.)?"                     # 可选缩写首字母，如 M.
    r"(?:\s+[A-Z][a-zA-Z'\-]+)"            # 姓 Last（恰好一个，不贪婪吞标题）
    r")\s*([0-9†‡*§¶∗]{1,3})(?![\w.])"
)

# 首页“标题/作者区”结束的标志
_HEADER_END = re.compile(r"\bAbstract\b|\bA\s?B\s?S\s?T\s?R\s?A\s?C\s?T\b|摘\s*要", re.I)


def split_dense_header(first_text):
    """
    处理 pypdf 把『标题+全部作者+单位+邮箱+Abstract』连成一行的情况。
    只在 Abstract 之前的“标题/作者区”里，用“第一个 人名+紧跟单位上标”切出标题。
    高置信、保守：切不出（前导太短/含邮箱或明显作者逗号串）就返回 ''，交给后续启发式/AI。
    返回标题字符串（可能为 ''）。
    """
    text = _clean(first_text)
    if not text:
        return ""
    # 只看 Abstract 之前的头部；没有 Abstract 标记则取前 320 字
    m = _HEADER_END.search(text)
    head = text[:m.start()] if m else text[:320]
    head = head.strip(" ,;·-—:")
    if not head:
        return ""

    am = _AUTHOR_AFFIX.search(head)
    if not am:
        return ""
    cand = head[:am.start()].strip(" ,;·-—:")

    # 前导必须确实像个标题：够长、不含邮箱/URL、不能本身已是“多个人名逗号串”
    if len(cand) < 12 or "@" in cand or "http" in cand.lower():
        return ""
    if cand.count(",") >= 2 and len(set(re.findall(r"\b[A-Z][a-z]+\b", cand))) >= 4:
        return ""
    # 标题一般不以介词/冠词收尾；切点落在头部太靠前（< 18%）也不可信
    if am.start() < len(head) * 0.18:
        return ""
    return clean_title(cand, "") or cand[:200]


def local_metadata(path):
    """只靠本地手段提取 {title, authors, date, pages, first_page_text}，不调用模型。"""
    r = PdfReader(path)
    pages = len(r.pages)
    title = authors = date = ""
    try:
        md = r.metadata
        if md:
            title = _clean(md.title) if getattr(md, "title", None) else ""
            authors = _clean(md.author) if getattr(md, "author", None) else ""
            date = _parse_pdf_date(md.get("/CreationDate"))
    except Exception:
        pass

    # 取前 3 页文本：封面通常只有大标题，作者/机构往往在第 2~3 页
    head_text = ""
    try:
        parts = []
        for p in r.pages[:3]:
            parts.append(_clean(p.extract_text() or ""))
        head_text = "\n".join(x for x in parts if x)[:4000]
    except Exception:
        pass

    # 标题启发式：元数据没有时，取首页前若干非空行里"最像标题"的一行
    first_text = head_text
    if not title and first_text:
        # 1) 优先处理“标题+作者+单位+Abstract 连成一整行”的密集首页（高置信切标题）
        dense = split_dense_header(first_text)
        if dense:
            title = dense
        else:
            # 2) 常规：在前若干行里找最像标题的一行
            lines = [ln.strip() for ln in re.split(r"[\r\n]+", first_text) if ln.strip()]
            for ln in lines[:10]:
                if (8 <= len(ln) <= 180
                        and not re.search(r"arxiv|preprint|doi|http|©|\bvol\b", ln, re.I)
                        and not _looks_like_author_block(ln)):
                    title = ln
                    break
            if not title and lines:
                cand = next((l for l in lines[:10]
                             if not _looks_like_author_block(l)), lines[0])
                title = cand[:180]
            title = clean_title(title, authors)  # 兜底清洗，剥不掉则置空等待 AI/联网补

    return {"title": title, "authors": authors, "date": date,
            "pages": pages, "head_text": head_text}


def import_pdf(src_path, original_name=None):
    """
    把一份 PDF 收入文库：拷贝原文 + 提取本地元数据。返回 (item_id, item)。
    已存在（按文件大小+原始名粗判）则直接返回旧条目，避免重复入库。
    """
    data = _load()
    original_name = original_name or os.path.basename(src_path)
    size = os.path.getsize(src_path)

    for iid, it in data.items():
        if it.get("original_name") == original_name and it.get("size") == size \
                and os.path.exists(pdf_path(iid)):
            return iid, it

    item_id = _new_id()
    dst = pdf_path(item_id)
    shutil.copyfile(src_path, dst)

    meta = local_metadata(dst)
    _title_fallback = not meta["title"]
    item = {
        "id": item_id,
        "file": os.path.basename(dst),
        "has_pdf": True,
        "original_name": original_name,
        "size": size,
        "title": meta["title"] or os.path.splitext(original_name)[0],
        "title_from": "filename" if _title_fallback else "",  # 标题是文件名回退（不可靠，允许 AI 修正）
        "authors": meta["authors"],
        "date": meta["date"],
        "pages": meta["pages"],
        "head_text": meta["head_text"],
        # 规范书目字段：上传的 PDF 暂缺，可后续用 DOI/标题联网补全
        "doi": "",
        "arxiv_id": "",
        "venue": "",
        "volume": "",
        "issue": "",
        "article_pages": "",
        "doc_type": "",
        "abstract": "",
        "source": "",
        "source_url": "",
        "pdf_url": "",
        "meta_source": "local",      # local / ai / online:*
        "tags": [],                  # 标签（一篇多个）
        "collections": [],           # 所属集合/文件夹（一篇可多个）
        "added_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "annotations": [],
    }
    data[item_id] = item
    _save(data)
    return item_id, item


# 字段级来源标记：ai（历史数据可能带）/ online（联网补全写入），供 UI 显示来源提示。
# v19.5：AI 从首页识别的补全入口（enrich_with_ai/needs_ai_enrich/_ENRICH_FIELDS）已删除，
# 补全统一走 lookup.resolve + enrich_online（arXiv/CrossRef，免模型 Key）。


def repair_dirty_titles():
    """
    一次性自愈：对库里『强信号脏标题』（带上标/邮箱、多逗号多人名、超长）
    用已知作者做确定性剥离。能剥出可信标题才改并落盘；剥不出（返回空）则保留，
    留给 AI 或联网补全。幂等：没有可修复项时不写盘。返回修复条数。
    """
    data = _load()
    changed = 0
    for item in data.values():
        t = item.get("title") or ""
        if t and _title_is_dirty(t):
            fixed = clean_title(t, item.get("authors", ""))
            if fixed and fixed != t:
                item["title"] = fixed
                changed += 1
                continue
        # 历史遗留空标题：若 head_text 是可高置信切分的密集首页，本地补回
        if not t and item.get("head_text"):
            guess = split_dense_header(item["head_text"])
            if guess:
                item["title"] = guess
                changed += 1
    if changed:
        _save(data)
    return changed


def _find_by_external_id(data, rec):
    """按 arxiv_id / DOI 找已有条目，避免重复入库。"""
    for iid, it in data.items():
        if rec.get("arxiv_id") and it.get("arxiv_id") == rec["arxiv_id"]:
            return iid, it
        if rec.get("doi") and (it.get("doi") or "").lower() == rec["doi"].lower():
            return iid, it
    return None, None


def _download_pdf(url, dst, timeout=60):
    """下载远程 PDF 到 dst；成功返回字节数，失败返回 None。"""
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "litpulse-tutorial/0.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read(MAX_REMOTE_PDF + 1)
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
    if len(data) > MAX_REMOTE_PDF:
        return None
    # 必须真的是 PDF（有些链接会返回 HTML 错误页）
    if not data.startswith(b"%PDF-") and "pdf" not in ctype.lower():
        return None
    tmp = dst + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)
    return len(data)


def import_record(rec, fetch_pdf=True):
    """
    把 lookup.resolve() 查出的一条记录收入文库。
    - 能下到开放 PDF（通常是 arXiv）就连原文一起存，并补本地页数；
    - 下不到（很多正式出版论文）就存成"纯元数据条目"，has_pdf=False。
    返回 (item_id, item)；已按 arxiv_id/DOI 去重。
    """
    data = _load()
    old_id, old = _find_by_external_id(data, rec)
    if old is not None:
        return old_id, old

    item_id = _new_id()
    size = 0
    pages = 0
    has_pdf = False

    if fetch_pdf and rec.get("pdf_url"):
        dst = pdf_path(item_id)
        size = _download_pdf(rec["pdf_url"], dst) or 0
        if size:
            has_pdf = True
            try:
                pages = len(PdfReader(dst).pages)
            except Exception:
                pages = 0

    file_name = (item_id + ".pdf") if has_pdf else ""
    item = {
        "id": item_id,
        "file": file_name,
        "has_pdf": has_pdf,
        "original_name": "",
        "size": size,
        "title": rec.get("title", "").strip() or "(未命名文献)",
        "authors": ", ".join(rec.get("authors", [])) if isinstance(
            rec.get("authors"), list) else (rec.get("authors") or ""),
        "date": rec.get("date", "")[:10],
        "pages": pages,
        # —— 新增的规范书目字段 ——
        "doi": rec.get("doi", ""),
        "arxiv_id": rec.get("arxiv_id", ""),
        "venue": rec.get("venue", ""),
        "volume": rec.get("volume", ""),
        "issue": rec.get("issue", ""),
        "article_pages": rec.get("article_pages", ""),
        "doc_type": rec.get("doc_type", ""),
        "abstract": (rec.get("abstract") or "")[:3000],
        "source": rec.get("source", ""),
        "source_url": rec.get("source_url", ""),
        "pdf_url": rec.get("pdf_url", ""),
        "meta_source": "online:" + rec.get("source", ""),
        "head_text": "",
        "tags": [],                  # 标签（一篇多个）
        "collections": [],           # 所属集合/文件夹（一篇可多个）
        "added_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "annotations": [],
    }
    data[item_id] = item
    _save(data)
    return item_id, item


def enrich_online(item_id, rec, overwrite=False):
    """
    用 lookup.resolve() 查到的【在线规范元数据】补全/更新一个已有条目。
    - 只补出版信息（标题/作者/年份/期刊会议/卷期页/DOI/类型/摘要/链接）；
    - 绝不动本地 PDF（file/has_pdf/size/pages）、精读笔记、按页标注；
    - overwrite=False（默认）：只填空字段，已有的不覆盖；
      overwrite=True：以在线信息为准覆盖出版字段（本地 PDF 相关仍不动）。
    返回 (item, changed_fields)；条目不存在返回 (None, [])。
    """
    data = _load()
    item = data.get(item_id)
    if not item:
        return None, []

    online = {
        "title": (rec.get("title") or "").strip(),
        "authors": (", ".join(rec.get("authors", []))
                    if isinstance(rec.get("authors"), list)
                    else (rec.get("authors") or "")),
        "date": (rec.get("date") or "")[:10],
        "doi": rec.get("doi", ""),
        "arxiv_id": rec.get("arxiv_id", ""),
        "venue": rec.get("venue", ""),
        "volume": rec.get("volume", ""),
        "issue": rec.get("issue", ""),
        "article_pages": rec.get("article_pages", ""),
        "doc_type": rec.get("doc_type", ""),
        "abstract": (rec.get("abstract") or "")[:3000],
        "source": rec.get("source", ""),
        "source_url": rec.get("source_url", ""),
        "pdf_url": rec.get("pdf_url", ""),
    }

    changed = []
    for k, v in online.items():
        if v in ("", None, [], ()):
            continue
        if overwrite or not str(item.get(k, "")).strip():
            if str(item.get(k, "")) != str(v):
                item[k] = v
                changed.append(k)
                # 字段级来源标记（用户手动联网补全，UI 据此提示“联网补全”）
                item.setdefault("field_source", {})[k] = "online"

    if changed:
        item["meta_source"] = "online:" + rec.get("source", "")
        data[item_id] = item
        _save(data)
    return item, changed


def delete_item(item_id):
    """从文库删除一篇（同时删 PDF 原文与记录）。"""
    data = _load()
    if item_id in data:
        p = pdf_path(item_id)
        if os.path.exists(p):
            os.remove(p)
        del data[item_id]
        _save(data)
        return True
    return False


# ----------------------------------------------------------------------
# 精读笔记：随论文条目一起长期保存，方便以后在文库里直接查看
# ----------------------------------------------------------------------
def save_notes(item_id, notes):
    """把生成的精读笔记（Markdown）存进该文库条目；返回更新后的条目或 None。"""
    data = _load()
    item = data.get(item_id)
    if not item:
        return None
    item["notes"] = str(notes or "")
    item["notes_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    _save(data)
    return item


# ----------------------------------------------------------------------
# 标注：页码 + 原文摘录 + 我的批注
# ----------------------------------------------------------------------
def add_annotation(item_id, page, quote, note):
    data = _load()
    item = data.get(item_id)
    if not item:
        return None
    ann = {
        "id": uuid.uuid4().hex[:8],
        "page": max(1, int(page)),
        "quote": _clean(quote)[:1000],
        "note": note.strip()[:2000],
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    item.setdefault("annotations", []).append(ann)
    item["annotations"].sort(key=lambda a: a["page"])
    _save(data)
    return ann


def delete_annotation(item_id, ann_id):
    data = _load()
    item = data.get(item_id)
    if not item:
        return False
    before = len(item.get("annotations", []))
    item["annotations"] = [a for a in item.get("annotations", []) if a["id"] != ann_id]
    if len(item["annotations"]) != before:
        _save(data)
        return True
    return False


# ======================================================================
# 标签 / 集合（文件夹）
# ======================================================================
def parse_labels(text):
    """把『逗号/顿号/分号/空白』分隔的一串标签解析成去重后的干净列表。"""
    parts = re.split(r"[,，、;；\s]+", text or "")
    out, seen = [], set()
    for p in parts:
        p = p.strip().lower()
        if p and p not in seen:
            seen.add(p)
            out.append(p.strip())
    return out


def _update_labels(item_id, field, values):
    data = _load()
    item = data.get(item_id)
    if not item:
        return None
    vals = []
    for v in values:
        v = str(v).strip()
        if v and v not in vals:
            vals.append(v)
    if item.get(field) != vals:
        item[field] = vals
        _save(data)
    return item


def set_tags(item_id, tags):
    """整体覆盖某篇的标签。tags 可以是字符串或列表。"""
    if isinstance(tags, str):
        tags = parse_labels(tags)
    return _update_labels(item_id, "tags", tags)


def set_collections(item_id, collections):
    if isinstance(collections, str):
        collections = parse_labels(collections)
    return _update_labels(item_id, "collections", collections)


def all_tags():
    counts = {}
    for it in list_items():
        for t in it.get("tags", []):
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))


def all_collections():
    counts = {}
    for it in list_items():
        for c in it.get("collections", []):
            counts[c] = counts.get(c, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))


# ======================================================================
# BibTeX 导出
# ======================================================================
_BIB_SPECIALS = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

def _bib_escape(s):
    s = "" if s is None else str(s)
    return "".join(_BIB_SPECIALS.get(ch, ch) for ch in s)


def _authors_bib(authors):
    """库内作者是『First Last, First Last』逗号串，转成 BibTeX 的 ' and ' 连接。"""
    if isinstance(authors, list):
        names = authors
    else:
        names = re.split(r"\s*(?:,|;|；|、)\s*", authors or "")
    names = [n.strip() for n in names if n.strip()]
    return " and ".join(names)


def _year_of(item):
    return (item.get("date") or "")[:4]


def _base_citation_key(item):
    authors = item.get("authors") or ""
    first = re.split(r"[,;；、]", authors)[0].strip() if authors else ""
    # 取第一作者“姓氏”：英文取最后一个词，中文取全名；无作者用 anon（学术惯例）
    last = first.split()[-1] if first else "anon"
    year = _year_of(item) or "nd"
    title = item.get("title") or item.get("original_name") or "untitled"
    # 标题第一个有意义的英文/数字词；中文标题取前两个字
    words = [w for w in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", title)
             if w.lower() not in {"the", "a", "an", "of", "on", "for", "and", "to"}]
    w0 = words[0] if words else "paper"
    if re.fullmatch(r"[\u4e00-\u9fff]+", w0):
        w0 = w0[:2]
    key = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]", "", f"{last}{year}{w0}")
    return key or f"ref{uuid.uuid4().hex[:6]}"


def citation_keys(items):
    """为一批条目生成互不重复的 citation key。"""
    used, result = set(), {}
    for it in items:
        base = _base_citation_key(it)
        key, suffix, i = base, "", 0
        while key in used:
            i += 1
            suffix = chr(ord("a") + i - 1)
            key = base + suffix
        used.add(key)
        result[it["id"]] = key
    return result


def _bib_pages(raw):
    # BibTeX 页码范围惯例用双连字符
    return re.sub(r"\s*-\s*", "--", (raw or "").strip())


def to_bibtex(item, key=None):
    """把单个文库条目转成一条 BibTeX 文本。"""
    key = key or _base_citation_key(item)
    dtype = item.get("doc_type") or ""
    year = _year_of(item)
    title = _bib_escape(item.get("title") or item.get("original_name") or "")
    authors = _bib_escape(_authors_bib(item.get("authors")))
    venue = _bib_escape(item.get("venue"))
    vol = _bib_escape(item.get("volume"))
    num = _bib_escape(item.get("issue"))
    pages = _bib_pages(item.get("article_pages"))
    doi = (item.get("doi") or "").strip()
    url = (item.get("source_url")
           or (("https://arxiv.org/abs/" + item["arxiv_id"]) if item.get("arxiv_id") else ""))

    fields = []
    def put(k, v):
        if v:
            fields.append(f"  {k} = {{{v}}}")

    # arXiv 预印本（或类型里含预印本）
    if item.get("arxiv_id") and ("预印本" in dtype or not venue):
        put("title", title); put("author", authors); put("year", year)
        put("eprint", item["arxiv_id"]); put("archivePrefix", "arXiv")
        if url:
            put("url", url)
        head = "misc"
    elif "会议" in dtype:
        put("title", title); put("author", authors); put("booktitle", venue)
        put("year", year); put("pages", pages); put("doi", doi); put("url", url)
        head = "inproceedings"
    elif "章节" in dtype:
        put("title", title); put("author", authors); put("booktitle", venue)
        put("year", year); put("pages", pages); put("volume", vol)
        put("doi", doi); put("url", url)
        head = "incollection"
    elif "图书" in dtype and "章节" not in dtype:
        put("title", title); put("author", authors); put("year", year)
        put("volume", vol); put("doi", doi); put("url", url)
        head = "book"
    elif "学位" in dtype:
        put("title", title); put("author", authors); put("school", venue)
        put("year", year); put("url", url)
        head = "phdthesis"
    elif venue:  # 默认有期刊名 -> article
        put("title", title); put("author", authors); put("journal", venue)
        put("year", year); put("volume", vol); put("number", num)
        put("pages", pages); put("doi", doi); put("url", url)
        head = "article"
    else:        # 信息不足，兜底 misc
        put("title", title); put("author", authors); put("year", year)
        put("howpublished", _bib_escape(url))
        head = "misc"

    body = ",\n".join(fields)
    return f"@{head}{{{key},\n{body}\n}}\n"


def export_bibtex(items):
    """一批条目 -> 完整 .bib 文本（key 去重，含文件头注释）。"""
    keys = citation_keys(items)
    chunks = [
        "% LitPulse 我的文库导出\n",
        f"% 共 {len(items)} 条 ｜ 导出时间 "
        f"{datetime.datetime.now().isoformat(timespec='seconds')}\n\n",
    ]
    for it in items:
        chunks.append(to_bibtex(it, keys[it["id"]]) + "\n")
    return "".join(chunks)
