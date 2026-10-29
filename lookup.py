"""
lookup.py —— 粘贴 arXiv 链接 / DOI / 标题，联网查出规范文献元数据
================================================================
三种输入自动识别：
  1. arXiv：https://arxiv.org/abs/2610.29933 、…/pdf/… 、或裸编号 2610.29933(v1)
  2. DOI  ：https://doi.org/10.1145/1234567 、doi:10.xxxx/yyy 、或裸 10.xxxx/yyy
  3. 标题  ：其余一律按论文标题检索

数据源（都免费、无需 API key）：
  - arXiv API    ：预印本，通常能拿到开放 PDF
  - CrossRef API ：正式出版的期刊/会议，拿 DOI、期刊会议名、卷期页、文献类型

统一返回一条记录 dict（字段见 resolve() 末尾）。查不到时抛 LookupError。
纯标准库（urllib + xml + json），不引第三方依赖。
"""

import re
import json
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

USER_AGENT = "litpulse-tutorial/0.3 (litpulse; mailto:litpulse@example.com)"
TIMEOUT = 20

ARXIV_NS = {"a": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom"}

# CrossRef 的英文类型 → 中文文献类型
CROSSREF_TYPE_CN = {
    "journal-article": "期刊论文",
    "proceedings-article": "会议论文",
    "book": "图书",
    "book-chapter": "图书章节",
    "book-section": "图书章节",
    "edited-book": "图书",
    "monograph": "专著",
    "report": "报告",
    "dissertation": "学位论文",
    "preprint": "预印本",
    "review": "综述/评论",
    "reference-book": "参考书",
}


class LookupError(Exception):
    """查不到或网络失败时抛出，消息可直接展示给用户。"""


# ----------------------------------------------------------------------
# 0. 输入识别
# ----------------------------------------------------------------------
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:\.pdf)?/?$", re.I)
_ARXIV_BARE_RE = re.compile(r"(?:^|[^\d.])(\d{4}\.\d{4,5})(v\d+)?$")
_ARXIV_OLD_RE = re.compile(r"(?:^|[^\w-])([a-z-]+/\d{7})(v\d+)?$")
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
_DOI_URL_RE = re.compile(r"(?:dx\.)?doi\.org/(10\.\d{4,9}/[^\s?#]+)", re.I)


def detect_kind(text):
    """返回 ('arxiv', 值) / ('doi', 值) / ('title', 原文)。"""
    t = (text or "").strip()
    if not t:
        raise LookupError("内容是空的，粘贴一个 arXiv 链接、DOI 或论文标题。")

    m = _ARXIV_URL_RE.search(t)
    if m:
        return "arxiv", re.sub(r"v\d+$", "", m.group(1))
    low = t.lower()
    if "arxiv" in low and re.search(r"\d{4}\.\d{4,5}", t):
        m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", t)
        if m:
            return "arxiv", m.group(1)
    m = _ARXIV_BARE_RE.search(t)
    if m and re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", t.strip()):
        return "arxiv", m.group(1)
    m = _ARXIV_OLD_RE.search(t)
    if m and re.fullmatch(r"[a-z-]+/\d{7}(v\d+)?", t.strip()):
        return "arxiv", m.group(1)

    m = _DOI_URL_RE.search(t)
    if m:
        return "doi", m.group(1)
    m = _DOI_RE.search(t)
    if m:
        return "doi", m.group(0).rstrip(".,;)")

    return "title", t


# ----------------------------------------------------------------------
# 1. HTTP 小工具
# ----------------------------------------------------------------------
def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise LookupError(f"数据源返回错误（HTTP {e.code}），可能这条记录不存在。")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LookupError(f"连不上数据源：{e}。请检查网络后重试。")


# ----------------------------------------------------------------------
# 2. arXiv
# ----------------------------------------------------------------------
def _arxiv_entry_to_record(entry):
    def find_text(tag):
        node = entry.find(tag, ARXIV_NS)
        return " ".join(node.text.split()) if node is not None and node.text else ""

    id_url = find_text("a:id")                      # http://arxiv.org/abs/2610.29933v1
    arxiv_id = id_url.rstrip("/").split("/abs/")[-1]
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id)

    published = find_text("a:published")[:10]
    authors = [a.find("a:name", ARXIV_NS).text
               for a in entry.findall("a:author", ARXIV_NS)
               if a.find("a:name", ARXIV_NS) is not None]

    pdf_url = ""
    for link in entry.findall("a:link", ARXIV_NS):
        if link.get("title") == "pdf":
            pdf_url = link.get("href")
            break
    if not pdf_url:
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

    # arXiv 有时会回填正式发表信息（期刊/DOI）
    venue = ""
    jr = entry.find("arxiv:journal_ref", ARXIV_NS)
    if jr is not None and jr.text:
        venue = " ".join(jr.text.split())
    doi = ""
    dnode = entry.find("arxiv:doi", ARXIV_NS)
    if dnode is not None and dnode.text:
        doi = dnode.text.strip()

    return {
        "source": "arxiv",
        "title": find_text("a:title"),
        "authors": authors,
        "date": published,
        "abstract": find_text("a:summary"),
        "doi": doi,
        "arxiv_id": arxiv_id,
        "venue": venue,
        "volume": "",
        "issue": "",
        "article_pages": "",
        "doc_type": "预印本（arXiv）",
        "source_url": id_url,
        "pdf_url": pdf_url,
    }


def lookup_arxiv(arxiv_id_or_query, by_title=False):
    if by_title:
        q = urllib.parse.urlencode({
            "search_query": f'ti:"{arxiv_id_or_query}"',
            "start": 0, "max_results": 3,
        })
    else:
        q = urllib.parse.urlencode({"id_list": arxiv_id_or_query})
    raw = _get("http://export.arxiv.org/api/query?" + q)
    try:
        root = ET.fromstring(raw.decode("utf-8"))
    except ET.ParseError:
        raise LookupError("arXiv 返回内容无法解析。")
    entries = root.findall("a:entry", ARXIV_NS)
    if not entries:
        raise LookupError("arXiv 没找到匹配的论文。")
    # id_list 精确查询可能仍含空 entry（id 错误时 entry 里没有 id），过滤一下
    for e in entries:
        if e.find("a:id", ARXIV_NS) is not None:
            return _arxiv_entry_to_record(e)
    raise LookupError("arXiv 没找到这篇论文，请核对编号。")


# ----------------------------------------------------------------------
# 3. CrossRef（DOI / 标题 → 正式出版元数据）
# ----------------------------------------------------------------------
def _strip_jats(text):
    """CrossRef 的摘要常带 <jats:…> 标签，去标签留纯文本。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _crossref_msg_to_record(msg):
    title = ""
    if msg.get("title"):
        title = " ".join(str(msg["title"][0]).split())

    authors = []
    for a in msg.get("author", []) or []:
        name = " ".join(x for x in [a.get("given"), a.get("family")] if x)
        if not name and a.get("name"):
            name = a["name"]
        if name:
            authors.append(name)

    venue = ""
    ct = msg.get("container-title") or []
    if ct:
        venue = str(ct[0])

    year = ""
    for key in ("published-print", "published-online", "issued", "created"):
        dp = (msg.get(key) or {}).get("date-parts") or []
        if dp and dp[0] and dp[0][0]:
            year = str(dp[0][0])
            break

    raw_type = msg.get("type", "")
    doc_type = CROSSREF_TYPE_CN.get(raw_type, raw_type or "其他")
    doi = msg.get("DOI", "")

    return {
        "source": "crossref",
        "title": title,
        "authors": authors,
        "date": year,
        "abstract": _strip_jats(msg.get("abstract")),
        "doi": doi,
        "arxiv_id": "",
        "venue": venue,
        "volume": str(msg.get("volume") or ""),
        "issue": str(msg.get("issue") or ""),
        "article_pages": str(msg.get("page") or ""),
        "doc_type": doc_type,
        "source_url": f"https://doi.org/{doi}" if doi else "",
        "pdf_url": "",          # CrossRef 只给书目信息，不直接提供 PDF
    }


def lookup_crossref_by_doi(doi):
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi) \
          + "?mailto=litpulse@example.com"
    raw = _get(url)
    try:
        msg = json.loads(raw.decode("utf-8")).get("message", {})
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise LookupError("CrossRef 返回内容无法解析。")
    if not msg:
        raise LookupError("CrossRef 没找到这个 DOI。")
    return _crossref_msg_to_record(msg)


def lookup_crossref_by_title(title):
    q = urllib.parse.urlencode({
        "query.bibliographic": title, "rows": 3,
        "mailto": "litpulse@example.com",
    })
    raw = _get("https://api.crossref.org/works?" + q)
    try:
        items = json.loads(raw.decode("utf-8")).get("message", {}).get("items", [])
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise LookupError("CrossRef 返回内容无法解析。")
    if not items:
        raise LookupError("CrossRef 没找到匹配的论文。")
    return _crossref_msg_to_record(items[0])


# ----------------------------------------------------------------------
# 4. 统一入口：粘贴什么都能查
# ----------------------------------------------------------------------
def resolve(text):
    """
    自动识别输入并联网查询，返回一条规范记录 dict。
    标题优先搜 arXiv（CS 论文多、且常能下到 PDF），arXiv 没有再退到 CrossRef。
    """
    kind, value = detect_kind(text)

    if kind == "arxiv":
        return lookup_arxiv(value)

    if kind == "doi":
        return lookup_crossref_by_doi(value)

    # 标题：先 arXiv，失败再 CrossRef
    try:
        return lookup_arxiv(value, by_title=True)
    except LookupError as e_arxiv:
        try:
            return lookup_crossref_by_title(value)
        except LookupError:
            raise LookupError(f"arXiv 和 CrossRef 都没找到这篇论文。（arXiv：{e_arxiv}）")
