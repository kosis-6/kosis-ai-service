import asyncio
import logging
from collections import OrderedDict
from typing import Any

import numpy as np
from sqlalchemy import text

from app.core.config import get_settings

logger = logging.getLogger(__name__)

LOCAL_MODEL = "BAAI/bge-m3"
ADAPTER_FILE = "data/embeddings/hyper_adapter.npz"
_adapter: dict[str, Any] | None = None
CLOVA_MODEL = "v2"
VECTORS_SQL = "SELECT tbl_id, embedding::real[] AS embedding FROM catalog.table_embeddings"
QUERY_TIMEOUT = 3.0
CACHE_SIZE = 1000

_encoder: Any = None
_cache: OrderedDict[tuple[str, str], np.ndarray] = OrderedDict()


async def load_matrix(conn: Any, ids: list[str]) -> np.ndarray | None:
    found = await conn.execute(text("SELECT to_regclass('catalog.table_embeddings')"))
    if found.scalar() is None:
        logger.warning("table_embeddings 없음 → 키워드 검색만 (pipeline.build_embeddings 실행)")
        return None
    rows = {r.tbl_id: r.embedding for r in (await conn.execute(text(VECTORS_SQL))).all()}
    missing = [i for i in ids if i not in rows]
    if missing:
        logger.warning("표 벡터 %d개 빠짐 → 키워드 검색만 (예: %s)", len(missing), missing[:3])
        return None
    return np.array([rows[i] for i in ids], dtype=np.float32)


def _local_encode(question: str) -> list[float]:
    global _encoder
    if _encoder is None:
        from sentence_transformers import SentenceTransformer

        _encoder = SentenceTransformer(LOCAL_MODEL)
    return _encoder.encode([question], normalize_embeddings=True)[0]


async def warm_up() -> None:
    if get_settings().embedding_backend == "local":
        await asyncio.to_thread(_local_encode, "준비")


async def embed_query(question: str) -> np.ndarray | None:
    backend = get_settings().embedding_backend
    if backend == "off":
        return None
    key = (backend, question.strip())
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    try:
        if backend == "local":
            vector = await asyncio.to_thread(_local_encode, question)
        else:
            from app.services import embedding

            vector = await embedding.embed_once(question, CLOVA_MODEL, QUERY_TIMEOUT)
    except Exception as e:
        logger.warning("질문 임베딩 실패 → 키워드 검색만: %s: %s", type(e).__name__, e)
        return None
    v = np.asarray(vector, dtype=np.float32)
    v = v / np.linalg.norm(v)
    _cache[key] = v
    if len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return v


def load_adapter(ids: list[str]) -> bool:
    global _adapter
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / ADAPTER_FILE
    if not path.exists():
        _adapter = None
        return False
    z = np.load(path, allow_pickle=True)
    pos = {t: i for i, t in enumerate(z["ids"].tolist())}
    missing = [i for i in ids if i not in pos]
    if missing:
        logger.warning("어댑터 표 벡터 %d개 빠짐 → 코사인만 (예: %s)", len(missing), missing[:3])
        _adapter = None
        return False
    order = [pos[i] for i in ids]
    _adapter = {
        "base": z["base"][order].astype(np.float32),
        "hyper": z["hyper"][order].astype(np.float32),
        "W": z["W"].astype(np.float32),
        "b": z["b"].astype(np.float32),
        "scale": float(np.exp(float(z["log_scale"]))),
    }
    return True


def adapter_loaded() -> bool:
    return _adapter is not None


def _expmap0(v: np.ndarray) -> np.ndarray:
    n = max(float(np.linalg.norm(v)), 1e-6)
    return np.concatenate([[np.cosh(n)], np.sinh(n) * v / n])


def adapter_scores(query: np.ndarray) -> list[float]:
    assert _adapter is not None
    base = _adapter["base"] @ query
    h = _expmap0((_adapter["W"] @ query + _adapter["b"]) * _adapter["scale"])
    hx = _adapter["hyper"]
    inner = -h[0] * hx[:, 0] + hx[:, 1:] @ h[1:]
    d = -np.arccosh(np.maximum(-inner, 1.0 + 1e-6))
    lo, hi = d.min(), d.max()
    return (base.min() + (d - lo) / max(hi - lo, 1e-9) * (base.max() - base.min())).tolist()


def scores(matrix: np.ndarray, query: np.ndarray) -> list[float]:
    if _adapter is not None:
        return adapter_scores(query)
    return (matrix @ query).tolist()
