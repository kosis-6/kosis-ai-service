import numpy as np

from pipeline import build_embeddings as be


def test_vector_literal() -> None:
    assert be.vector_literal(np.array([0.5, -0.25, 1e-8], dtype=np.float32)) == "[0.5,-0.25,1e-08]"


def test_cache_reorders_and_requires_all_ids(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(be, "EMB_DIR", tmp_path)
    vectors = np.array([[1.0, 0.0], [0.0, 1.0], [0.6, 0.8]], dtype=np.float32)
    be.save_cache("bge-m3", "full", ["A", "B", "C"], vectors)
    got = be.load_cache("bge-m3", "full", ["C", "A"])
    assert got.tolist() == [vectors[2].tolist(), vectors[0].tolist()]
    assert be.load_cache("bge-m3", "full", ["A", "D"]) is None
    assert be.load_cache("bge-m3", "name", ["A"]) is None
