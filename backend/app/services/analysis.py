from collections.abc import Mapping


class AnalysisError(ValueError):
    pass


def change(base: float, current: float) -> float:
    return current - base


def growth_rate(base: float, current: float) -> float:
    if base == 0:
        raise AnalysisError("기준값이 0이라 증감률을 계산할 수 없습니다.")
    return (current - base) / abs(base) * 100


def point_change(base_rate: float, current_rate: float) -> float:
    return current_rate - base_rate


def share(part: float, total: float) -> float:
    if total == 0:
        raise AnalysisError("전체 값이 0이라 비중을 계산할 수 없습니다.")
    return part / total * 100


def times(a: float, b: float) -> float:
    if b == 0:
        raise AnalysisError("나누는 값이 0이라 몇 배인지 계산할 수 없습니다.")
    return a / b


def composition(values: Mapping[str, float], total: float | None = None) -> dict[str, float]:
    if not values:
        raise AnalysisError("구성비를 계산할 항목이 없습니다.")
    denominator = sum(values.values()) if total is None else total
    return {label: share(value, denominator) for label, value in values.items()}


def rank(values: Mapping[str, float], descending: bool = True) -> list[tuple[int, str, float]]:
    rows = rank_rows([{"label": k, "value": v} for k, v in values.items()], descending)
    return [(r["rank"], r["label"], r["value"]) for r in rows]


def rank_rows(rows: list[dict], descending: bool = True, key: str = "value") -> list[dict]:
    ordered = sorted(rows, key=lambda r: r[key], reverse=descending)
    for position, row in enumerate(ordered, start=1):
        prev = ordered[position - 2] if position > 1 else None
        row["rank"] = prev["rank"] if prev and prev[key] == row[key] else position
    return ordered
