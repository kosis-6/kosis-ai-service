import asyncio

from app.services import slots as sl


def _slots(question: str, **cond) -> dict:
    s = sl.empty_slots(question)
    s["conditions"].update(cond)
    return sl.correct(question, s)


def test_normalize_fills_missing_keys_and_drops_bad_values() -> None:
    s = sl.normalize(
        "질문",
        {
            "topic": " 혼인 건수 ",
            "target": "",
            "conditions": {"classes": "연령", "sex": "남녀", "region_level": "구", "year": "2024"},
            "query_type": "모름",
        },
    )
    assert s["topic"] == "혼인 건수"
    assert s["target"] is None
    c = s["conditions"]
    assert (c["classes"], c["items"], c["sex"], c["region_level"], c["year"]) == (
        ["연령"],
        [],
        None,
        None,
        2024,
    )
    assert s["query_type"] == "find_table"


def test_year_only_from_question() -> None:
    # LLM이 추측한 연도는 지운다
    assert _slots("외국인 주민 수", year=2024)["conditions"]["year"] is None
    c = _slots("2010년부터 2020년까지 출생아 수")["conditions"]
    assert (c["year"], c["year_end"]) == (2010, 2020)


def test_sex() -> None:
    assert _slots("미혼 여성 인구", sex=None)["conditions"]["sex"] == "여"
    # 성별 말이 없는데 LLM이 '전체'를 넣으면 null
    assert _slots("혼인 건수", sex="전체")["conditions"]["sex"] is None
    both = _slots("남녀별 혼인 건수")["conditions"]
    assert (both["sex"], both["classes"]) == (None, ["성별"])
    assert _slots("남녀 합계 출생아 수")["conditions"]["sex"] == "전체"


def test_region_and_level() -> None:
    c = _slots("부산 외국인 주민 수", region="부산")["conditions"]
    assert (c["region"], c["region_level"]) == ("부산광역시", "시도")
    c = _slots("부산 구별 외국인 주민 수", region="부산")["conditions"]
    assert c["region_level"] == "시군구"
    c = _slots("부산광역시 해운대구 인구", region="부산광역시 해운대구")["conditions"]
    assert c["region_level"] == "시군구"
    # '가구별'의 '구별'은 지역 수준이 아니다
    assert _slots("세대구성별 가구별 인구")["conditions"]["region_level"] is None
    # 지역 말이 없는데 LLM이 넣은 수준은 지운다
    assert _slots("혼인 건수", region_level="시도")["conditions"]["region_level"] is None


def test_classes_and_items_cleanup() -> None:
    s = sl.empty_slots("2010년 시도별 출생아 수")
    s["topic"] = "출생아 수"
    s["conditions"].update(classes=["시도", "연령"], items=["출생아 수", "미혼"])
    c = sl.correct("2010년 시도별 출생아 수", s)["conditions"]
    assert (c["classes"], c["items"]) == (["연령"], ["미혼"])
    c = _slots("남녀별 1인 가구", items=["남", "여"])["conditions"]
    assert (c["classes"], c["items"]) == (["성별"], [])


def test_extract_retries_then_falls_back(monkeypatch) -> None:
    calls = []

    async def fake_chat(question: str) -> str:
        calls.append(question)
        return "JSON을 만들 수 없습니다"

    monkeypatch.setattr(sl, "_chat", fake_chat)
    trace: dict = {}
    s = asyncio.run(sl.extract("2024년 서울 시군구별 여성 인구", trace))
    assert len(calls) == 2
    assert s["parse_error"] is True
    c = s["conditions"]
    assert (c["year"], c["sex"], c["region_level"]) == (2024, "여", "시군구")
    assert len(trace["hcx_raw"]) == 2


def test_extract_reads_json_in_code_block(monkeypatch) -> None:
    async def fake_chat(question: str) -> str:
        return '```json\n{"topic": "귀화자 수", "target": "혼인 귀화자", "conditions": {}}\n```'

    monkeypatch.setattr(sl, "_chat", fake_chat)
    s = asyncio.run(sl.extract("혼인 귀화자 수"))
    assert (s["topic"], s["target"], s["parse_error"]) == ("귀화자 수", "혼인 귀화자", False)


def test_target_cleanup() -> None:
    s = sl.empty_slots("서울 구별 65세 이상 인구")
    s["target"] = "65세 이상"
    s["conditions"]["classes"] = ["구별"]
    out = sl.correct("서울 구별 65세 이상 인구", s)
    assert (out["target"], out["conditions"]["items"], out["conditions"]["classes"]) == (
        None,
        ["65세 이상"],
        [],
    )
    s = sl.empty_slots("외국인 근로자 남자")
    s["target"] = "남자"
    assert sl.correct("외국인 근로자 남자", s)["target"] is None


def test_householder_sex_is_a_class() -> None:
    c = _slots("여성 가구주 가구 수", sex="여")["conditions"]
    assert (c["sex"], c["classes"]) == (None, ["가구주 성별"])
    # 연령별 미혼 여성: 주제와 겹쳐도 값 조건은 남긴다
    s = sl.empty_slots("연령별 미혼 여성 인구")
    s["topic"] = "미혼 인구"
    s["conditions"]["items"] = ["미혼"]
    assert sl.correct("연령별 미혼 여성 인구", s)["conditions"]["items"] == ["미혼"]


def test_short_year_and_projection() -> None:
    assert _slots("95년 인구 통계")["conditions"]["year"] == 1995
    assert _slots("'05년도 출생아")["conditions"]["year"] == 2005
    # '10년 전', '90년생', '20년간'은 연도가 아니다
    for q in ("10년 전 대비 인구", "90년생 인구", "20년간 인구 변화"):
        assert _slots(q)["conditions"]["year"] is None
    # 올해 이후 연도나 예측 표현이면 장래 추계
    assert _slots("2050년 인구")["projection"] is True
    assert _slots("앞으로 인구가 어떻게 될지")["projection"] is True
    assert _slots("2010년 인구")["projection"] is False


def test_region_words() -> None:
    assert _slots("우리 구 인구")["conditions"]["region_level"] == "시군구"
    assert _slots("우리 동네 외국인 주민 수")["conditions"]["region_level"] == "시군구"
    assert _slots("동 단위 인구")["conditions"]["region_level"] == "읍면동"


def test_negation_and_spouse() -> None:
    assert _slots("귀화한 사람 말고 결혼이민자 통계")["exclude"] == ["귀화한"]
    assert _slots("65세 이상 제외 인구")["exclude"] == []  # 숫자·범위 단어는 빼지 않는다
    s = _slots("외국인 여자와 결혼한 한국 남자")
    # 배우자를 가리키는 남녀 단어는 성별 조건이 아니다
    assert (s["spouse"], s["conditions"]["sex"]) == ("외국인 아내", None)
    assert _slots("한국 여자가 외국 남자랑 결혼한 건수")["spouse"] == "외국인 남편"
    # HCX가 배우자 표현을 대상으로 뽑아도 대상 조건으로 쓰지 않는다
    q = "외국인 여자와 결혼한 한국 남자"
    s = sl.empty_slots(q)
    s["target"] = "한국 남자"
    assert sl.correct(q, s)["target"] is None
    # 결혼·이혼 질문이 아니면 그냥 성별
    s = _slots("외국인 여자 수")
    assert (s["spouse"], s["conditions"]["sex"]) == (None, "여")
