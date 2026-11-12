"""주민등록인구의 최신 달 값 (연간 자료 다음 해)

수집본(mart.population)은 같은 표(DT_1B04006)의 연간 값(12월 말)만 있다.
KOSIS 원표는 월별로 올라오므로, 연간 자료에 없는 올해를 물으면
장래인구추계로 대신 답하던 것을 최신 달 값으로 답한다.
(추계는 7월 1일 기준·외국인 포함이라 실제 주민등록인구와 다르다)

  components(지역 코드, 성별) → {"year", "month", "period", "total", "young", "working", "old",
                                 "ages"}
    지역 하나·성별 하나에 KOSIS 요청 1번 (1세별 102행). 같은 값은 CACHE_FOR 동안 다시 받지 않는다
  components_many(지역 코드 목록, 성별) → {지역 코드: 값}
    순위처럼 지역이 많을 때. 캐시에 없는 지역만 CHUNK곳씩 묶어 한 요청
    받지 못하면 None (질문 처리는 지금처럼 수집본·추계로 답한다)

계산(indicators.py)은 스레드에서 돌므로 여기서도 동기 요청을 쓴다. 오류 메시지의 apiKey는 가린다.
"""

import logging
import threading
import time
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.kosis_client import mask_key

logger = logging.getLogger(__name__)

TABLE = "DT_1B04006"
ORG_ID = "101"
SEX_ITEM = {"계": "T2", "남": "T3", "여": "T4"}
CACHE_FOR = 6 * 3600.0  # 초. 월별 자료는 한 달에 한 번 올라온다
FAILED_FOR = 300.0  # 초. 받기에 실패하면 이 동안은 다시 부르지 않는다 (질문마다 기다리지 않게)
TIMEOUT = 15.0
CHUNK = 100  # 한 요청에 묶는 지역 수 (100곳 × 1세별 102행 ≈ 1만 행, KOSIS 한도 4만 행)

_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
_lock = threading.Lock()


def components(region_code: str, sex: str = "계") -> dict | None:
    """그 지역·성별의 최신 달 인구 (연령별 포함). 받지 못하면 None"""
    return components_many([region_code], sex).get(region_code)


def components_many(codes: list[str], sex: str = "계") -> dict[str, dict]:
    """여러 지역의 최신 달 인구 {지역 코드: 값}. 받지 못한 지역은 빠진다.
    캐시에 없는 지역만 CHUNK개씩 묶어 받는다 (KOSIS objL1에 코드를 '+'로 이어 보냄)"""
    now = time.monotonic()
    found: dict[str, dict] = {}
    missing = []
    with _lock:
        for code in dict.fromkeys(codes):
            hit = _cache.get((code, sex))
            if hit and now < hit[0]:
                if hit[1]:
                    found[code] = hit[1]
            else:
                missing.append(code)
    for i in range(0, len(missing), CHUNK):
        chunk = missing[i : i + CHUNK]
        try:
            got = _parse_many(_fetch_month("+".join(chunk), SEX_ITEM.get(sex, "T2")))
            until = time.monotonic() + CACHE_FOR
        except Exception as e:  # noqa: BLE001 - 최신 달을 못 받아도 답은 수집본으로 한다
            logger.warning(
                "최신 달 인구 받기 실패 (%s개 지역): %s", len(chunk), mask_key(str(e))[:200]
            )
            got, until = {}, time.monotonic() + FAILED_FOR
        with _lock:
            for code in chunk:
                _cache[(code, sex)] = (until, got.get(code))
        found.update(got)
    return found


def band(comp: dict, lo: int, hi: int | None) -> int | None:
    """연령 구간 인구 (lo세 ~ hi세, hi가 None이면 lo세 이상)"""
    ages = comp.get("ages") or {}
    picked = [v for age, v in ages.items() if age >= lo and (hi is None or age <= hi)]
    return sum(picked) if picked else None


def _fetch_month(region_codes: str, item: str) -> list[dict[str, Any]]:
    settings = get_settings()
    params = {
        "apiKey": settings.kosis_api_key,
        "format": "json",
        "jsonVD": "Y",
        "method": "getList",
        "orgId": ORG_ID,
        "tblId": TABLE,
        "prdSe": "M",
        "newEstPrdCnt": "1",  # 가장 최근 시점 하나
        "itmId": item,
        "objL1": region_codes,
        "objL2": "ALL",
    }
    with httpx.Client(base_url=settings.kosis_base_url, timeout=TIMEOUT) as client:
        response = client.get("/Param/statisticsParameterData.do", params=params)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    data = response.json()
    if isinstance(data, dict):  # {"err": "...", "errMsg": "..."}
        raise RuntimeError(f"{data.get('err')}: {data.get('errMsg')}")
    return data


def _parse_many(rows: list[dict[str, Any]]) -> dict[str, dict]:
    """여러 지역의 KOSIS 행 → {지역 코드: 값}"""
    by_region: dict[str, list] = {}
    for r in rows:
        by_region.setdefault(str(r.get("C1")), []).append(r)
    return {code: comp for code, part in by_region.items() if (comp := _parse(part))}


def _parse(rows: list[dict[str, Any]]) -> dict | None:
    """KOSIS 행 → 총인구·연령 구간 인구 (preprocess.build_population과 같은 규칙)"""
    if not rows:
        return None
    period = str(rows[0]["PRD_DE"])
    total, ages = None, {}
    for r in rows:
        if str(r.get("PRD_DE")) != period or r.get("DT") in (None, "", "-"):
            continue
        name, value = str(r.get("C2_NM") or ""), int(float(r["DT"]))
        if name == "계":
            total = value
        elif digits := "".join(ch for ch in name if ch.isdigit()):
            ages[int(digits)] = value  # "100세 이상"은 100
    if total is None:
        return None
    comp = {
        "year": int(period[:4]),
        "month": int(period[4:6]),
        "period": period,
        "total": total,
        "ages": ages,
    }
    comp["young"] = band(comp, 0, 14)
    comp["working"] = band(comp, 15, 64)
    comp["old"] = band(comp, 65, None)
    return comp
