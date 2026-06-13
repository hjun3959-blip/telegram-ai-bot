"""向量语义记忆服务 —— PyTorch-CPU + Milvus Lite

外部依赖：
    sentence-transformers>=2.7
    pymilvus>=2.4.3
    torch>=2.2 (CPU-only)
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

EMBED_MODEL = os.getenv(
    "MEMORY_EMBED_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
DB_PATH = str(Path("data/memory.db").resolve())
COLLECTION = "chat_memory"
DIM = 384
TOP_K = 5
SCORE_THRESHOLD = 0.55
MAX_TEXT_LEN = 500

_encoder: Any = None
_milvus: Any = None
_init_lock = asyncio.Lock()
_ready = False


def _ensure_data_dir() -> None:
    Path("data").mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def _load_encoder():
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
            logger.info("memory_vector: ready")
        except Exception as e:
            logger.warning("memory_vector: init failed, disabled | err=%s", e)
            return False
    return _ready


def _embed_sync(text: str) -> list[float]:
    vec = _encoder.encode(text[:MAX_TEXT_LEN], normalize_embeddings=True)
    return vec.tolist()


def _text_hash(chat_id: str, text: str) -> str:
    return hashlib.sha1(f"{chat_id}:{text[:200]}".encode()).hexdigest()


async def remember(
    chat_id: str | int,
    text: str,
    role: str = "user",
    scope: str = "default",
    extra: dict | None = None,
) -> bool:
    if not text or not text.strip():
        return False
    if not await _ensure_ready():
        return False

    chat_id_str = str(chat_id)
    t_hash = _text_hash(chat_id_str, text)
    loop = asyncio.get_running_loop()

    # 去重检查 — pymilvus 2.4 正确参数是 filter（Milvus Lite 支持）
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
        return False

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
    hits.sort(key=lambda x: x["score"], reverse=True)
    return hits


async def forget_user(chat_id: str | int) -> int:
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
    if not hits:
        return ""
    lines, total = [], 0
    for h in hits:
        role_label = "用户" if h["role"] == "user" else "Bot"
        line = f"[记忆 score={h['score']}] {role_label}: {h['text']}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)
