"""向量语义记忆服务 —— PyTorch-CPU + Milvus Lite

架构：
- Embedder  : sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
              纯 CPU 推理，512 维，中英文均可，Replit 上约 80ms/条
- 向量库     : Milvus Lite (pymilvus >= 2.4)，本地文件 ./data/memory.db
- 记忆粒度   : 每条聊天消息存一个向量 + payload
- 检索策略   : COSINE 相似度，Top-K=5，score 阈值 0.55
- 去重       : 相同 chat_id + text hash 不重复写入
- 自动初始化 : 首次 import 时懒初始化，不阻塞 bot 启动

外部依赖（requirements.txt 需新增）：
    sentence-transformers>=2.7
    pymilvus>=2.4.3
    torch>=2.2 (CPU-only wheel)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─── 常量 ───────────────────────────────────────────────────────────────────
EMBED_MODEL = os.getenv(
    "MEMORY_EMBED_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
DB_PATH = str(Path("data/memory.db").resolve())
COLLECTION = "chat_memory"
DIM = 384          # MiniLM-L12 输出维度
TOP_K = 5
SCORE_THRESHOLD = 0.55
MAX_TEXT_LEN = 500 # 单条记忆最大字符

# ─── 懒初始化状态 ────────────────────────────────────────────────────────────
_encoder: Any = None
_milvus: Any = None
_init_lock = asyncio.Lock()
_ready = False


def _ensure_data_dir() -> None:
    Path("data").mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def _load_encoder():
    """首次调用时加载模型（约 2-4 秒），之后缓存。"""
    from sentence_transformers import SentenceTransformer  # type: ignore
    logger.info("memory_vector: loading embed model %s", EMBED_MODEL)
    return SentenceTransformer(EMBED_MODEL, device="cpu")


def _get_milvus():
    from pymilvus import MilvusClient  # type: ignore
    _ensure_data_dir()
    client = MilvusClient(DB_PATH)
    if not client.has_collection(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            dimension=DIM,
            metric_type="COSINE",
            auto_id=True,
        )
        logger.info("memory_vector: created collection %s dim=%d", COLLECTION, DIM)
    return client


async def _ensure_ready() -> bool:
    global _encoder, _milvus, _ready
    if _ready:
        return True
    async with _init_lock:
        if _ready:
            return True
        try:
            loop = asyncio.get_running_loop()
            _encoder = await loop.run_in_executor(None, _load_encoder)
            _milvus = await loop.run_in_executor(None, _get_milvus)
            _ready = True
            logger.info("memory_vector: ready (Milvus-Lite + %s)", EMBED_MODEL)
        except Exception as e:
            logger.warning("memory_vector: init failed, feature disabled | err=%s", e)
            return False
    return _ready


# ─── 公开 API ────────────────────────────────────────────────────────────────

def _embed_sync(text: str) -> list[float]:
    vec = _encoder.encode(text[:MAX_TEXT_LEN], normalize_embeddings=True)
    return vec.tolist()


def _text_hash(chat_id: str, text: str) -> str:
    return hashlib.sha1(f"{chat_id}:{text[:200]}".encode()).hexdigest()


async def remember(
    chat_id: str | int,
    text: str,
    role: str = "user",        # "user" | "bot"
    scope: str = "default",
    extra: dict | None = None,
) -> bool:
    """把一条消息写入向量库。重复内容自动跳过。返回是否真正写入。"""
    if not text or not text.strip():
        return False
    if not await _ensure_ready():
        return False

    chat_id_str = str(chat_id)
    t_hash = _text_hash(chat_id_str, text)

    # 去重检查：按 hash 查询
    loop = asyncio.get_running_loop()
    existing = await loop.run_in_executor(
        None,
        lambda: _milvus.query(
            collection_name=COLLECTION,
            filter=f'hash == "{t_hash}"',
            output_fields=["id"],
            limit=1,
        ),
    )
    if existing:
        return False  # 已存在，跳过

    vec = await loop.run_in_executor(None, _embed_sync, text)
    payload = {
        "vector": vec,
        "chat_id": chat_id_str,
        "role": role,
        "scope": scope,
        "text": text[:MAX_TEXT_LEN],
        "hash": t_hash,
        "ts": int(time.time()),
        **(extra or {}),
    }
    await loop.run_in_executor(
        None,
        lambda: _milvus.insert(collection_name=COLLECTION, data=[payload]),
    )
    return True


async def recall(
    chat_id: str | int,
    query: str,
    top_k: int = TOP_K,
    threshold: float = SCORE_THRESHOLD,
    scope: str | None = None,
) -> list[dict]:
    """语义检索与 query 最相关的历史记忆。返回 [{text, role, score, ts}]。"""
    if not query or not await _ensure_ready():
        return []

    chat_id_str = str(chat_id)
    loop = asyncio.get_running_loop()
    vec = await loop.run_in_executor(None, _embed_sync, query)

    filter_expr = f'chat_id == "{chat_id_str}"'
    if scope:
        filter_expr += f' and scope == "{scope}"'

    results = await loop.run_in_executor(
        None,
        lambda: _milvus.search(
            collection_name=COLLECTION,
            data=[vec],
            filter=filter_expr,
            limit=top_k,
            output_fields=["text", "role", "ts", "scope"],
            search_params={"metric_type": "COSINE"},
        ),
    )

    hits = []
    for hit in (results[0] if results else []):
        score = hit.get("distance", 0)
        if score < threshold:
            continue
        entity = hit.get("entity", {})
        hits.append({
            "text": entity.get("text", ""),
            "role": entity.get("role", "user"),
            "score": round(score, 3),
            "ts": entity.get("ts", 0),
        })
    # 按相关度降序
    hits.sort(key=lambda x: x["score"], reverse=True)
    return hits


async def forget_user(chat_id: str | int) -> int:
    """删除某用户全部记忆。返回删除条数。"""
    if not await _ensure_ready():
        return 0
    chat_id_str = str(chat_id)
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None,
        lambda: _milvus.delete(
            collection_name=COLLECTION,
            filter=f'chat_id == "{chat_id_str}"',
        ),
    )
    cnt = res.get("delete_count", 0) if isinstance(res, dict) else 0
    logger.info("memory_vector: forget_user chat_id=%s count=%d", chat_id_str, cnt)
    return cnt


def format_memory_context(hits: list[dict], max_chars: int = 800) -> str:
    """把 recall 结果格式化成可注入 prompt 的字符串。"""
    if not hits:
        return ""
    lines = []
    total = 0
    for h in hits:
        role_label = "用户" if h["role"] == "user" else "Bot"
        line = f"[记忆 score={h['score']}] {role_label}: {h['text']}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)
