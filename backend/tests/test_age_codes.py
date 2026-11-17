"""표의 연령 값 읽기(parse_age)와 연령 구간 → 코드 고르기(_age_codes)"""

import pytest

from app.services import lookup as L


@pytest.mark.parametrize(
    "name, band",
    [
        ("15~19세", (15, 19)),
        ("15 - 19세", (15, 19)),
        ("만 7세~만 12세", (7, 12)),
        ("20세미만", (0, 19)),
        ("85세 이상", (85, None)),
        ("65세 이하", (0, 65)),
        ("90대 이상", (90, None)),
        ("20대이하", (0, 29)),
        ("30대", (30, 39)),
        ("19세", (19, 19)),
        ("만3세", (3, 3)),
        ("60~64", (60, 64)),
        ("청년층(19-34세)", None),  # 다른 구간과 겹치는 묶음은 쓰지 않는다
    ],
)
def test_parse_age(name, band):
    assert L.parse_age(name) == band


# 2020 인구총조사(DT_1PM2002)처럼 5세 구간과 한 살 값이 함께 있는 표
MIXED = (
    ("000", "합계"),
    ("020", "15~19세"),
    *[(f"02000{i}", f"{14 + i}세") for i in range(1, 6)],
    ("025", "20~24세"),
    *[(f"02500{i}", f"{19 + i}세") for i in range(1, 6)],
    ("030", "25~29세"),
    ("035", "30~34세"),
    ("040", "35세이상"),
)
SRC = {"table": "T", "age_dim": "c2", "age_total": "000", "age_min": 15}


@pytest.fixture
def mixed(monkeypatch):
    monkeypatch.setattr(L, "_dim_labels", lambda tbl, dim: MIXED)


def test_5세_경계는_그대로(mixed):
    assert L._age_codes(SRC, (20, 34)) == ["025", "030", "035"]


def test_한_살_값으로_시작을_맞춘다(mixed):
    assert L._age_codes(SRC, (19, 34)) == ["020005", "025", "030", "035"]


def test_끝이_열린_구간(mixed):
    assert L._age_codes(SRC, (30, None)) == ["035", "040"]


def test_최소_연령_이하로_넓게_물으면_합계(mixed):
    assert L._age_codes(SRC, (10, None)) == ["000"]


def test_자를_수_없으면_AgeMismatch(mixed):
    with pytest.raises(L.AgeMismatch):
        L._age_codes(SRC, (31, 34))  # 31세에서 시작하는 값이 없다
    with pytest.raises(L.AgeMismatch):
        L._age_codes(SRC, (19, 32))  # 32세에서 끝나는 값이 없다


def test_한_살_값_여러_개로_시작을_맞춘다(mixed):
    assert L._age_codes(SRC, (22, 34)) == ["025003", "025004", "025005", "030", "035"]
