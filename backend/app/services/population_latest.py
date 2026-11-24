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
CACHE_FOR = 6 * 3600.0
FAILED_FOR = 300.0
TIMEOUT = 15.0
CHUNK = 100

_cache: dict[tuple[str, str, str], tuple[float, dict | None]] = {}
_lock = threading.Lock()


def components(region_code: str, sex: str = "계") -> dict | None:
    return components_many([region_code], sex).get(region_code)


def components_many(
    codes: list[str], sex: str = "계", period: str | None = None
) -> dict[str, dict]:
    now = time.monotonic()
    found: dict[str, dict] = {}
    missing = []
    with _lock:
        for code in dict.fromkeys(codes):
            hit = _cache.get((code, sex, period or ""))
            if hit and now < hit[0]:
                if hit[1]:
                    found[code] = hit[1]
            else:
                missing.append(code)
    for i in range(0, len(missing), CHUNK):
        chunk = missing[i : i + CHUNK]
        try:
            got = _parse_many(_fetch_month("+".join(chunk), SEX_ITEM.get(sex, "T2"), period))
            until = time.monotonic() + CACHE_FOR
        except Exception as e:  # noqa: BLE001 - 최신 달을 못 받아도 답은 수집본으로 한다
            logger.warning(
                "최신 달 인구 받기 실패 (%s개 지역): %s", len(chunk), mask_key(str(e))[:200]
            )
            got, until = {}, time.monotonic() + FAILED_FOR
        with _lock:
            for code in chunk:
                _cache[(code, sex, period or "")] = (until, got.get(code))
        found.update(got)
    return found


def band(comp: dict, lo: int, hi: int | None) -> int | None:
    ages = comp.get("ages") or {}
    picked = [v for age, v in ages.items() if age >= lo and (hi is None or age <= hi)]
    return sum(picked) if picked else None


def _fetch_month(region_codes: str, item: str, period: str | None = None) -> list[dict[str, Any]]:
    settings = get_settings()
    params = {
        "apiKey": settings.kosis_api_key,
        "format": "json",
        "jsonVD": "Y",
        "method": "getList",
        "orgId": ORG_ID,
        "tblId": TABLE,
        "prdSe": "M",
        "itmId": item,
        "objL1": region_codes,
        "objL2": "ALL",
    }
    if period:
        params |= {"startPrdDe": period, "endPrdDe": period}
    else:
        params["newEstPrdCnt"] = "1"
    with httpx.Client(base_url=settings.kosis_base_url, timeout=TIMEOUT) as client:
        response = client.get("/Param/statisticsParameterData.do", params=params)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    data = response.json()
    if isinstance(data, dict):
        raise RuntimeError(f"{data.get('err')}: {data.get('errMsg')}")
    return data


def _parse_many(rows: list[dict[str, Any]]) -> dict[str, dict]:
    by_region: dict[str, list] = {}
    for r in rows:
        by_region.setdefault(str(r.get("C1")), []).append(r)
    return {code: comp for code, part in by_region.items() if (comp := _parse(part))}


def _parse(rows: list[dict[str, Any]]) -> dict | None:
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
            ages[int(digits)] = value
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
