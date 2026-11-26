"""숫자 구간 축의 '구간 합' (11/26, A_20 뒤 표 경로 보완).

연령은 lookup._age_codes 가 "35세 이상 → 35-39 + 40-44 + …"처럼 구간 여러 개를 합친다. 체중 ·
결혼생활기간 · 동거기간처럼 단위가 있는 다른 구간 축은 칸 하나에만 이어서 "2.5kg 미만"
(1.5미만 + 1.5-1.9 + 2.0-2.4)을 못 냈다. 여기서 칸 이름을 숫자 범위로 읽고, 질문의 범위가 덮는
칸들을 모아 준다.

정의어(catalog_notes.toml [[band_words]]): "고령산모 = 모의 연령 35세 이상", "저체중아 = 출생시 체중
2.5kg 미만"처럼 통계 표준 정의가 있는 말은 숫자 범위로 바꿔 같은 방식으로 잇는다."""

from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from app.services import lookup
from app.services.indicators import NOTES_PATH

if TYPE_CHECKING:
    from app.services.dynamic_indicator import Dim

UNITS = {
    "㎏": "kg",
    "kg": "kg",
    "g": "g",
    "㎝": "cm",
    "cm": "cm",
    "년": "년",
    "개월": "개월",
    "주": "주",
}
NUM = r"(\d+(?:\.\d+)?)"
UNIT = r"(㎏|kg|g|㎝|cm|년|개월|주)"


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", re.sub(r"\s+", "", str(text))).lower()


@dataclass(frozen=True)
class Band:
    lo: float | None  # 포함
    hi: (
        float | None
    )  # 포함 (칸 "2.0 - 2.4㎏" 은 2.4, 질문 "2.5kg 미만" 은 2.5 미만을 뜻하므로 open_hi)
    unit: str
    open_hi: bool = False  # hi 를 포함하지 않음 (미만)
    open_lo: bool = False  # lo 를 포함하지 않음 (초과)


# 칸 이름 → 구간. "1.5㎏미만" · "1.5 - 1.9㎏" · "4.5㎏이상" · "20년이상" · "10~14년" · "0∼4년"
LABEL_PATTERNS = [
    (
        re.compile(rf"^{NUM}{UNIT}?[-~∼]{NUM}{UNIT}$"),
        lambda m: Band(float(m[1]), float(m[3]), UNITS[m[4]]),
    ),
    (
        re.compile(rf"^{NUM}{UNIT}미만$"),
        lambda m: Band(None, float(m[1]), UNITS[m[2]], open_hi=True),
    ),
    (re.compile(rf"^{NUM}{UNIT}이하$"), lambda m: Band(None, float(m[1]), UNITS[m[2]])),
    (re.compile(rf"^{NUM}{UNIT}(?:이상|초과)$"), lambda m: Band(float(m[1]), None, UNITS[m[2]])),
    (re.compile(rf"^{NUM}{UNIT}$"), lambda m: Band(float(m[1]), float(m[1]), UNITS[m[2]])),
]
# 질문의 범위. "2.5kg 미만" · "3kg 이상" · "2.5~3.9kg" · "20년 이상" · "4kg 초과"
ASK_PATTERNS = [
    (
        re.compile(rf"{NUM}\s*{UNIT}?\s*[-~∼]\s*{NUM}\s*{UNIT}"),
        lambda m: Band(float(m[1]), float(m[3]), UNITS[m[4]]),
    ),
    (
        re.compile(rf"{NUM}\s*{UNIT}\s*미만"),
        lambda m: Band(None, float(m[1]), UNITS[m[2]], open_hi=True),
    ),
    (re.compile(rf"{NUM}\s*{UNIT}\s*이하"), lambda m: Band(None, float(m[1]), UNITS[m[2]])),
    (
        re.compile(rf"{NUM}\s*{UNIT}\s*초과"),
        lambda m: Band(float(m[1]), None, UNITS[m[2]], open_lo=True),
    ),
    (
        re.compile(rf"{NUM}\s*{UNIT}\s*(?:이상|넘는|넘게)"),
        lambda m: Band(float(m[1]), None, UNITS[m[2]]),
    ),
]


def parse_label(name: str) -> Band | None:
    text = _norm(name)
    for pattern, make in LABEL_PATTERNS:
        if m := pattern.match(text):
            return make(m)
    return None


def parse_ask(text: str) -> list[tuple[Band, str]]:
    """질문 글에서 단위 있는 범위를 모두 찾는다 → [(구간, 질문의 표현)]"""
    out = []
    norm = unicodedata.normalize("NFKC", text)
    for pattern, make in ASK_PATTERNS:
        for m in pattern.finditer(norm):
            if not any(m[0] in shown or shown in m[0] for _, shown in out):
                out.append((make(m), m[0].strip()))
    return out


def _within(band: Band, ask: Band) -> bool:
    if band.lo is None and ask.lo is not None:
        return False
    if band.hi is None and ask.hi is not None:
        return False
    if ask.lo is not None and band.lo is not None:
        if band.lo < ask.lo or (ask.open_lo and band.lo == ask.lo):
            return False
    if ask.hi is not None and band.hi is not None:
        if band.hi > ask.hi or (ask.open_hi and band.hi >= ask.hi and not band.open_hi):
            return False
    return True


def _straddles(band: Band, ask: Band) -> bool:
    """칸이 질문 범위의 경계를 걸침 (예: 2.5kg 미만을 물었는데 2.0-2.9㎏ 칸)"""
    lo = band.lo if band.lo is not None else float("-inf")
    hi = band.hi if band.hi is not None else float("inf")
    if ask.hi is not None and lo < ask.hi < hi:
        return True
    return ask.lo is not None and lo < ask.lo < hi


def _contains(outer: Band, inner: Band) -> bool:
    lo_ok = outer.lo is None or (inner.lo is not None and inner.lo >= outer.lo)
    hi_ok = outer.hi is None or (inner.hi is not None and inner.hi <= outer.hi)
    return lo_ok and hi_ok and outer != inner


def union_codes(dim: Dim, ask: Band, *, age: bool = False) -> list[str] | None:
    """질문 범위가 덮는 칸 코드들. 칸이 경계를 걸치거나 덮는 칸이 없으면 None.
    겹치는 칸(동거기간 '20년이상'과 '20~24년')이 있으면 넓은 칸 하나를 우선한다"""
    bands: dict[str, Band] = {}
    for code, name in dim.codes.items():
        if code == dim.total():
            continue
        if age:
            parsed = lookup.parse_age(name)
            b = (
                Band(float(parsed[0]), None if parsed[1] is None else float(parsed[1]), "세")
                if parsed
                else None
            )
        else:
            b = parse_label(name)
        if b is not None and b.unit == ask.unit:
            bands[code] = b
    if not bands:
        return None
    # 다른 칸을 통째로 품는 넓은 칸("20년이상"이 "20∼24년"을 품음)은 경계 걸침 검사에서 뺀다
    aggregate = {
        c for c, b in bands.items() if any(_contains(b, o) for o in bands.values() if o != b)
    }
    if any(_straddles(b, ask) for c, b in bands.items() if c not in aggregate):
        return None
    inside = {c: b for c, b in bands.items() if _within(b, ask)}
    if not inside:
        return None
    exact = [c for c, b in inside.items() if b == ask or (b.lo == ask.lo and b.hi == ask.hi)]
    if exact:
        return exact[:1]

    # 겹치는 칸 제거: 다른 선택 칸을 통째로 품는 넓은 칸이 있으면 좁은 칸들을 뺀다
    codes = [c for c, b in inside.items() if not any(_contains(o, b) for o in inside.values())]
    return sorted(codes, key=lambda c: inside[c].lo if inside[c].lo is not None else -1)


# ---------------------------------------------------------------- 정의어 (toml [[band_words]])
@dataclass(frozen=True)
class Word:
    word: str
    dim: str  # 분류 이름에 들어 있어야 하는 말 ("모의 연령" · "체중")
    range: str  # "35세 이상" · "2.5kg 미만"
    note: str = ""


@lru_cache
def words() -> tuple[Word, ...]:
    with open(NOTES_PATH, "rb") as f:
        raw = tomllib.load(f).get("band_words") or []
    return tuple(Word(w["word"], w["dim"], w["range"], w.get("note", "")) for w in raw)


@dataclass(frozen=True)
class Union:
    dim_no: int
    codes: tuple[str, ...]
    label: str  # 답에 쓰는 말 ("저체중아(2.5kg 미만)")


def find(texts: list[str], dims: list[Dim], region_dim: int | None) -> list[Union]:
    """질문 글(또는 조건 말)에서 단위 있는 범위 · 정의어를 찾아 각 축의 구간 합으로 잇는다"""
    out: list[Union] = []
    seen: set[int] = set()
    joined = " ".join(t for t in texts if t)
    norm = _norm(joined)
    asks: list[tuple[Band, str, str | None]] = [(b, shown, None) for b, shown in parse_ask(joined)]
    for w in words():
        if _norm(w.word) in norm:
            for b, shown in parse_ask(w.range):
                asks.append((b, f"{w.word}({shown})", w.dim))
            if w.range.endswith(("세 이상", "세 미만", "세 이하")) or "세" in w.range:
                if parsed := lookup.parse_age(w.range):
                    asks.append(
                        (
                            Band(
                                float(parsed[0]),
                                None if parsed[1] is None else float(parsed[1]),
                                "세",
                            ),
                            f"{w.word}({w.range})",
                            w.dim,
                        )
                    )
    for ask, shown, dim_word in asks:
        for d in dims:
            if d.no == region_dim or d.no in seen:
                continue
            if dim_word and _norm(dim_word) not in _norm(d.name):
                continue
            age = ask.unit == "세"
            if age and not (dim_word and d.is_age()):
                continue  # 사람 연령은 lookup._age_codes 가 맡는다. 정의어가 가리킨 연령 축만
            codes = union_codes(d, ask, age=age)
            if codes:
                out.append(Union(d.no, tuple(codes), shown))
                seen.add(d.no)
                break
    return out
