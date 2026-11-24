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
        ("청년층(19-34세)", None),
    ],
)
def test_parse_age(name, band):
    assert L.parse_age(name) == band


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
        L._age_codes(SRC, (31, 34))
    with pytest.raises(L.AgeMismatch):
        L._age_codes(SRC, (19, 32))


def test_한_살_값_여러_개로_시작을_맞춘다(mixed):
    assert L._age_codes(SRC, (22, 34)) == ["025003", "025004", "025005", "030", "035"]
