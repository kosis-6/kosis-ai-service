"""주석 고지 (실전 2 F-07, 검색 규칙 9장)

stat_tables.comments의 주석 줄을 고지 종류로 나누고, 질문에 맞는 것만 답변에 붙인다.
주석 줄은 1만 2천여 개이고 대부분 공표 변경 안내라, 규칙에 맞는 줄만 고른다.

  기준일        항상. '2015년 1월1일 기준', '매년 11월1일 기준'
  결측 표기     항상. '*은 5인 미만인 경우임', '5미만 자료는 X로 표기'
  대상 정의     대상 멤버를 고른 경우 (패밀리 안 모든 판에서 찾음). '혼인귀화자 : 내국인과 혼인을 …'
  행정구역 변경 지역 이름을 물은 경우, 그 지역이 나오는 줄만.
                '2010년 7월 1일자로 마산시, 진해시는 창원시로 통합'
  조사 제외     연도를 물은 경우, 그 연도를 포함한 기간이 적힌 줄만. '새터민은 조사 제외(2007~2008)'
"""

import re
from typing import Any

from app.services.search import norm

MAX_NOTICES = 5

BASIS = re.compile(r"\d{1,2}\s*월\s*\d{1,2}\s*일[^,.\n]{0,8}기준|\d{1,2}\s*월\s*말\s*(?:기준|현재)")
MISSING = re.compile(r"\d\s*인?\s*미만|X\s*로\s*표기|비공개")
REGION_CHANGE = re.compile(r"통합|승격|편입|분리|분구|폐지|신설|명칭\s*변경|관할구역\s*변경|→")
EXCLUDED = re.compile(r"제외")
DEFINITION = re.compile(r"^\s*([^:：()]{1,20}?)\s*[:：]\s*(\S.*)$")
YEAR_RANGE = re.compile(r"((?:19|20)\d{2})\s*(?:년)?\s*(?:~\s*((?:19|20)\d{2}))?")
PREFIX = re.compile(r"^\s*(?:주\s*\d+\)|※|ㅇ|-|·|\*(?!\s*은))\s*")
HTML = re.compile(r"<[^>]+>")


def clean(line: str) -> str:
    """'주3) …', '※ …', HTML 태그를 지운다"""
    return PREFIX.sub("", HTML.sub("", line)).strip()


def lines_of(t: dict[str, Any]) -> list[str]:
    out = []
    for c in t.get("comments") or []:
        out += [cleaned for line in c.get("lines") or [] if (cleaned := clean(line))]
    return list(dict.fromkeys(out))


def basis(lines: list[str]) -> list[str]:
    return [line for line in lines if BASIS.search(line)][:1]


def missing(lines: list[str]) -> list[str]:
    return [line for line in lines if MISSING.search(line)][:1]


def definitions(family: list[dict[str, Any]], target: str | None) -> list[str]:
    """고른 대상의 정의 줄. 옛 판에만 정의가 있는 경우가 있어 패밀리 전체에서 찾는다"""
    if not target or target == "전체":
        return []
    key = norm(target)
    for t in family:
        for line in lines_of(t):
            m = DEFINITION.match(line)
            if m and norm(m.group(1)) == key:
                return [line]
    return []


def region_changes(lines: list[str], region: str | None) -> list[str]:
    if not region:
        return []
    # '부산광역시 해운대구' → '부산', '해운대구'. 시도 정식 이름의 앞 두 글자도 찾는다
    words = {w for w in region.split() if len(w) >= 2}
    words |= {w[:2] for w in region.split() if w.endswith(("특별시", "광역시", "특별자치시", "도"))}
    return [line for line in lines if REGION_CHANGE.search(line) and any(w in line for w in words)][
        :2
    ]


def exclusions(lines: list[str], year: int | None) -> list[str]:
    if year is None:
        return []
    out = []
    for line in lines:
        if not EXCLUDED.search(line):
            continue
        for start, end in YEAR_RANGE.findall(line):
            if int(start) <= year <= int(end or start):
                out.append(line)
                break
    return out[:1]


def notices(t: dict[str, Any], family: list[dict[str, Any]], slots: dict[str, Any]) -> list[str]:
    """고른 표에 붙일 주석 고지 (최대 5줄)"""
    c = slots["conditions"]
    lines = lines_of(t)
    found = [
        *basis(lines),
        *missing(lines),
        *definitions(family, t.get("target")),
        *region_changes(lines, c.get("region")),
        *exclusions(lines, c.get("year")),
    ]
    return list(dict.fromkeys(found))[:MAX_NOTICES]
