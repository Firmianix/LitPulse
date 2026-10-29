"""
app.py —— LitPulse 手机端/电脑端网页（Streamlit）
==================================================
启动：streamlit run app.py
然后：
  - 本机浏览器会自动打开；
  - 手机使用：电脑和手机连同一个 WiFi，用命令行提示的 "Network URL"
    （形如 http://你电脑IP:8501）在手机浏览器打开即可；
  - 界面是响应式的，手机上自动单列、字号自适应。

本文件只负责"界面"，所有真本事都复用 reader.py / weekly_pulse.py。
"""

import os
import html
import json
import datetime
from urllib.parse import urlparse, quote as urlquote

import streamlit as st

import reader
import compare
import common
import library
import lookup
import weekly_pulse as wp
import pdfserve

# 启动应用内的 pdf.js / PDF 静态服务（幂等，守护线程）
PDF_PORT = pdfserve.ensure_started()
PDFJS_VIEWER = "static/pdfjs/web/viewer.html"

# 老数据自愈：把早期 AI 误并入作者的脏标题确定性剥离（幂等，干净库不写盘）
try:
    library.repair_dirty_titles()
except Exception:
    pass

# ----------------------------------------------------------------------
# 页面基础设置（手机优先的窄布局）
# ----------------------------------------------------------------------
st.set_page_config(
    page_title="LitPulse 研究脉搏",
    page_icon="📡",
    layout="centered",
    initial_sidebar_state="collapsed",   # 手机上默认收起侧栏
)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ----------------------------------------------------------------------
# 全局样式：字号收敛为 4 档（页标题 / 区块标题 / 正文 / 辅助说明），
# 白底浅灰圆角卡片，扁平极简。
# ----------------------------------------------------------------------
st.markdown(
    """
    <style>
    /* 字号四档 */
    h1, .stApp h1 { font-size: 1.55rem !important; font-weight: 700 !important; }
    h2, h3, h4, .stApp h2, .stApp h3, .stApp h4 {
        font-size: 1.18rem !important; font-weight: 600 !important; }
    .stApp, .stApp p, .stApp li { font-size: 0.98rem !important; }
    [data-testid="stCaptionContainer"] { font-size: 0.84rem !important; }

    /* 按钮统一小巧 */
    .stButton > button, .stFormSubmitButton > button {
        font-size: 0.9rem !important; padding: 0.35rem 0.9rem !important;
        border-radius: 8px !important; }

    /* 卡片：浅灰细边框圆角（expander / 提示框统一观感） */
    [data-testid="stExpander"] {
        border: 1px solid #e6e6e9 !important; border-radius: 10px !important;
        background: #fff; }
    [data-testid="stExpanderDetails"] summary { font-size: 0.95rem !important; }
    .stAlert { border-radius: 10px !important; }

    /* ===== 色系统一：青绿主色（浅蓝浅绿调）===== */
    /* 所有提示框（成功/警告/错误/信息）统一浅青绿底 + 青绿左边条 */
    .stAlert, [data-testid="stAlert"] {
        background: #f0faf8 !important;
        border: 1px solid #bfe8e2 !important;
        border-left: 4px solid #0f9d8f !important;
        border-radius: 10px !important;
        color: #1f3d38 !important;
    }
    .stAlert svg, [data-testid="stAlert"] svg { fill: #0f9d8f !important; }

    /* primary 按钮与进度条：青绿 */
    .stButton > button[kind="primary"] {
        background: #0f9d8f !important;
        border-color: #0f9d8f !important;
        color: #fff !important;
    }
    .stButton > button[kind="primary"]:hover {
        background: #0c8a7e !important;
        border-color: #0c8a7e !important;
    }
    .stProgress > div > div > div { background: #0f9d8f !important; }

    /* toast 提示 */
    [data-testid="stToast"] {
        background: #f0faf8 !important;
        border: 1px solid #bfe8e2 !important;
        color: #1f3d38 !important;
    }

    /* tab 下划线与选中色 */
    .stTabs [data-baseweb="tab-highlight"] { background-color: #0f9d8f !important; }
    .stTabs [data-baseweb="tab"][aria-selected="true"] { color: #0f9d8f !important; }

    /* 链接统一青绿、无下划线 */
    .stApp a { color: #0f9d8f; text-decoration: none; }

    /* 顶部标签页：紧凑 */
    .stTabs [data-baseweb="tab"] { font-size: 0.95rem !important; padding: 8px 14px; }
    .stTabs [data-baseweb="tab-highlight"] { height: 2px; }
    .stTabs [data-baseweb="tab-list"] { gap: 2px; border-bottom: 1px solid #eee; }
    </style>
    """,
    unsafe_allow_html=True,
)



def local_css():
    """让页面在手机上更宽松、按钮更大好点按。"""
    st.markdown("""
    <style>
      .block-container {padding-top: 1.2rem; padding-left: 1rem; padding-right: 1rem;}
      h1 {font-size: 1.6rem !important;}
      .stButton>button {width: 100%; min-height: 2.8rem; font-size: 1rem;}
      @media (min-width: 768px) {.stButton>button {width: auto;}}
    </style>
    """, unsafe_allow_html=True)


local_css()


def viewer_base_url():
    """根据浏览器当前访问地址，推导 pdf.js 服务的基址（手机/电脑自动同 host）。"""
    host = None
    try:
        u = st.context.url
        if u:
            host = urlparse(u).hostname
    except Exception:
        host = None
    if not host:
        import socket
        host = socket.gethostbyname(socket.gethostname())
    return f"http://{host}:{PDF_PORT}"


import re as _re

_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was",
         "were", "have", "has", "their", "they", "which", "when", "what",
         "into", "such", "than", "then", "these", "those", "our", "its",
         "can", "will", "may", "use", "using", "based", "paper", "method"}


def clean_phrase(text):
    """
    从片段中挑一个【最有辨识度的检索词】交给 pdf.js 高亮。
    实测：单个专有/技术术语（guardrails、tripwire、math_homework_tripwire）
    在 PDF 文本层（可能有断词、粘连）里命中率最高；长句/非相邻多词容易失配。
      - 英文：优先选含下划线/连字符/驼峰的技术词，其次较长的实词；
      - 中文：取开头一段连续汉字（中文按字即可精确短语匹配）。
    即使检索词失配，链接里的 #page= 仍保证准确跳页，高亮只是增强。
    """
    s = " ".join(str(text).split())

    # 中文为主：取连续汉字（6~12 个）
    cjk = _re.findall(r"[\u4e00-\u9fff]{6,12}", s)
    if cjk:
        return cjk[0]

    # 英文：切成候选 token（保留连字符/下划线，去掉其它标点）
    raw = _re.sub(r"[^\w\-]", " ", s)
    tokens = [t for t in raw.split() if len(t) >= 4]

    def score(tok):
        sc = 0
        if "_" in tok:
            sc += 5                 # math_homework_tripwire 这类最独特
        if "-" in tok:
            sc += 3                 # 复合技术词
        if _re.search(r"[a-z][A-Z]", tok):
            sc += 3                 # 驼峰命名
        if tok.isdigit():
            sc -= 5
        if tok.lower() in _STOP:
            sc -= 5
        sc += min(len(tok), 14) / 14.0   # 略偏好较长的词
        return sc

    good = [t for t in tokens if not t.isdigit() and t.lower() not in _STOP]
    if not good:
        good = tokens
    return max(good, key=score) if good else ""


def viewer_link(pdf_name, page, phrase="", source="uploads"):
    """生成 pdf.js 链接：跳到第 page 页并自动搜索高亮 phrase。
    source: 'uploads'（临时上传，只读）或 'library'（持久文库，可自动存回标注）。"""
    base = viewer_base_url()
    pdf_url = f"{base}/{source}/{urlquote(pdf_name)}"
    url = (f"{base}/{PDFJS_VIEWER}?file={urlquote(pdf_url, safe='')}"
           f"#page={int(page)}")
    phrase = clean_phrase(phrase)
    if phrase:
        url += f"&search={urlquote(phrase)}&phrase=true"
    if source == "library":
        # 文库文档附带一次性保存令牌，阅读器据此把画的标注自动存回
        item_id = pdf_name[:-4] if pdf_name.endswith(".pdf") else pdf_name
        token = pdfserve.new_save_token(item_id)
        url += (f"&lptoken={urlquote(token, safe='')}"
                f"&lpname={urlquote(pdf_name, safe='')}")
    return url


def source_chips(pdf_name, sources, source="uploads"):
    """把命中片段渲染成一排可点击的『第X页』，新标签打开 pdf.js。"""
    seen_pages, chips = set(), []
    for pg, _score, head, _detail in sources:
        if pg in seen_pages:
            continue
        seen_pages.add(pg)
        link = viewer_link(pdf_name, pg, head, source=source)
        chips.append(
            f'<a href="{link}" target="_blank" rel="noopener" '
            f'style="display:inline-block;margin:3px 6px 3px 0;padding:3px 10px;'
            f'border:1px solid #9fd8d0;border-radius:14px;font-size:0.86rem;background:#f2fbf9;'
            f'text-decoration:none;color:#0f9d8f;">第 {pg} 页</a>')
    if chips:
        st.markdown("点击页码跳回原文：<br>" + "".join(chips),
                    unsafe_allow_html=True)


def need_model():
    """AI 操作前置检查：未配置 Key 时给中文提示并终止本次运行（不抛英文异常）。"""
    if common.is_configured():
        return
    st.warning("这个功能需要模型：请先在左侧【模型设置】填入 API Key 并保存。")
    st.stop()


# ======================================================================
# 侧边栏：模型设置（大众用户在网页里填一次即可，无需手改 .env）
# ======================================================================
configured = common.is_configured()
with st.sidebar:
    st.header("模型设置")
    _ak, _bu, _md = common.get_raw_config()
    if configured:
        st.success("模型已配置")
        st.caption(common.active_provider_hint())
    else:
        st.warning("还没配置模型 Key")

    provider_names = [p["name"] for p in common.PROVIDERS]
    _idx = 0
    for _i, _p in enumerate(common.PROVIDERS):
        if _p["base_url"] and _p["base_url"] == _bu:
            _idx = _i
    sel = st.selectbox("模型服务商", provider_names, index=_idx, key="cfg_provider")
    api_key = st.text_input(
        "API Key", type="password", key="cfg_key",
        placeholder="粘贴 sk- 开头的 Key",
        help="Key 只保存在你本机的 .env 文件里，不会上传到任何第三方。")
    chosen = next(p for p in common.PROVIDERS if p["name"] == sel)
    if chosen["base_url"]:
        base_url, model = chosen["base_url"], chosen["model"]
        st.caption(f"接口地址：{base_url}")
        st.caption(f"模型：{model}")
    else:
        base_url = st.text_input("接口地址 Base URL", value=_bu or "", key="cfg_url")
        model = st.text_input("模型名称", value=_md or "", key="cfg_model")

    if st.button("保存配置", use_container_width=True, key="cfg_save"):
        if len(api_key.strip()) < 8:
            st.error("请先粘贴有效的 API Key（一般较长，sk- 开头）。")
        else:
            try:
                common.save_config(api_key.strip(), base_url,
                                   model or common.DEFAULT_MODEL)
                reader.reset_client()
                wp.reset_client()
                st.success("已保存并生效")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"保存失败：{e}")
    st.divider()
    st.caption("没有 Key？可到 [DeepSeek 开放平台](https://platform.deepseek.com) 注册，"
               "充值后即可使用。")

def _meta_badge(it, field):
    """字段来源徽标：AI 补全 / 联网补全（本地拿到的字段不标）。"""
    fs = (it.get("field_source") or {})
    if fs.get(field) == "ai":
        return "（AI 补全）"
    if fs.get(field) == "online":
        return "（联网补全）"
    return ""


st.title("LitPulse 研究脉搏")
st.caption("上传论文生成精读笔记 · 带页码出处问答 · 每周追新")

if not configured:
    st.warning("**首次使用请先在左侧【模型设置】填入 API Key 并保存**。\n\n"
               "保存前，上传 PDF、建索引等本地操作可以试；但**生成笔记、问答、思考题、"
               "多篇对比、整库综述、追新周报**等 AI 功能需要先配置模型。")

# 青绿统计卡片（追新库 / 我的文库 共用）
def _stat_card(col, label, value):
    col.markdown(
        f'<div style="border:1.5px solid #0f9d8f;border-radius:0.6rem;'
        f'padding:0.55rem 0.4rem;text-align:center;background:#f7fdfc;">'
        f'<div style="font-size:1.5rem;font-weight:700;color:#0f9d8f;'
        f'line-height:1.2;">{value}</div>'
        f'<div style="font-size:0.84rem;color:#4a6b66;margin-top:2px;">{label}</div>'
        f'</div>', unsafe_allow_html=True)

tab_read, tab_compare, tab_pulse, tab_lib, tab_mylib = st.tabs(
# ======================================================================

["精读论文", "多篇对比", "追新周报", "追新库", "我的文库"])

# ======================================================================
# 页面 1：精读论文（上传 → 笔记 → 问答）
# ======================================================================
with tab_read:
    uploaded = st.file_uploader(
        "上传一篇 PDF 论文（请选可复制文字的 PDF）",
        type=["pdf"],
        key="pdf_upload",
    )

    pdf_path = None
    lib_item = None
    if uploaded is not None:
        pdf_path = os.path.join(UPLOAD_DIR, uploaded.name)
        with open(pdf_path, "wb") as f:
            f.write(uploaded.getbuffer())
        # 同步收入持久文库（按 文件名+大小 幂等，重复上传不会产生副本）
        _lid, lib_item = library.import_pdf(pdf_path, uploaded.name)
        st.success(f"已接收并存入「我的文库」：{lib_item['title']}")

    if pdf_path and lib_item:
        # 换论文时清空上一篇的思考题状态（题目/作答/点评/参考都是"这一篇"的，
        # session_state 不会自动清，必须手动识别切换并重置）
        _this_quiz_paper = lib_item["id"]
        if st.session_state.get("_cur_quiz_paper") != _this_quiz_paper:
            st.session_state.pop("quiz", None)
            for k in list(st.session_state):
                if k.startswith(("quiz_reviewed_", "quiz_refed_", "user_answer_")):
                    st.session_state.pop(k)
            st.session_state["_cur_quiz_paper"] = _this_quiz_paper
        # ---- 论文信息卡片（标题/作者/日期/页数）----
        with st.container(border=True):
            st.markdown(f"#### {lib_item.get('title') or lib_item['original_name']}")
            m1, m2 = st.columns(2)
            m1.write(f"**作者**：{lib_item.get('authors') or '—'}"
                     f"{_meta_badge(lib_item, 'authors')}")
            m2.write(f"**日期**：{lib_item.get('date') or '—'}"
                     f"{_meta_badge(lib_item, 'date')}")
            mc1, mc2 = st.columns(2)
            mc1.write(f"**页数**：{lib_item.get('pages') or '—'}")
            _vj = lib_item.get("venue") or ""
            mc2.write(f"**期刊/会议**：{_vj or '—'}"
                      f"{_meta_badge(lib_item, 'venue') if _vj else ''}")
            _vd = lib_item.get("doi") or ""
            mv1, mv2 = st.columns(2)
            mv1.write(f"**DOI**：{_vd or '—'}"
                      f"{_meta_badge(lib_item, 'doi') if _vd else ''}")
            bcol1, bcol2 = st.columns(2)
            read_url = viewer_link(lib_item["file"], 1, source="library")
            bcol1.markdown(
                f'<a href="{read_url}" target="_blank" rel="noopener" '
                f'style="display:block;text-align:center;padding:0.45rem;'
                f'border:1px solid #0f9d8f;border-radius:0.5rem;color:#0f9d8f;'
                f'text-decoration:none;font-weight:600;">在线阅读原文</a>',
                unsafe_allow_html=True)
            # 信息齐全时按钮自动隐藏（v18 惯例）；联网查 arXiv/CrossRef 比首页识别更准
            _miss = [k for k in ("authors", "venue", "doi")
                     if not lib_item.get(k)]
            if _miss and bcol2.button("联网补全信息", use_container_width=True,
                                      help="用标题/DOI 联网查 arXiv / CrossRef，"
                                           "补作者、期刊、DOI；不消耗模型额度"):
                with st.spinner("正在联网查询 arXiv / CrossRef ..."):
                    _q = lib_item.get("doi") or lib_item.get("title") or ""
                    try:
                        _rec = lookup.resolve(_q)
                        lib_item, _chg = library.enrich_online(
                            lib_item["id"], _rec, overwrite=False)
                        st.toast("联网补全：" + ("、".join(_chg) if _chg else "无新信息"))
                    except lookup.LookupError as e:
                        st.toast(f"未查到：{e}")
                st.rerun()
            if st.button("生成精读笔记", use_container_width=True, type="primary",
                         disabled=not common.is_configured(),
                         help="AI 通读全文生成中文精读笔记（长文需几分钟）"):
                need_model()
                with st.spinner("正在阅读并生成中文精读笔记（长文需几分钟）..."):
                    idx = reader.build_or_load_index(pdf_path)
                    out_path, notes = reader.generate_notes(pdf_path, idx)
                # 精读笔记随论文一起存进我的文库，以后在文库里可直接查看
                library.save_notes(lib_item["id"], notes)
                st.toast("精读笔记已存入「我的文库」", icon=None)
                st.session_state["_just_notes"] = (lib_item["id"], notes, out_path)
                st.rerun()

        # 刚生成的笔记：显示在信息卡下方（只对同一篇论文生效）
        _jn = st.session_state.get("_just_notes")
        if _jn and lib_item and _jn[0] == lib_item["id"]:
            st.markdown(_jn[1])
            st.download_button(
                "下载笔记 Markdown",
                _jn[1],
                file_name=os.path.basename(_jn[2] or "notes.md"),
                mime="text/markdown",
                use_container_width=True,
            )
        elif lib_item and lib_item.get("notes"):
            with st.expander(f"已保存的精读笔记（{lib_item.get('notes_at','')[:10]}）",
                             expanded=False):
                st.markdown(lib_item["notes"])
                st.download_button(
                    "下载这份笔记",
                    lib_item["notes"],
                    file_name=(lib_item.get("title", "paper")[:40] + "_精读笔记.md"),
                    mime="text/markdown",
                    key="dl_saved_notes",
                    use_container_width=True)

        st.divider()
        st.subheader("针对这篇论文提问")
        # 问答历史存 session_state：点按钮（生成思考题等）触发 rerun 也不丢
        _qa_key = f"qa_history_{lib_item['id']}"
        if _qa_key not in st.session_state:
            st.session_state[_qa_key] = []
        question = st.chat_input("例如：研究方法是什么？结论是什么？")
        if question:
            need_model()
            with st.chat_message("user"):
                st.write(question)
            with st.spinner("正在检索原文并作答 ..."):
                idx = reader.build_or_load_index(pdf_path)
                pages, sources, answer, mode = reader.answer_question(idx, question)
            st.session_state[_qa_key].append(
                {"q": question, "answer": answer, "pages": pages,
                 "sources": sources, "mode": mode})
            st.rerun()
        for _qa in st.session_state[_qa_key]:
            with st.chat_message("user"):
                st.write(_qa["q"])
            with st.chat_message("assistant"):
                st.write(_qa["answer"])
                if _qa["pages"]:
                    mode_name = {"hybrid": "混合", "bm25": "关键词",
                                 "fulltext": "全文通读"}.get(_qa["mode"], _qa["mode"])
                    st.caption(f"出处页码：{_qa['pages']}（{mode_name}）")
                    if _qa["sources"]:
                        source_chips(uploaded.name, _qa["sources"])
                        with st.expander("查看命中的原文片段"):
                            for pg, score, head, detail in _qa["sources"]:
                                st.write(f"**第{pg}页**（融合分 {round(score,3)}，"
                                         f"BM25={detail.get('bm25')} 语义={detail.get('vec')}）：{head}...")
        if len(st.session_state[_qa_key]) > 1:
            if st.button("清空问答记录", key="qa_clear"):
                st.session_state[_qa_key] = []
                st.rerun()

        # ---- 思考题导师互动 ----
        st.divider()
        st.subheader("思考题")

        if st.button("生成 5 道思考题", use_container_width=True, key="gen_quiz"):
            need_model()
            with st.spinner("导师正在通读论文并出题 ..."):
                idx_q = reader.build_or_load_index(pdf_path)
                st.session_state["quiz"] = reader.generate_quiz(idx_q, n=5)
            st.success("题目已生成，全部列出，逐题作答即可。")

        quiz = st.session_state.get("quiz")
        if quiz:
            st.caption(f"共 {len(quiz)} 题，全部列出；逐题作答，可分别让导师点评或看参考要点。")
            for qi, item in enumerate(quiz):
                with st.container(border=True):
                    st.markdown(f"**第 {qi+1} 题：{item['q']}**")
                    if item.get("pages"):
                        st.caption(f"建议回看页码：{item['pages']}")
                    with st.expander("先看看这题该答到哪些要点（评分标准）"):
                        for kp in item.get("keypoints", []):
                            st.write(f"- {kp}")

                    user_answer = st.text_area("用自己的话作答", height=140,
                                               key=f"user_answer_{qi}")
                    cq1, cq2 = st.columns(2)
                    if cq1.button("导师点评我的回答", use_container_width=True,
                                  key=f"quiz_review_{qi}"):
                        if not user_answer.strip():
                            st.warning("先写一点你的理解，导师才能点评哦～")
                        else:
                            need_model()
                            with st.spinner("导师正在对照原文点评 ..."):
                                idx_r = reader.build_or_load_index(pdf_path)
                                pages_r, review = reader.review_user_answer(
                                    idx_r, item["q"], user_answer)
                            head_by_page = {}
                            for ch in idx_r["chunks"]:
                                head_by_page.setdefault(ch["page"], ch["text"][:80])
                            st.session_state[f"quiz_reviewed_{qi}"] = (
                                review, [(pg, head_by_page.get(pg, "")) for pg in pages_r])
                            st.rerun()
                    if cq2.button("参考答案要点", use_container_width=True,
                                  key=f"do_ref_{qi}"):
                        need_model()
                        with st.spinner("正在整理参考答案 ..."):
                            idx_a = reader.build_or_load_index(pdf_path)
                            pages_a, reference = reader.answer_thinking(idx_a, item["q"])
                        head_by_page = {}
                        for ch in idx_a["chunks"]:
                            head_by_page.setdefault(ch["page"], ch["text"][:80])
                        st.session_state[f"quiz_refed_{qi}"] = (
                            reference, [(pg, head_by_page.get(pg, "")) for pg in pages_a])
                        st.rerun()

                    _rev = st.session_state.get(f"quiz_reviewed_{qi}")
                    if _rev:
                        st.markdown("**导师点评**")
                        st.markdown(_rev[0])
                        if _rev[1]:
                            source_chips(uploaded.name,
                                         [(pg, 0, h, {}) for pg, h in _rev[1]])
                    _ref = st.session_state.get(f"quiz_refed_{qi}")
                    if _ref:
                        st.markdown("**参考答案要点**")
                        st.markdown(_ref[0])
                        if _ref[1]:
                            source_chips(uploaded.name,
                                         [(pg, 0, h, {}) for pg, h in _ref[1]])

# ======================================================================
# 页面 2：多篇对比矩阵（上传 N 篇 → 复用 compare 检索抽取 → 汇成一张表）
# ======================================================================
with tab_compare:
    st.write("上传 **2~6 篇** 同主题论文，自动按「问题 / 方法 / 数据集 / 指标 / 结果 / 结论」"
             "逐篇检索抽取，汇成一张对比表，每个字段都可点页码跳回原文。")
    cmp_files = st.file_uploader(
        "选择多篇 PDF（可一次多选）", type=["pdf"],
        accept_multiple_files=True, key="cmp_uploads")

    if cmp_files:
        st.caption("已选择 " + "、".join(f.name for f in cmp_files))
    run_cmp = st.button("生成对比矩阵", use_container_width=True,
                        disabled=not cmp_files or len(cmp_files) < 2)

    if run_cmp and cmp_files:
        need_model()
        paths, saved_names = [], []
        for up in cmp_files:
            p = os.path.join(UPLOAD_DIR, up.name)
            with open(p, "wb") as f:
                f.write(up.getbuffer())
            paths.append(p)
            saved_names.append(up.name)

        bar = st.progress(0.0, text="准备中 ...")

        def _prog(done, total, name):
            bar.progress(done / total, text=f"[{done}/{total}] 检索并抽取：{name}")

        with st.spinner("正在逐篇检索抽取（每篇一次模型调用，请稍候）..."):
            result = compare.compare_papers(paths, progress=_prog)
        bar.progress(1.0, text="完成")
        st.session_state["cmp_result"] = result
        st.session_state["cmp_names"] = saved_names

    # 渲染结果
    result = st.session_state.get("cmp_result")
    cmp_names = st.session_state.get("cmp_names", [])
    if result and cmp_names and len(cmp_names) == len(result["rows"]):
        st.subheader("对比矩阵")
        st.caption(f"检索模式：{ '、'.join(sorted({r['mode'] for r in result['rows']})) }"
                   "（页码可点击跳转原文）")

        def _cell_md(row_idx, field):
            """单元格：抽取文本 + 可点击跳原文的页码链接。"""
            row = result["rows"][row_idx]
            cell = row["record"].get(field, {"text": "", "pages": []})
            name = cmp_names[row_idx]
            links = []
            for pg in cell.get("pages", []):
                # 用该字段命中片段里这一页的开头作为高亮词
                phrase = ""
                for hpg, head, _s, _d in row["hits"].get(field, []):
                    if hpg == pg and head:
                        phrase = clean_phrase(head)
                        break
                links.append(f"[p{pg}]({viewer_link(name, pg, phrase)})")
            tail = " " + " ".join(links) if links else ""
            return cell.get("text", "").replace("|", "/") + tail

        # 字段为行、论文为列的 Markdown 表
        titles = [r["record"].get("title") or cmp_names[i]
                  for i, r in enumerate(result["rows"])]
        lines = ["| 维度 | " + " | ".join(titles) + " |",
                 "|" + "---|" * (len(result["rows"]) + 1)]
        for field, (zh, _q) in result["fields"].items():
            cells = [_cell_md(i, field) for i in range(len(result["rows"]))]
            lines.append(f"| **{zh}** | " + " | ".join(cells) + " |")
        st.markdown("\n".join(lines), unsafe_allow_html=True)

        # 抽取失败提示
        for i, r in enumerate(result["rows"]):
            if r["record"].get("_error"):
                st.warning(f"{cmp_names[i]} 抽取失败：{r['record']['_error']}")

        md = compare.to_markdown(result)
        st.download_button(
            "下载对比矩阵 Markdown", md,
            file_name="litpulse_compare.md", use_container_width=True)

        st.divider()
        if st.button("在此基础上生成整库综述",
                     use_container_width=True, key="do_synthesize"):
            need_model()
            with st.spinner("reduce 阶段：综合各篇证据卡，写演进/共识/分歧/建议 ..."):
                st.session_state["cmp_review"] = compare.synthesize_review(result)

        review = st.session_state.get("cmp_review")
        if review and st.session_state.get("cmp_names") == cmp_names:
            def _cite_html(text):
                """把句子里的 [n:pX] 渲染成可点击 pdf.js 链接（正文先转义防注入）。"""
                safe = html.escape(text)

                def repl(m):
                    n = int(m.group(1))
                    if not (1 <= n <= len(cmp_names)):
                        return html.escape(m.group(0))
                    pages = compare.cite_pages(m.group(2))
                    if not pages:
                        return html.escape(m.group(0))
                    name = cmp_names[n - 1]
                    tip = html.escape(name)
                    # 编号只显示一次，每个页码各自是一个可点击链接
                    links = []
                    for pg in pages:
                        url = viewer_link(name, pg)
                        links.append(
                            f'<a href="{url}" target="_blank" rel="noopener" title="{tip}" '
                            f'style="white-space:nowrap;color:#0f9d8f;text-decoration:none;'
                            f'font-weight:600;">p{pg}</a>')
                    return f'<span style="white-space:nowrap;">[{n}:{",".join(links)}]</span>'

                return compare.CITE_RE.sub(repl, safe)

            st.subheader("整库综述")
            _allowed, bad = compare.valid_citations(result, review)
            if bad:
                st.warning("有引用页码超出该篇抽取范围，已保留文字但可能不准："
                           + " ".join(bad))
            else:
                st.caption("引用 [n:pX] 均来自实际抽取页码，可点击跳转原文。")

            for key, title in compare.REVIEW_SECTIONS:
                st.markdown(f"**{title}**")
                for it in review.get(key) or []:
                    st.markdown("• " + _cite_html(it), unsafe_allow_html=True)

            with st.expander("引用对照表"):
                for n, r in enumerate(result["rows"], 1):
                    st.markdown(f"[{n}] **{r['record'].get('title') or cmp_names[n-1]}**"
                                f" ｜ `{cmp_names[n-1]}`")

            st.download_button(
                "下载综述 Markdown",
                compare.review_to_markdown(result, review),
                file_name="litpulse_review.md", use_container_width=True)


# ======================================================================
# 页面 3：追新周报（一键运行 weekly_pulse 的主流程）
# ======================================================================
with tab_pulse:
    topic_cfg = wp.load_topic()
    with st.form("topic_form", border=False):
        new_topic = st.text_area(
            "研究课题（一句话描述你关注的方向，模型据此判断相关性；修改后点「保存」生效）",
            value=topic_cfg["topic"], key="topic_input",
            placeholder="例：检索增强生成（RAG）：如何让大模型基于外部知识库准确回答、减少幻觉")
        new_kw = st.text_input(
            "arXiv 关键词（用空格或逗号分隔，2~4 个英文词即可）",
            value=" ".join(topic_cfg["keywords"]), key="kw_input",
            placeholder="例：retrieval, augmented, generation")
        if st.form_submit_button("保存"):
            try:
                wp.save_topic(new_topic, new_kw)
                st.success("已保存，下次扫描立即生效。")
                st.rerun()
            except ValueError as e:
                st.warning(str(e))
    if topic_cfg["keywords"]:
        st.caption(f"关键词：{' '.join(topic_cfg['keywords'])} ｜ 候选 {wp.MAX_PAPERS} 篇 ｜ 阈值 ≥ {wp.MIN_SCORE}")
    else:
        st.caption("先填写课题和关键词，保存后即可扫描。")

    _topic_ready = bool(topic_cfg["topic"] and topic_cfg["keywords"])
    if st.button("立即扫描并生成周报", use_container_width=True, disabled=not _topic_ready,
                 help=None if _topic_ready else "先在上方填写并保存研究课题，才能扫描"):
        need_model()
        progress = st.progress(0.0, text="正在从 arXiv 拉取最新论文 ...")
        try:
            papers = wp.fetch_arxiv(topic_cfg["keywords"], wp.MAX_PAPERS)
        except Exception as e:
            progress.empty()
            st.error(f"拉取 arXiv 论文失败：{e}\n\n"
                     "常见原因：网络不通，或 arXiv 免费接口限流（每 3 秒 1 次）。"
                     "稍等 1–2 分钟再点「立即扫描」。")
            st.stop()
        progress.progress(0.3, text=f"拉到 {len(papers)} 篇，正在用长期记忆去重 ...")

        memory = wp.PaperMemory()
        new_papers = [p for p in papers if not memory.has(p["id"])]
        progress.progress(0.45, text=f"新论文 {len(new_papers)} 篇，正在让模型打分 ...")

        scores = wp.score_papers(topic_cfg["topic"], new_papers)
        picked = []
        for i, p in enumerate(new_papers):
            s = scores.get(i, {})
            score = s.get("score", 0)
            relation = s.get("relation", "")
            stance = s.get("stance", "related")
            stance_reason = s.get("stance_reason", "")
            if stance not in wp.STANCES:
                stance = "related"
            keep = score >= wp.MIN_SCORE
            # 无论是否入选都记入长期记忆；立场随论文一起存（AI 初判，可在追新库人工锁定）
            memory.remember(p["id"], p, score=score, relation=relation, pushed=keep,
                            stance=stance, stance_reason=stance_reason)
            if keep:
                picked.append((score, p, relation, stance, stance_reason))
        picked.sort(key=lambda x: -x[0])

        progress.progress(0.85, text="正在生成周报并保存记忆 ...")
        # 注意：这里要传 memory【对象】本身，build_report 内部会调 .counts()/.stance_counts()
        report = wp.build_report(
            topic_cfg["topic"], picked, len(papers), len(new_papers), memory)
        today = datetime.date.today().isoformat()
        os.makedirs("reports", exist_ok=True)
        report_path = f"reports/pulse_{today}.md"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
        memory.save()
        progress.progress(1.0, text="完成！")

        st.success(f"本周新高相关论文 {len(picked)} 篇，已保存：{report_path}")
        st.download_button(
            "下载周报 Markdown",
            report,
            file_name=f"pulse_{today}.md",
            mime="text/markdown",
            use_container_width=True,
        )
        st.markdown(report)

# ======================================================================
# 页面 4：我的文献库（查看长期记忆、一键改状态）
# ======================================================================
with tab_lib:
    if not os.path.exists(wp.SEEN_PATH):
        st.info("还没有文献库记录，先到「追新周报」扫描一次吧。")
    else:
        data = json.load(open(wp.SEEN_PATH, encoding="utf-8"))
        counts = {"new": 0, "read": 0, "useful": 0, "skip": 0}
        stance_counts = {k: 0 for k in wp.STANCES}
        for r in data.values():
            counts[r.get("status", "new")] = counts.get(r.get("status", "new"), 0) + 1
            stance_counts[r.get("stance", "related")] = \
                stance_counts.get(r.get("stance", "related"), 0) + 1
        c1, c2, c3, c4 = st.columns(4)
        _stat_card(c1, "累计", len(data))
        _stat_card(c2, "待读", counts["new"])
        _stat_card(c3, "已读", counts["read"])
        _stat_card(c4, "有用", counts["useful"])

        # 立场地图概览
        stance_line = "　".join(
            f"{wp.STANCES[k][0]} {stance_counts.get(k,0)}" for k in wp.STANCES)
        st.caption("立场分布：" + stance_line)

        f1, f2 = st.columns(2)
        filter_status = f1.selectbox("筛选阅读状态",
                                     ["all", "new", "useful", "read", "skip"])
        filter_stance = f2.selectbox(
            "筛选立场", ["all"] + list(wp.STANCES.keys()),
            format_func=lambda k: "全部立场" if k == "all" else wp.STANCES[k][0])

        rows = [(pid, r) for pid, r in data.items()
                if (filter_status == "all" or r.get("status") == filter_status)
                and (filter_stance == "all" or r.get("stance", "related") == filter_stance)]
        rows.sort(key=lambda x: -(x[1].get("score") or 0))

        st.write(f"共 {len(rows)} 篇")
        for pid, r in rows:
            with st.container(border=True):
                st.write(f"**{r.get('title','')}**")
                cur_stance = r.get("stance", "related")
                who = "已确认" if r.get("stance_confirmed") else "AI 初判"
                st.caption(
                    f"ID `{pid}` ｜ 评分 {r.get('score','-')}/10 ｜ "
                    f"阅读：{r.get('status','new')} ｜ 立场：{wp.STANCES[cur_stance][0]}（{who}）")
                if r.get("relation"):
                    st.write(r.get("relation"))
                if r.get("link"):
                    st.markdown(f"[打开论文]({r['link']})")

                # 选择即"确认/修改立场"并锁定（AI 以后不再覆盖）
                new_stance = st.selectbox(
                    "确认/修改它对你课题的立场",
                    list(wp.STANCES.keys()),
                    index=list(wp.STANCES.keys()).index(cur_stance)
                          if cur_stance in wp.STANCES else 4,
                    format_func=lambda k: wp.STANCES[k][0],
                    key=f"stance_{pid}")
                if new_stance != cur_stance:
                    mem = wp.PaperMemory()
                    if mem.set_stance(pid, new_stance):
                        mem.save()
                        st.toast(f"立场已确认为：{wp.STANCES[new_stance][0]}（已锁定）")
                        st.rerun()

                m1, m2, m3, m4 = st.columns(4)
                if m1.button("有用", key=f"u_{pid}", use_container_width=True):
                    mem = wp.PaperMemory(); mem.set_status(pid, "useful"); mem.save()
                    st.rerun()
                if m2.button("已读", key=f"r_{pid}", use_container_width=True):
                    mem = wp.PaperMemory(); mem.set_status(pid, "read"); mem.save()
                    st.rerun()
                if m3.button("待读", key=f"n_{pid}", use_container_width=True):
                    mem = wp.PaperMemory(); mem.set_status(pid, "new"); mem.save()
                    st.rerun()
                if m4.button("忽略", key=f"s_{pid}", use_container_width=True):
                    mem = wp.PaperMemory(); mem.set_status(pid, "skip"); mem.save()
                    st.rerun()


# 页面 5：我的文库（持久保存上传的 PDF + 元数据 + 标注）
# ======================================================================
def _fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


with tab_mylib:
    st.write("所有上传的文献都会**长期保存在本机** `library/` 目录，可随时重新打开阅读与标注。")
    st.caption("在阅读页用 **Highlight / Text / Draw** 标注，停笔约 1 秒自动存回，下次打开仍在。"
               "「标注」区还可按页码写文字批注并跳回该页。")

    ups = st.file_uploader("把 PDF 加入我的文库（可一次多选）", type=["pdf"],
                           accept_multiple_files=True, key="mylib_uploads")
    if ups:
        n_added = 0
        for up in ups:
            tmp = os.path.join(UPLOAD_DIR, up.name)
            with open(tmp, "wb") as f:
                f.write(up.getbuffer())
            library.import_pdf(tmp, up.name)
            n_added += 1
        st.success(f"已把 {n_added} 篇收入我的文库。")

    # ---- 通过 arXiv 链接 / DOI / 标题 联网导入 ----
    with st.expander("粘贴导入：arXiv / DOI / 标题", expanded=False):
        st.caption("支持 arXiv 链接或编号、DOI（如 10.1145/…）、或直接粘论文标题；"
                   "自动联网补全标题、作者、期刊/会议、卷期页、DOI、文献类型。"
                   "arXiv 论文通常会顺带下载 PDF；正式出版论文若拿不到开放 PDF，"
                   "会先存成带完整信息的条目并附原文链接。")
        with st.form("lookup_form"):
            query = st.text_input(
                "arXiv 链接 / DOI / 论文标题",
                placeholder="例如 https://arxiv.org/abs/1706.03762 或 Attention Is All You Need")
            submitted = st.form_submit_button("查询")
        if submitted and query.strip():
            with st.spinner("正在查询 arXiv / CrossRef ..."):
                try:
                    st.session_state["lookup_candidate"] = lookup.resolve(query.strip())
                    st.rerun()
                except lookup.LookupError as e:
                    st.session_state.pop("lookup_candidate", None)
                    st.warning(str(e))

        cand = st.session_state.get("lookup_candidate")
        if cand:
            with st.container(border=True):
                st.markdown(f"**{cand.get('title') or '(无标题)'}**")
                st.caption(
                    f"作者：{', '.join(cand.get('authors', [])[:6]) or '—'} ｜ "
                    f"年份：{cand.get('date') or '—'} ｜ 类型：{cand.get('doc_type') or '—'}")
                venue_line = cand.get("venue") or ""
                bits = []
                if cand.get("volume"):
                    bits.append("卷 " + cand["volume"])
                if cand.get("issue"):
                    bits.append("期 " + cand["issue"])
                if cand.get("article_pages"):
                    bits.append("页 " + cand["article_pages"])
                if venue_line or bits:
                    st.caption(f"期刊/会议：{venue_line or '—'} ｜ {' ｜ '.join(bits)}")
                if cand.get("doi"):
                    st.caption(f"DOI：{cand['doi']}")
                if cand.get("abstract"):
                    st.caption("摘要：" + cand["abstract"][:300] +
                               ("…" if len(cand["abstract"]) > 300 else ""))
                can_pdf = bool(cand.get("pdf_url"))
                st.caption(("检测到开放 PDF，加入时会一起下载。" if can_pdf
                            else "无开放 PDF：仅保存元数据与原文链接，稍后可再上传 PDF。"))
                cc1, cc2 = st.columns(2)
                if cc1.button("加入我的文库", use_container_width=True,
                              key="lookup_confirm"):
                    with st.spinner("正在加入（如能下载 PDF 会稍慢）..."):
                        iid, it = library.import_record(cand, fetch_pdf=can_pdf)
                    st.session_state.pop("lookup_candidate", None)
                    if it.get("has_pdf"):
                        st.success("已加入，并下载了 PDF。")
                    else:
                        st.success("已加入（纯元数据条目，可点原文链接或之后补传 PDF）。")
                    st.rerun()
                if cc2.button("取消", use_container_width=True, key="lookup_cancel"):
                    st.session_state.pop("lookup_candidate", None)
                    st.rerun()

    items = library.list_items()
    if not items:
        st.info("文库还是空的：在上面上传，或到「精读论文」打开一篇，会自动存入这里。")
    else:
        total_ann = sum(len(it.get("annotations", [])) for it in items)
        total_pages = sum(it.get("pages") or 0 for it in items)
        s1, s2, s3 = st.columns(3)
        _stat_card(s1, "文献", len(items))
        _stat_card(s2, "标注", total_ann)
        _stat_card(s3, "总页数", total_pages)

        kw = st.text_input("搜索：标题 / 作者 / 期刊 / DOI", key="mylib_filter")
        tag_opts = [t for t, _ in library.all_tags()]
        col_opts = [c for c, _ in library.all_collections()]
        fc1, fc2 = st.columns(2)
        sel_tags = fc1.multiselect(
            "按标签筛选（任一命中）", tag_opts, key="mylib_sel_tags")
        sel_col = fc2.selectbox(
            "按文件夹筛选", ["全部"] + col_opts, key="mylib_sel_col")

        def _match(it):
            hay = " ".join([
                it.get("title", ""), it.get("authors", ""),
                it.get("venue", ""), it.get("doi", ""),
                " ".join(it.get("tags", [])), " ".join(it.get("collections", [])),
            ]).lower()
            if kw and kw.strip().lower() not in hay:
                return False
            if sel_tags and not any(t in it.get("tags", []) for t in sel_tags):
                return False
            if sel_col != "全部" and sel_col not in it.get("collections", []):
                return False
            return True

        filtered = [it for it in items if _match(it)]
        ec_a, ec_b = st.columns([3, 2])
        ec_a.caption(f"当前显示 **{len(filtered)} / {len(items)}** 篇"
                     + (f"　标签 {', '.join(sel_tags)}" if sel_tags else "")
                     + (f"　文件夹 {sel_col}" if sel_col != "全部" else ""))
        if filtered:
            ec_b.download_button(
                "导出 BibTeX (.bib)",
                library.export_bibtex(filtered),
                file_name="litpulse_refs.bib",
                mime="application/x-bibtex",
                use_container_width=True,
                key="bib_filtered")

        for it in filtered:
            with st.container(border=True):
                has_pdf = it.get("has_pdf", bool(it.get("file")))
                type_badge = f" ｜ `{it['doc_type']}`" if it.get("doc_type") else ""
                st.markdown(f"**{it.get('title') or it.get('original_name') or '(未命名文献)'}**{type_badge}")
                st.caption(
                    f"作者：{it.get('authors') or '—'} ｜ 日期：{it.get('date') or '—'}"
                    + (f" ｜ {it.get('pages') or '?'} 页 ｜ {_fmt_size(it.get('size', 0))}"
                       if has_pdf else " ｜ 无本地 PDF")
                    + f" ｜ 加入于 {(it.get('added_at') or '')[:10]}")
                # 期刊/会议 + 卷期页 + DOI（有才显示）
                _bib = []
                if it.get("venue"):
                    _bib.append(it["venue"] + _meta_badge(it, "venue"))
                _vp = " ｜ ".join(
                    x for x in ([("卷 " + it["volume"]) if it.get("volume") else "",
                                 ("期 " + it["issue"]) if it.get("issue") else "",
                                 ("页 " + it["article_pages"]) if it.get("article_pages") else ""]
                                ) if x)
                if _vp:
                    _bib.append(_vp)
                if it.get("doi"):
                    _bib.append("DOI: " + it["doi"]
                                + _meta_badge(it, "doi"))
                if _bib:
                    st.caption("　".join(_bib))

                # 已贴标签 / 所属文件夹徽章
                _tag_chips = "".join(
                    f"`{t}` " for t in it.get("tags", []))
                _col_chips = "".join(
                    f"`{c}` " for c in it.get("collections", []))
                if _tag_chips or _col_chips:
                    st.markdown(_col_chips + _tag_chips)

                rc1, rc2 = st.columns([4, 1])
                if has_pdf:
                    rc1.markdown(
                        f'<a href="{viewer_link(it["file"], 1, source="library")}" '
                        f'target="_blank" rel="noopener" style="display:block;text-align:center;'
                        f'padding:0.45rem;border:1px solid #0f9d8f;border-radius:0.5rem;'
                        f'color:#0f9d8f;text-decoration:none;font-weight:600;">在线阅读</a>',
                        unsafe_allow_html=True)
                elif it.get("source_url"):
                    rc1.markdown(
                        f'<a href="{it["source_url"]}" target="_blank" rel="noopener" '
                        f'style="display:block;text-align:center;padding:0.45rem;'
                        f'border:1px solid #9fd8d0;border-radius:0.5rem;color:#0f9d8f;background:#f2fbf9;'
                        f'text-decoration:none;font-weight:600;">打开原文页面</a>',
                        unsafe_allow_html=True)
                else:
                    rc1.caption("（无可用原文链接）")
                pending = st.session_state.get("del_pending")
                if rc2.button("删除" if pending != it["id"] else "确认删除",
                              key=f"del_{it['id']}", use_container_width=True):
                    if pending == it["id"]:
                        library.delete_item(it["id"])
                        st.session_state["del_pending"] = None
                    else:
                        st.session_state["del_pending"] = it["id"]
                    st.rerun()

                # 补全入口统一在「精读论文」信息卡：上传论文 → 联网补全信息，
                # 结果直接写入文库数据（library.enrich_online），此处不再重复放按钮。

                n_ann = len(it.get("annotations", []))
                # ---- 笔记与标注（合并展示）----
                with st.expander("笔记与标注"):
                    if it.get("notes"):
                        st.markdown(it["notes"])
                        st.download_button(
                            "下载笔记 Markdown", it["notes"],
                            file_name=(it.get("title", "paper")[:40] + "_精读笔记.md"),
                            mime="text/markdown", key=f"dlnotes_{it['id']}",
                            use_container_width=True)
                    elif has_pdf:
                        st.caption("还没有笔记：到「精读论文」打开这篇并点【生成精读笔记】即可。")
                    else:
                        st.caption("这条还没有本地 PDF。先上传 PDF 后，才能在「精读论文」里生成笔记。")

                # ---- 标签 / 文件夹管理 + 单篇 BibTeX ----
                _n_tag = len(it.get("tags", []))
                _n_col = len(it.get("collections", []))
                with st.expander(f"整理与导出（标签 {_n_tag} · 文件夹 {_n_col}）"):
                    with st.form(f"labels_{it['id']}"):
                        st.caption("多个标签或文件夹用逗号分隔；一篇可以属于多个文件夹。"
                                   "删掉文字即移除，也可直接输入新名字。")
                        in_tags = st.text_input(
                            "标签", value=", ".join(it.get("tags", [])),
                            key=f"intags_{it['id']}")
                        in_cols = st.text_input(
                            "文件夹", value=", ".join(it.get("collections", [])),
                            key=f"incols_{it['id']}")
                        if st.form_submit_button("保存"):
                            library.set_tags(it["id"], in_tags)
                            library.set_collections(it["id"], in_cols)
                            st.success("已保存。")
                            st.rerun()

                    _key = library.citation_keys([it])[it["id"]]
                    st.caption(f"BibTeX citation key：`{_key}`")
                    st.download_button(
                        "导出这条的 BibTeX",
                        library.to_bibtex(it, _key),
                        file_name=(_key + ".bib"),
                        mime="application/x-bibtex",
                        use_container_width=True,
                        key=f"bibone_{it['id']}")

                    if has_pdf:
                        st.divider()
                        with st.form(f"annform_{it['id']}"):
                            ap = st.number_input(
                                "页码", min_value=1,
                                max_value=max(1, it.get("pages") or 1),
                                value=1, step=1, key=f"annp_{it['id']}")
                            aq = st.text_area("原文摘录（可选，用于自动高亮定位）",
                                              key=f"annq_{it['id']}")
                            an = st.text_area("我的批注", key=f"annt_{it['id']}")
                            if st.form_submit_button("添加标注"):
                                if an.strip():
                                    library.add_annotation(it["id"], int(ap), aq, an)
                                    st.rerun()
                                else:
                                    st.warning("先写一点批注内容。")

                        for a in it.get("annotations", []):
                            link = viewer_link(it["file"], a["page"],
                                               a.get("quote", ""), source="library")
                            st.markdown(
                                f"[{ '第 ' + str(a['page']) + ' 页'}]({link}) "
                                f"｜ {(a.get('created_at') or '')[:10]}")
                            if a.get("quote"):
                                st.caption("摘录：" + a["quote"][:220])
                            st.write(a["note"])
                            if st.button("删除该标注", key=f"delann_{a['id']}"):
                                library.delete_annotation(it["id"], a["id"])
                                st.rerun()
                            st.divider()

                # ---- 整理与导出：标签/文件夹 + BibTeX ----

st.caption("LitPulse · 数据全部保存在本机（library/ 原文、data/ 记忆、notes/、reports/）")
