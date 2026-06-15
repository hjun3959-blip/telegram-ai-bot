from __future__ import annotations
from utils.logger import setup_logging
logger = setup_logging()
"""
conscious_memory.py
═══════════════════════════════════════════════════════════════════════
MadDog 语义长期记忆核心模块
架构：bge-small-zh-v1.5 (512-dim) + Milvus-Lite + 双层召回（向量优先 + Bigram 兜底）
设计原则：懒加载、永久失败降级、asyncio.to_thread 包装、threading.Lock 保护模型

fix(BUG-04/09): Milvus filter 只保留顶层字段 owner_key；
  "type" 字段存在 metadata JSON 中不是顶层字段，改为 Python 层 post-filter。
═══════════════════════════════════════════════════════════════════════
"""

from utils.text import make_bigrams as _make_bigrams

import asyncio
import json
import re
import threading
import time
import uuid
from typing import Any

_MilvusClient = None
_SentenceTransformer = None


def _import_deps() -> tuple[Any, Any]:
    global _MilvusClient, _SentenceTransformer
    if _MilvusClient is None:
        from pymilvus import MilvusClient
        _MilvusClient = MilvusClient
    if _SentenceTransformer is None:
        from sentence_transformers import SentenceTransformer
        _SentenceTransformer = SentenceTransformer
    return _MilvusClient, _SentenceTransformer


COLLECTION_NAME  = "user_semantic_memories"
EMBED_MODEL_ID   = "BAAI/bge-small-zh-v1.5"
EMBED_DIM        = 512
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
DEFAULT_DB_PATH  = "/opt/xinxue-bot/memory_vectors.db"

_global_memory: "SemanticMemory | None" = None
_global_lock   = asyncio.Lock()


async def get_memory(db_path: str = DEFAULT_DB_PATH) -> "SemanticMemory":
    global _global_memory
    if _global_memory is not None:
        return _global_memory
    async with _global_lock:
        if _global_memory is None:
            _global_memory = SemanticMemory(db_path)
            await _global_memory._ensure_ready()
    return _global_memory


class SemanticMemory:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self._db_path    = db_path
        self._model      = None
        self._client     = None
        self._ready      = False
        self._failed     = False
        self._embed_lock = threading.Lock()
        self._init_lock  = asyncio.Lock()
        self._existing_ids: set[str] = set()

    async def _ensure_ready(self) -> None:
        if self._ready or self._failed:
            return
        async with self._init_lock:
            if self._ready or self._failed:
                return
            try:
                await asyncio.to_thread(self._sync_init)
                self._ready = True
            except Exception as e:
                self._failed = True
                logger.warning("SemanticMemory 初始化失败，Bigram兜底 | err=%s", e)

    def _sync_init(self) -> None:
        MilvusClient, SentenceTransformer = _import_deps()
        self._model = SentenceTransformer(EMBED_MODEL_ID, device="cpu")
        test_vec = self._model.encode("维度校验测试", normalize_embeddings=True)
        actual_dim = len(test_vec)
        assert actual_dim == EMBED_DIM, (
            f"Embedding 维度不匹配：期望 {EMBED_DIM}，实际 {actual_dim}。"
        )
        self._client = MilvusClient(uri=self._db_path)
        if not self._client.has_collection(COLLECTION_NAME):
            self._client.create_collection(
                collection_name=COLLECTION_NAME,
                dimension=EMBED_DIM,
                primary_field_name="id",
                vector_field_name="embedding",
                auto_id=False,
            )
        # 确保 collection 处于 loaded 状态（pymilvus 3.x 兼容）
        try:
            self._client.load_collection(COLLECTION_NAME)
        except Exception:
            pass
        self._sync_load_existing_ids()

    def _sync_load_existing_ids(self) -> None:
        try:
            rows = self._client.query(
                collection_name=COLLECTION_NAME,
                filter="",
                output_fields=["id"],
                limit=10000,
            )
            self._existing_ids = {r["id"] for r in rows}
        except Exception:
            self._existing_ids = set()

    async def _embed(self, text: str, is_query: bool = False) -> list[float] | None:
        if not self._ready:
            return None
        raw_text = text

        def _sync_encode():
            with self._embed_lock:
                inp = (BGE_QUERY_PREFIX + raw_text) if is_query else raw_text
                return self._model.encode(
                    inp, normalize_embeddings=True, show_progress_bar=False
                ).tolist()

        try:
            return await asyncio.to_thread(_sync_encode)
        except Exception as e:
            logger.warning("SemanticMemory encode失败 | err=%s", e)
            return None

    async def _is_near_duplicate(self, content: str, threshold: float = 0.93) -> bool:
        vec = await self._embed(content, is_query=False)
        if not vec:
            return False
        try:
            results = await asyncio.to_thread(
                self._client.search,
                collection_name=COLLECTION_NAME,
                data=[vec],
                limit=1,
                output_fields=["id"],
            )
            if results and results[0] and results[0][0].get("distance", 0) >= threshold:
                return True
        except Exception:
            pass
        return False

    async def add(
        self,
        content: str,
        owner_key: str,
        metadata: dict | None = None,
        check_duplicate: bool = True,
    ) -> str | None:
        await self._ensure_ready()
        if not self._ready:
            return None
        if check_duplicate and await self._is_near_duplicate(content):
            return None
        vec = await self._embed(content, is_query=False)
        if not vec:
            return None
        record_id = str(uuid.uuid4())
        meta = {
            "type": "general",
            "source_mode": "task",
            "importance": 5,
            "timestamp": int(time.time()),
            **(metadata or {}),
        }
        row = {
            "id": record_id,
            "owner_key": owner_key,
            "content": content,
            "embedding": vec,
            "metadata": json.dumps(meta, ensure_ascii=False),
            "importance": meta.get("importance", 5),
            "timestamp": meta["timestamp"],
        }
        try:
            await asyncio.to_thread(
                self._client.insert,
                collection_name=COLLECTION_NAME,
                data=[row],
            )
            self._existing_ids.add(record_id)
            return record_id
        except Exception as e:
            logger.warning("SemanticMemory 写入失败 | err=%s", e)
            return None

    async def _vector_search(
        self,
        query: str,
        owner_key: str,
        top_k: int,
        filter_types: list[str] | None,
        min_similarity: float,
        source_mode: str | None,
    ) -> list[dict]:
        vec = await self._embed(query, is_query=True)
        if not vec:
            return []

        # fix(BUG-04): 只过滤顶层字段，type 在 metadata JSON 内，改 Python 层过滤
        filter_parts = [f'owner_key == "{owner_key}"']
        if source_mode:
            filter_parts.append(f'source_mode == "{source_mode}"')
        filter_expr = " && ".join(filter_parts)

        try:
            raw = await asyncio.to_thread(
                self._client.search,
                collection_name=COLLECTION_NAME,
                data=[vec],
                limit=top_k * 3,   # 多取，给 Python 层 type 过滤留余量
                filter=filter_expr,
                output_fields=["id", "owner_key", "content", "metadata",
                               "importance", "timestamp"],
            )
            results = []
            for hit in (raw[0] if raw else []):
                sim = hit.get("distance", 0)
                if sim < min_similarity:
                    continue
                meta_raw = hit.get("entity", {}).get("metadata", "{}")
                try:
                    meta = json.loads(meta_raw)
                except Exception:
                    meta = {}
                # Python 层 type 过滤（fix BUG-04）
                if filter_types and meta.get("type") not in filter_types:
                    continue
                results.append({
                    "id": hit.get("id"),
                    "content": hit.get("entity", {}).get("content", ""),
                    "similarity": round(sim, 4),
                    "metadata": meta,
                })
                if len(results) >= top_k:
                    break
            return results
        except Exception as e:
            logger.warning("SemanticMemory 向量检索失败 | err=%s", e)
            return []

    async def _bigram_search(
        self,
        query: str,
        owner_key: str,
        top_k: int,
        filter_types: list[str] | None,
    ) -> list[dict]:
        # fix(BUG-09): 移除 Milvus 层 type filter，改 Python 层过滤
        try:
            rows = await asyncio.to_thread(
                self._client.query,
                collection_name=COLLECTION_NAME,
                filter=f'owner_key == "{owner_key}"',
                output_fields=["id", "content", "metadata", "importance", "timestamp"],
                limit=500,
            )
        except Exception:
            return []

        bigrams = set(_make_bigrams(query))
        scored = []
        for row in rows:
            content = row.get("content", "")
            meta_raw = row.get("metadata", "{}")
            try:
                meta = json.loads(meta_raw)
            except Exception:
                meta = {}
            # Python 层 type 过滤（fix BUG-09）
            if filter_types and meta.get("type") not in filter_types:
                continue
            row_bigrams = set(_make_bigrams(content))
            overlap = len(bigrams & row_bigrams)
            if overlap > 0:
                score = overlap / max(len(bigrams), 1)
                scored.append({
                    "id": row.get("id"),
                    "content": content,
                    "similarity": round(score * 0.5, 4),
                    "metadata": meta,
                })
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:top_k]

    async def recall(
        self,
        query: str,
        owner_key: str,
        top_k: int = 5,
        filter_types: list[str] | None = None,
        min_similarity: float = 0.60,
        source_mode: str | None = None,
    ) -> list[dict]:
        await self._ensure_ready()
        if self._ready:
            results = await self._vector_search(
                query, owner_key, top_k, filter_types, min_similarity, source_mode
            )
        else:
            results = []
        if len(results) < 2:
            bigram_results = await self._bigram_search(
                query, owner_key, top_k, filter_types
            )
            existing_ids = {r["id"] for r in results}
            for br in bigram_results:
                if br["id"] not in existing_ids:
                    results.append(br)
                if len(results) >= top_k:
                    break
        results.sort(
            key=lambda x: x["similarity"] * (1 + 0.1 * x["metadata"].get("importance", 5)),
            reverse=True,
        )
        return results[:top_k]

    async def record(
        self,
        content: str,
        owner_key: str,
        metadata: dict | None = None,
    ) -> str | None:
        return await self.add(content, owner_key, metadata, check_duplicate=True)


async def recall_block(
    query: str,
    owner_key: str,
    top_k: int = 5,
    filter_types: list[str] | None = None,
    min_similarity: float = 0.60,
    source_mode: str | None = None,
) -> list[dict]:
    try:
        mem = await get_memory()
        return await mem.recall(
            query, owner_key, top_k, filter_types, min_similarity, source_mode
        )
    except Exception as e:
        logger.warning("recall_block 异常降级 | err=%s", e)
        return []


async def record_decision(
    content: str,
    owner_key: str,
    metadata: dict | None = None,
) -> str | None:
    try:
        mem = await get_memory()
        return await mem.record(content, owner_key, metadata)
    except Exception as e:
        logger.warning("record_decision 异常降级 | err=%s", e)
        return None