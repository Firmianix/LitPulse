"""
embeddings.py —— 本地语义向量（Embedding）
==========================================
为什么需要它：
  BM25 只能按"关键词字面"匹配；向量能按"语义"匹配——
  比如问"怎么防止模型胡说八道"，即使原文写的是"reduce hallucinations"，
  向量也能认出它们意思相近。

方案：用本地多语言模型 paraphrase-multilingual-MiniLM-L12-v2
  - 免费、离线运行、支持中英文、约 120MB、CPU 可跑
  - 首次使用自动下载（可用 HF_ENDPOINT=https://hf-mirror.com 走国内镜像）
  - 论文/资料片段的向量会缓存到 data/vectors/，第二次提问不用重新计算

依赖（可选）：pip install sentence-transformers
  没装时本模块 AVAILABLE=False，reader.py 会自动回退到纯 BM25，不影响使用。
"""

import os

# 首次下载模型默认走国内镜像，避免连不上 HuggingFace（已设置则不覆盖）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

try:
    import numpy as np
    from sentence_transformers import SentenceTransformer
    AVAILABLE = True
except Exception:
    np = None
    SentenceTransformer = None
    AVAILABLE = False

# 可用环境变量覆盖模型；默认多语言 MiniLM
MODEL_NAME = os.environ.get(
    "LITPULSE_EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
VECTOR_DIR = os.path.join("data", "vectors")
os.makedirs(VECTOR_DIR, exist_ok=True)

_model = None


def get_model():
    """惰性加载模型（第一次调用才下载/载入，省启动时间）。"""
    global _model
    if not AVAILABLE:
        raise RuntimeError("未安装 sentence-transformers，无法使用向量检索")
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def encode(texts):
    """把一批文本编码成 L2 归一化向量；归一化后余弦相似度=向量点积。"""
    vecs = get_model().encode(
        texts, normalize_embeddings=True,
        convert_to_numpy=True, show_progress_bar=False)
    return np.asarray(vecs, dtype=np.float32)


def _vector_path(stem):
    return os.path.join(VECTOR_DIR, stem + ".npz")


def build_or_load_vectors(index, stem):
    """
    为一篇资料的所有片段构建/读取向量缓存。
    返回 np.ndarray，形状 (片段数, 维度)，顺序与 index['chunks'] 对齐。
    """
    if not AVAILABLE:
        return None

    path = _vector_path(stem)
    n = len(index["chunks"])
    if os.path.exists(path):
        loaded = np.load(path)
        if loaded["vecs"].shape[0] == n:     # 片段数没变才用缓存
            return loaded["vecs"]

    texts = [c["text"] for c in index["chunks"]]
    vecs = encode(texts)
    np.savez_compressed(path, vecs=vecs)
    return vecs


def cosine_scores(chunk_vecs, query_vec):
    """chunk_vecs 已 L2 归一化、query_vec 也归一化，点积即余弦，范围约 [-1,1]。"""
    q = query_vec.reshape(1, -1)
    return (chunk_vecs @ q.T).reshape(-1)
