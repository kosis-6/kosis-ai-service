from app.services import search as se


def table(tbl_id, name, values):
    return {
        "tbl_id": tbl_id,
        "classifications": [{"name": "분류", "values": values}],
        "name_n": name,
        "path_n": "",
        "meta_n": " ".join(values),
    }


def test_compound_scores_after_table_list_id_reuse(monkeypatch):
    monkeypatch.setattr(se, "id", lambda _: 1, raising=False)
    monkeypatch.setattr(se, "_value_index", None)
    se.value_index([table("OLD", "다른표", ["지체"])])
    tables = [
        table("A", "연령및성별장애인인구", ["합계"]),
        table("B", "장애유형별장애인인구", ["합계", "청각"]),
    ]
    tokens = [{"variants": ["청각장애인"], "value": False}]
    a, b = se.keyword_scores(tables, tokens)
    assert b > a


def test_value_like_after_table_list_id_reuse(monkeypatch):
    monkeypatch.setattr(se, "id", lambda _: 1, raising=False)
    monkeypatch.setattr(se, "_value_like", {})
    assert not se.value_like([table("OLD", "아파트", [])], "아파트")
    tables = [table(str(i), "다른표", ["아파트"]) for i in range(20)]
    assert se.value_like(tables, "아파트")
