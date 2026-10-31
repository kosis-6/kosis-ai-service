import numpy as np

from pipeline import build_embeddings as be


def test_vector_literal() -> None:
    assert be.vector_literal(np.array([0.5, -0.25, 1e-8], dtype=np.float32)) == "[0.5,-0.25,1e-08]"


def test_cache_reorders_and_requires_all_ids(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(be, "EMB_DIR", tmp_path)
    vectors = np.array([[1.0, 0.0], [0.0, 1.0], [0.6, 0.8]], dtype=np.float32)
    be.save_cache("bge-m3", "full", ["A", "B", "C"], vectors)
    # 표 순서가 달라도 ID로 맞춘다 (DB는 tbl_id 순, 캐시는 카탈로그 순)
    got = be.load_cache("bge-m3", "full", ["C", "A"])
    assert got.tolist() == [vectors[2].tolist(), vectors[0].tolist()]
    # 캐시에 없는 표가 있으면 다시 계산하도록 None
    assert be.load_cache("bge-m3", "full", ["A", "D"]) is None
    assert be.load_cache("bge-m3", "name", ["A"]) is None
