"""조회 지표는 연 단위 시점만 읽는다. 표 전체를 적재하면 월·분기 행(시점 202401 등)이 같은 표에
섞여 들어와, 거르지 않으면 수록 기간이 2000~202402년처럼 잡혀 정상 질문이 답변 불가가 된다
(2026-11-09 로컬 DB에 월 행을 넣어 확인: 조건 없이 5,438개 중 4,522개 결과가 바뀜)"""

import inspect
import re

from app.services import lookup


def test_item_years_reads_yearly_rows_only(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(lookup, "_in", lambda *a: ("item_key in (?)", [1]))
    monkeypatch.setattr(lookup, "_tbl_key", lambda tbl: 7)

    def rows(sql, params):
        seen.append(sql)
        return [{"lo": 1, "hi": 2}]

    monkeypatch.setattr(lookup, "_rows", rows)
    lookup._item_years.__wrapped__("DT_TEST", ("T1",))
    assert lookup.YEARLY in seen[0]


def test_every_stat_value_query_filters_period_type() -> None:
    # stat_value를 읽는 쿼리를 새로 만들 때 YEARLY 조건을 빠뜨리지 않게 한다
    source = inspect.getsource(lookup)
    queries = len(re.findall(r"from stat_value", source))
    assert queries == len(re.findall(r"\{YEARLY\}", source)) >= 3
