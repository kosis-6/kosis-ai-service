"""질문 임베딩과 표 벡터 점수 (실전 2 F-04 하이브리드 검색의 임베딩 쪽)

표 벡터    pipeline/build_embeddings.py가 table_embeddings에 넣은 bge-m3 벡터(1024차원, 길이 1).
           search.load_tables가 표 메타데이터와 함께 한 번 읽어 메모리에 둔다.
           표가 2천여 개라 질문마다 전체 코사인 유사도를 numpy로 계산한다 (1ms 안팎).
           표가 크게 늘면 pgvector의 `<=>` 검색으로 바꾼다.
질문 벡터  설정 EMBEDDING_BACKEND
           clova  CLOVA Studio v2 (기본값). bge-m3와 같은 모델이라 표 벡터와 섞어 쓴다
                  (같은 표의 벡터 코사인 평균 0.99999,
                  eval/results/embedding_compare_full_20261031.md)
           local  sentence-transformers로 bge-m3를 직접 돌린다. 서버 이미지에는 없으므로
                  `uv run --with sentence-transformers`로 실행할 때만 쓴다
           off    임베딩을 쓰지 않는다 (키워드 검색만)
질문 임베딩이 실패하거나 표 벡터가 없으면 None을 돌려주고, 검색은 키워드만으로 이어간다.
"""

import asyncio
import logging
from typing import Any

import numpy as np
from sqlalchemy import text

from app.core.config import get_settings

logger = logging.getLogger(__name__)

LOCAL_MODEL = "BAAI/bge-m3"
CLOVA_MODEL = "v2"
VECTORS_SQL = "SELECT tbl_id, embedding::real[] AS embedding FROM table_embeddings"

_encoder: Any = None


async def load_matrix(conn: Any, ids: list[str]) -> np.ndarray | None:
    """ids 순서대로 표 벡터 행렬. 테이블이 없거나 빠진 표가 있으면 None"""
    if (await conn.execute(text("SELECT to_regclass('table_embeddings')"))).scalar() is None:
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


async def embed_query(question: str) -> np.ndarray | None:
    """질문 → 길이 1 벡터. 끄거나 실패하면 None"""
    backend = get_settings().embedding_backend
    if backend == "off":
        return None
    try:
        if backend == "local":
            vector = await asyncio.to_thread(_local_encode, question)
        else:
            from app.services import embedding

            vector = (await embedding.embed([question], CLOVA_MODEL))[0]
    except Exception as e:  # 임베딩 장애·호출 제한이어도 키워드 검색으로 답은 한다
        logger.warning("질문 임베딩 실패 → 키워드 검색만: %s: %s", type(e).__name__, e)
        return None
    v = np.asarray(vector, dtype=np.float32)
    return v / np.linalg.norm(v)


def scores(matrix: np.ndarray, query: np.ndarray) -> list[float]:
    """표마다 질문과의 코사인 유사도 (둘 다 길이 1이라 내적)"""
    return (matrix @ query).tolist()
