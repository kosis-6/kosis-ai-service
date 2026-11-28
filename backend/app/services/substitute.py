"""대안 답변: 답변 불가 대신 조건 하나를 가장 가까운 것으로 바꿔 값을 준다.

명세: docs/clarify_refusal_spec_2026-11-27.md '대안 답변 기능'
- 한 번에 조건 하나만 바꾼다 (연도 · 연령 구간 · 지역 단위)
- 첫 문장에서 무엇을 바꿨는지 밝힌다 (result["lead"])
- 바꾼 조건은 result["changed_condition"]에 남긴다 (화면 · 채점이 대안 답변으로 읽는 필드)

여기 함수는 계산하지 않는다. 결과(거절)와 질문 조건을 보고 '무엇으로 바꿀지'만 정한다.
"""

import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from app.services import agegroup
from app.services.josa import josa

DONG_TABLE = "DT_1B04005N"  # 행정구역(읍면동)별 인구: 읍면동 → 시군구를 찾는 데 쓴다
DONG = re.compile(r"^([가-힣]{1,6}?)(?:\d*|본)?(동|읍|면)$")
RATE_WORDS = re.compile(r"율|률|비$|비율|지수|평균|중위|기대수명")
AGE_RANGE = re.compile(
    r"(?:만\s*)?(\d+)\s*(?:세|살)\s*(?:부터|에서)\s*(?:만\s*)?"
    r"(\d+)\s*(?:세|살)\s*(?:까지|사이)?"
)
AGE_POINT = re.compile(r"(?:만\s*)?\d+\s*(?:세|살)")


@dataclass
class Swap:
    name: str  # 연도 · 연령 · 지역
    before: str
    after: str
    change: dict[str, Any]  # 질문 조건(parsed)에 덮어쓸 값
    lead: str  # 답 첫 문장
    others: list[str] = field(default_factory=list)  # 다른 대안 (후속 버튼용)
    why: str = ""  # 바꾼 이유만 (follow가 이어 쓸 때)
    because: str = ""  # 바꾸게 된 원래 status (region_not_in_year · out_of_range …)

    def record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "from": self.before,
            "to": self.after,
            "others": self.others,
            "applied": self.change,
            "because": self.because,
        }


def _single_year(q: dict) -> int | None:
    if q.get("type") not in (None, "value", "composition") or q.get("start_year"):
        return None
    y = q.get("year")
    return y if isinstance(y, int) else None


def year_swap(q: dict, result: dict) -> Swap | None:
    """그해에 없던 지역 → 생긴 해, 수록 기간 전 연도 → 첫해. 미래 연도는 바꾸지 않는다"""
    y = _single_year(q)
    if y is None:
        return None
    status = result.get("status")
    if status == "region_not_in_year" and (born := result.get("available_from")):
        if born <= y:
            return None
        where = result.get("region") or "이 지역"
        lead = f"{josa(where, '은는')} {born}년에 생겨 {y}년 자료가 없어 {born}년 값으로 답합니다."
        why = f"{josa(where, '은는')} {born}년에 생겼고"
        return Swap("연도", f"{y}년", f"{born}년", {"year": born}, lead, why=why, because=status)
    if status == "out_of_range" and (span := result.get("available_years")):
        lo, _ = span
        if not isinstance(lo, int) or y >= lo:
            return None
        what = result.get("indicator") or "이 통계"
        lead = (
            f"{josa(what, '은는')} {lo}년부터 자료가 있어 {y}년 대신 가장 가까운 {lo}년 값으로 "
            "답합니다."
        )
        return Swap("연도", f"{y}년", f"{lo}년", {"year": lo}, lead, because=status)
    return None


def follow(first: Swap, q: dict, again: dict) -> Swap | None:
    """연도를 바꿨는데 그 연도도 수록 기간 전이면 자료가 있는 첫해로 한 번 더.
    바꾸는 조건은 여전히 연도 하나다"""
    if first.name != "연도" or (second := year_swap(q, again)) is None:
        return None
    lead = (
        f"{first.why} 자료는 {second.after}부터 있어 {first.before} 대신 가장 가까운 "
        f"{second.after} 값으로 답합니다."
        if first.why
        else second.lead.replace(second.before, first.before)
    )
    # 두 번째로 바꾼 까닭(수록 기간 전)이 마지막 값을 정했으니 그 까닭을 남긴다
    return Swap("연도", first.before, second.after, second.change, lead, because=second.because)


OPEN_END = 100  # 'N세 이상' 구간의 끝으로 보는 나이


def _overlap(asked: tuple[int, int], band: tuple[int, int | None]) -> Fraction:
    """겹친 나이 수 ÷ 두 구간을 합친 나이 수. 'N세 이상'처럼 넓은 구간은 낮게 나온다"""
    lo, hi = asked
    a, b = band
    b = max(OPEN_END, a) if b is None else b
    inter = max(0, min(hi, b) - max(lo, a) + 1)
    union = max(hi, b) - min(lo, a) + 1
    return Fraction(inter, union)


def _label(band: tuple[int, int | None]) -> str:
    lo, hi = band
    if hi is None:
        return f"{lo}세 이상"
    return f"{lo}세" if lo == hi else f"{lo}~{hi}세"


def nearest_band(
    asked: tuple[int, int], bands: list[tuple[int, int | None]], rate: bool
) -> tuple[tuple[int, int | None], list[tuple[int, int | None]]] | None:
    """가장 많이 겹치는 구간. 같은 만큼 겹치는 구간이 여럿이면 건수는 이어 붙인 합,
    비율은 아래 구간.
    돌려주는 것: (고른 구간, 다른 후보 구간들)"""
    scored = [(b, _overlap(asked, b)) for b in bands]
    scored = [(b, s) for b, s in scored if s > 0]
    if not scored:
        return None
    top = max(s for _, s in scored)
    best = sorted((b for b, s in scored if s == top), key=lambda b: b[0])
    others = [b for b, _ in sorted(scored, key=lambda x: (-x[1], x[0][0])) if b not in best]
    if len(best) == 1 or rate:
        return best[0], best[1:] + others
    lo, hi = best[0][0], best[-1][1]
    joined = all(
        prev[1] is not None and prev[1] + 1 == nxt[0]
        for prev, nxt in zip(best, best[1:], strict=False)
    )
    if not joined:
        return best[0], best[1:] + others
    return (lo, hi), best + others


def band_swap(q: dict, result: dict) -> Swap | None:
    """표에 없는 연령 구간 → 가장 많이 겹치는 표의 구간"""
    if result.get("status") != "band_mismatch":
        return None
    bands = [tuple(b) for b in result.get("available_band_ranges") or []]
    ages = q.get("ages") or []
    if not bands or len(ages) != 1 or ages[0][1] is None:
        return None
    asked = (ages[0][0], ages[0][1])
    rate = bool(RATE_WORDS.search(str(result.get("indicator") or q.get("indicator") or "")))
    picked = nearest_band(asked, bands, rate)
    if picked is None:
        return None
    band, others = picked
    new = [[band[0], band[1]]]
    lead = f"표에 {_label(asked)} 구간이 없어 가장 가까운 {_label(band)} 값으로 답합니다."
    return Swap(
        "연령",
        _label(asked),
        _label(band),
        {"ages": new, "age_group": None, "age_note": None, "born": None},
        lead,
        [_label(b) for b in others[:2]],
        because=str(result.get("status") or ""),
    )


def dong_parent(name: str, labels: list[tuple[str, str]]) -> str | None:
    """'반포동' → '서울특별시 서초구'. 읍면동별 인구 표의 지역 목록에서 같은 이름으로 시작하는
    읍면동을 찾고, 모두 한 시군구(코드 앞 5자리)에 있을 때만 그 시군구를 돌려준다"""
    m = DONG.match(name)
    if not m:
        return None
    stem, tail = m.groups()
    by_code = dict(labels)
    hits = [
        code
        for code, label in labels
        if len(code) == 10
        and label.startswith(stem)
        and label.endswith(tail)
        and re.fullmatch(rf"{re.escape(stem)}(?:\d*|본|제\d+)?{tail}", label)
    ]
    parents = {code[:5] for code in hits}
    if len(parents) != 1:
        return None
    parent = parents.pop()
    sigungu, sido = by_code.get(parent), by_code.get(parent[:2])
    if not sigungu:
        return None
    return f"{sido} {sigungu}" if sido else sigungu


def region_swap(q: dict, result: dict, labels: list[tuple[str, str]]) -> Swap | None:
    """읍면동 → 속한 시군구"""
    if result.get("status") != "unknown_region":
        return None
    name = str(result.get("region") or "")
    regions = q.get("regions") or []
    if name not in regions or len(regions) != 1:
        return None
    parent = result.get("alternative") if q.get("sub_area") else dong_parent(name, labels)
    if not parent or "," in parent:
        return None
    short = parent.split()[-1]
    lead = f"'{name}' 같은 읍면동 단위 자료는 없어 {short} 값으로 답합니다."
    change = {"regions": [parent], "sub_area": None}
    return Swap("지역", name, parent, change, lead, because="unknown_region")


def plan(q: dict, result: dict, dong_labels=None) -> Swap | None:
    if q.get("substituted") or result.get("status") == "ok":
        return None
    if swap := year_swap(q, result) or band_swap(q, result):
        return swap
    if result.get("status") == "unknown_region":
        labels = dong_labels() if callable(dong_labels) else (dong_labels or [])
        return region_swap(q, result, list(labels))
    return None


def mark(result: dict, swap: Swap) -> dict:
    """다시 계산한 결과에 바꾼 조건과 첫 문장을 단다"""
    result["changed_condition"] = swap.record()
    lead = result.get("lead")
    result["lead"] = f"{swap.lead} {lead}" if lead else swap.lead
    return result


def applied_conditions(parsed: dict, result: dict) -> dict:
    change = (result.get("changed_condition") or {}).get("applied")
    return {**parsed, **change} if change else parsed


def is_age_item(item: str) -> bool:
    text = agegroup.native_to_digits(item).strip()
    span = agegroup.span(text)
    return bool(
        AGE_RANGE.fullmatch(text) or AGE_POINT.fullmatch(text) or (span and span.expr == text)
    )


def age_band_items(checks: dict) -> list[str]:
    """연령 구간만 맞지 않은 경우 바꿔도 되는 검색 항목을 돌려준다."""
    failed = [key for key, ok in checks.items() if ok is False]
    if checks.get("class:연령") is not True:
        return []
    items = []
    for key in failed:
        if not key.startswith("item:"):
            return []
        item = key.removeprefix("item:")
        if not is_age_item(item):
            return []
        items.append(item)
    return items


def table_band_swap(
    q: dict, checks: dict, bands: list[tuple[int, int | None]], name: str
) -> Swap | None:
    """표 경로: 후보 표가 연령 칸만 ✘ (연령 분류는 있음) → 표의 가장 가까운 구간"""
    if not age_band_items(checks):
        return None
    result = {"status": "band_mismatch", "available_band_ranges": bands, "indicator": name}
    return band_swap(q, result)


def table_region_swap(q: dict, checks: dict, resolve, name: str) -> Swap | None:
    """표 경로: 후보 표가 지역 단위만 ✘ → 바로 위 지역 (시군구 → 시도)"""
    failed = [k for k, ok in (checks or {}).items() if ok is False]
    regions = q.get("regions") or []
    if failed != ["region_level"] or len(regions) != 1:
        return None
    reg = resolve(regions[0], q.get("year"))
    if reg.get("status") != "ok" or reg.get("kind") != "시군구" or not reg.get("sido_name"):
        return None
    sido = reg["sido_name"]
    what = f"'{name}' 통계는" if name else "이 통계는"
    lead = f"{what} 시도 단위까지만 집계돼 {sido} 값으로 답합니다."
    shown = reg.get("display") or regions[0]
    return Swap("지역", shown, sido, {"regions": [sido]}, lead, because="region_level")


def mark_response(res: dict, swap: Swap) -> dict:
    """표 경로 응답(qa.answer 모양)에 바꾼 조건과 첫 문장을 단다"""
    result = res.setdefault("result", {})
    if isinstance(result, dict):
        result["changed_condition"] = swap.record()
        res["parsed"] = applied_conditions(res.get("parsed") or {}, result)
    answer = res.get("answer") or ""
    if not answer.startswith(swap.lead):
        res["answer"] = f"{swap.lead}\n{answer}" if answer else swap.lead
    return res
