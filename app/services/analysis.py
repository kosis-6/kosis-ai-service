"""통계 수치 계산 함수.

LLM이 수치를 직접 계산하지 않도록 모든 계산은 여기서 한다.
결측값은 호출 전에 걸러내고(답변 가능 여부 판단 단계), 반올림은 답변을 만들 때 한다.
"""

from collections.abc import Mapping, Sequence


class AnalysisError(ValueError):
    """계산할 수 없는 입력 (분모 0, 빈 목록 등)."""


def change(base: float, current: float) -> float:
    """증감량: current - base."""
    return current - base


def growth_rate(base: float, current: float) -> float:
    """증감률(%): (current - base) / base * 100."""
    if base == 0:
        raise AnalysisError("기준값이 0이라 증감률을 계산할 수 없습니다.")
    return (current - base) / abs(base) * 100


def point_change(base_rate: float, current_rate: float) -> float:
    """비율 지표의 증감(%p). 고령화율 15% → 20%는 +5%p (증감률로는 +33.3%)."""
    return current_rate - base_rate


def annual_growth_rate(base: float, current: float, years: float) -> float:
    """연평균 증감률(%, CAGR): (current / base) ** (1 / years) - 1."""
    if years <= 0:
        raise AnalysisError("기간은 0보다 커야 합니다.")
    if base <= 0 or current < 0:
        raise AnalysisError("연평균 증감률은 양수 값에서만 계산할 수 있습니다.")
    return ((current / base) ** (1 / years) - 1) * 100


def mean(values: Sequence[float]) -> float:
    """산술 평균."""
    if not values:
        raise AnalysisError("평균을 낼 값이 없습니다.")
    return sum(values) / len(values)


def share(part: float, total: float) -> float:
    """비중(%): part / total * 100."""
    if total == 0:
        raise AnalysisError("전체 값이 0이라 비중을 계산할 수 없습니다.")
    return part / total * 100


def composition(values: Mapping[str, float], total: float | None = None) -> dict[str, float]:
    """항목별 구성비(%). total이 없으면 항목 합계를 전체로 사용.

    통계표에 '계' 항목이 따로 있으면 그 값을 total로 넘겨야 기타·미상 항목이 빠져도 맞는다.
    """
    if not values:
        raise AnalysisError("구성비를 계산할 항목이 없습니다.")
    denominator = sum(values.values()) if total is None else total
    return {label: share(value, denominator) for label, value in values.items()}


def rank(values: Mapping[str, float], descending: bool = True) -> list[tuple[int, str, float]]:
    """순위 목록 [(순위, 항목, 값)]. 같은 값은 같은 순위 (1, 2, 2, 4)."""
    ordered = sorted(values.items(), key=lambda item: item[1], reverse=descending)
    ranked: list[tuple[int, str, float]] = []
    for position, (label, value) in enumerate(ordered, start=1):
        if ranked and ranked[-1][2] == value:
            position = ranked[-1][0]
        ranked.append((position, label, value))
    return ranked


def top(values: Mapping[str, float]) -> list[str]:
    """최댓값 항목들 (동률이면 모두)."""
    return _extreme(values, max)


def bottom(values: Mapping[str, float]) -> list[str]:
    """최솟값 항목들 (동률이면 모두)."""
    return _extreme(values, min)


def _extreme(values: Mapping[str, float], pick) -> list[str]:
    if not values:
        raise AnalysisError("비교할 항목이 없습니다.")
    target = pick(values.values())
    return [label for label, value in values.items() if value == target]
