"""통계표 임베딩 적재: stat_tables → table_embeddings (실전 2 F-02)

표마다 검색 문서(pipeline/search_docs.py)를 만들어 임베딩하고 pgvector에 넣는다.
검색(app/services/search.py)은 이 벡터와 질문 벡터의 코사인 유사도를 키워드 점수와 함께 쓴다.

모델
  bge-m3    로컬에서 sentence-transformers로 계산 (Apple GPU 약 1분). 기본값.
            서버 이미지에는 sentence-transformers가 없으므로 내 PC에서 계산해 캐시를 만든다
  clova-v2  CLOVA Studio v2 (bge-m3와 같은 모델). 분당 60건 제한이라 약 40분

캐시: data/embeddings/{모델}__{문서구성}.npy / .ids
      (노트북 notebooks/embedding_compare.ipynb와 같은 파일)
      표 ID가 모두 들어 있으면 다시 계산하지 않는다. 표 순서는 달라도 된다.

출력: table_embeddings (tbl_id, model, preset, document, embedding vector(1024))
      stat_tables에서 다시 만들 수 있으므로 실행할 때마다 새로 만든다.

실행
  # 1. 내 PC에서 임베딩 계산 → 캐시 (카탈로그 파일로 계산하므로 DB 불필요)
  uv run --with sentence-transformers python -m pipeline.build_embeddings --dry-run
  # 2. 서버 컨테이너에서 캐시를 DB에 적재
  docker compose exec api python -m pipeline.build_embeddings
"""

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from pipeline.search_docs import PRESETS, build

ROOT = Path(__file__).resolve().parent.parent
EMB_DIR = ROOT / "data" / "embeddings"
MODELS = {"bge-m3": "BAAI/bge-m3", "clova-v2": "v2"}
DIM = 1024
BATCH_SIZE = 16

DDL = f"""
DROP TABLE IF EXISTS table_embeddings;
CREATE TABLE table_embeddings (
    tbl_id     text PRIMARY KEY,
    model      text NOT NULL,
    preset     text NOT NULL,
    document   text NOT NULL,
    embedding  vector({DIM}) NOT NULL
);
"""


def cache_path(model: str, preset: str) -> Path:
    return EMB_DIR / f"{model}__{preset}.npy"


def load_cache(model: str, preset: str, ids: list[str]) -> np.ndarray | None:
    """캐시에 ids가 모두 있으면 ids 순서로 다시 맞춘 행렬, 아니면 None"""
    path = cache_path(model, preset)
    id_path = path.with_suffix(".ids")
    if not (path.exists() and id_path.exists()):
        return None
    cached_ids = id_path.read_text().split()
    index = {tbl_id: i for i, tbl_id in enumerate(cached_ids)}
    if any(i not in index for i in ids):
        return None
    return np.load(path)[[index[i] for i in ids]]


def save_cache(model: str, preset: str, ids: list[str], vectors: np.ndarray) -> None:
    EMB_DIR.mkdir(parents=True, exist_ok=True)
    np.save(cache_path(model, preset), vectors)
    cache_path(model, preset).with_suffix(".ids").write_text("\n".join(ids))


def embed(model: str, docs: list[str]) -> np.ndarray:
    if model == "bge-m3":
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError:
            sys.exit(
                "sentence-transformers가 없습니다. 내 PC에서 실행하세요:\n  uv run --with "
                "sentence-transformers python -m pipeline.build_embeddings --dry-run"
            )
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        encoder = SentenceTransformer(MODELS[model], device=device)
        return encoder.encode(
            docs, batch_size=BATCH_SIZE, normalize_embeddings=True, show_progress_bar=True
        )
    import asyncio

    from app.services import embedding

    raw = np.array(asyncio.run(embedding.embed(docs, MODELS[model])), dtype=np.float32)
    return raw / np.linalg.norm(raw, axis=1, keepdims=True)


def vectors_for(
    tables: list[dict[str, Any]], model: str, preset: str
) -> tuple[list[str], np.ndarray, str]:
    """(문서, 벡터, 출처). 캐시가 있으면 쓰고 없으면 계산해 캐시에 남긴다"""
    ids = [t["tbl_id"] for t in tables]
    docs = [build(t, preset) for t in tables]
    cached = load_cache(model, preset, ids)
    if cached is not None:
        return docs, cached, f"캐시 {cache_path(model, preset).relative_to(ROOT)}"
    start = time.perf_counter()
    vectors = embed(model, docs)
    save_cache(model, preset, ids, vectors)
    return docs, vectors, f"계산 {time.perf_counter() - start:.0f}초 → 캐시 저장"


def vector_literal(v: np.ndarray) -> str:
    """pgvector 입력 형식 '[0.1,0.2,…]'"""
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def load(
    tables: list[dict[str, Any]], docs: list[str], vectors: np.ndarray, model: str,
    preset: str, url: str,
) -> int:  # fmt: skip
    import psycopg

    cols = ["tbl_id", "model", "preset", "document", "embedding"]
    with psycopg.connect(url) as conn:  # 블록이 끝나면 커밋, 오류면 전부 되돌림
        conn.execute(DDL)
        with conn.cursor().copy(f"COPY table_embeddings ({', '.join(cols)}) FROM STDIN") as copy:
            for t, doc, v in zip(tables, docs, vectors, strict=True):
                copy.write_row((t["tbl_id"], model, preset, doc, vector_literal(v)))
        return conn.execute("SELECT count(*) FROM table_embeddings").fetchone()[0]


def read_tables(url: str) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT tbl_id, survey_name, table_name, classifications, items, path_texts"
            " FROM stat_tables ORDER BY tbl_id"
        ).fetchall()


def main() -> int:
    from pipeline.load_catalog import database_url, read_catalog

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", choices=list(MODELS), default="bge-m3")
    parser.add_argument("--preset", choices=PRESETS, default="full", help="검색 문서 구성")
    parser.add_argument("--dry-run", action="store_true", help="DB 없이 카탈로그로 계산·캐시만")
    args = parser.parse_args()

    if args.dry_run:
        tables = read_catalog()
    else:
        tables = read_tables(database_url())
        if not tables:
            print("stat_tables가 비어 있음. pipeline.load_catalog를 먼저 실행", file=sys.stderr)
            return 1
    docs, vectors, source = vectors_for(tables, args.model, args.preset)
    print(f"{args.model} · {args.preset} · 표 {len(tables)}개 · {vectors.shape} · {source}")
    if vectors.shape != (len(tables), DIM):
        print(f"벡터 크기가 ({len(tables)}, {DIM})이 아님", file=sys.stderr)
        return 1
    if args.dry_run:
        print("--dry-run: DB에는 넣지 않음")
        return 0
    n = load(tables, docs, vectors, args.model, args.preset, database_url())
    print(f"table_embeddings 적재 완료: {n}행")
    return 0


if __name__ == "__main__":
    sys.exit(main())
