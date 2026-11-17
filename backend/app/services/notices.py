"""주석 고지 (실전 2 F-07, 검색 규칙 9장)

table_meta.comments의 주석 줄을 고지 종류로 나누고, 질문에 맞는 것만 답변에 붙인다.
주석 줄은 1만 2천여 개이고 대부분 공표 변경 안내라, 규칙에 맞는 줄만 고른다.

  기준일        항상. '2015년 1월1일 기준', '매년 11월1일 기준'
  대상 범위     범위를 눈에 띄게 좁히는 말이 있을 때 (최대 2줄).
                '일반가구의 가구원을 대상으로 집계',
                '집계 대상: 일반가구 · 집단가구, 집단시설가구, 외국인가구 및 특별조사구 제외'
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
MAX_SCOPE = 2  # 대상 범위 줄 수
MAX_SCOPE_BLOCK = 3  # '집계대상' 머리말 아래에서 한 줄로 묶는 줄 수
MAX_SCOPE_LEN = 150

BASIS = re.compile(r"\d{1,2}\s*월\s*\d{1,2}\s*일[^,.\n]{0,8}기준|\d{1,2}\s*월\s*말\s*(?:기준|현재)")
MISSING = re.compile(r"\d\s*인?\s*미만|X\s*로\s*표기|비공개")
REGION_CHANGE = re.compile(r"통합|승격|편입|분리|분구|폐지|신설|명칭\s*변경|관할구역\s*변경|→")
EXCLUDED = re.compile(r"제외")
DEFINITION = re.compile(r"^\s*([^:：()]{1,20}?)\s*[:：]\s*(\S.*)$")
YEAR_RANGE = re.compile(r"((?:19|20)\d{2})\s*(?:년)?\s*(?:~\s*((?:19|20)\d{2}))?")
PREFIX = re.compile(r"^\s*(?:주\s*\d+\)|※|ㅇ|-|·|\*(?!\s*은))\s*")
HTML = re.compile(r"<[^>]+>")

# ---- 대상 범위: 표가 누구를 세고 누구를 뺐는지 ----
# 인구총조사 표는 주석에 '집계대상' 머리말을 두고 그 아래 여러 줄로 적거나(476개 표),
# '…을 대상으로 집계. 단, … 제외'처럼 문장으로 적는다. 연도 없는 줄이라 '조사 제외'에는 안 걸린다
SCOPE_PREFIX = re.compile(r"^\s*(?:주\s*\d+\)|\d+\)|※|ㅇ|○|◦|-|·|\*(?!\s*은))\s*")
SCOPE_HEADER = re.compile(r"^(?:집계\s*대상|조사\s*대상|대상)\s*[:：]?$")
SCOPE_INLINE_HEADER = re.compile(r"^(?:집계\s*대상|조사\s*대상)\s*[:：]\s*(\S.*)$")
SCOPE_STATES = re.compile(r"대상으로\s*집계|(?:을|를)\s*대상|의\s*범위\s*[:：]")
SCOPE_BUT = re.compile(r"^단\s*,")
# '용어 : 뜻풀이' 꼴. 뜻풀이 안의 '제외'는 표의 대상 범위가 아니다 ('대상 정의'가 맡는다)
SCOPE_TERM = re.compile(r"^[^:：]{1,25}[:：]\s*\S")
# 연도 · 날짜가 든 줄은 '조사 제외' · '행정구역 변경'이 맡는다
SCOPE_DATED = re.compile(r"(?:19|20)\d{2}|\d{2}\.\s?\d{1,2}\.\s?\d{1,2}")
# 공표 방식이 바뀌었다는 안내와 그 아래 목록
SCOPE_CHANGE = re.compile(r"변경\s*사항|주요\s*변경|확대\s*공표|세분화\s*공표|유의\s*사항")
# 표기 · 계산 방법, 행정구역 변경
NOT_SCOPE = re.compile(
    r"X\s*로\s*표기|미만\s*자료|사례수|합계와|반올림|통계표|통계현황에서|"
    r"명칭\s*변경|통합|승격|편입|분리|신설|폐지"
)
SCOPE_NEW_ITEM = re.compile(r"^\s*(?:주\s*\d+\)|\d+\)|※|ㅇ|○)")
# 거의 모든 인구총조사 표에 붙는 문구. 이것만 있으면 범위를 눈에 띄게 좁힌 것이 아니다
SCOPE_COMMON = re.compile(r"외국인\s*가구|특별\s*조사구(?:\s*\(군인 등\))?(?:의)?\s*(?:인구)?")
# 범위를 눈에 띄게 좁히는 말: 일반가구(원)만, 집단가구 제외, 외국인 제외 · 내국인만,
# 나이 · 거주 기간
SCOPE_NARROWS = re.compile(
    r"일반\s*가구|가구원|집단\s*(?:시설)?\s*가구|내국인|외국인|거주자|상주|"
    r"\d+\s*세\s*(?:이상|이하|미만)|\d+\s*개월\s*이상|표본|기혼|미혼|취업"
)


def clean(line: str) -> str:
    """'주3) …', '※ …', HTML 태그를 지운다"""
    return PREFIX.sub("", HTML.sub("", line)).strip()


def lines_of(t: dict[str, Any]) -> list[str]:
    out = []
    for c in t.get("comments") or []:
        out += [cleaned for line in c.get("lines") or [] if (cleaned := clean(line))]
    return list(dict.fromkeys(out))


def comment_lines(t: dict[str, Any]) -> list[str]:
    """주석 원문 줄. 문장 중간에서 끊긴 줄(쉼표로 끝나거나 괄호가 안 닫힘)은 다음 줄과 잇는다"""
    out: list[str] = []
    for c in t.get("comments") or []:
        for line in c.get("lines") or []:
            prev = out[-1] if out else ""
            broken = prev.rstrip().endswith(",") or prev.count("(") > prev.count(")")
            if out and broken and not SCOPE_NEW_ITEM.match(line):
                out[-1] = f"{prev.rstrip()} {line.strip()}"
            else:
                out.append(line)
    return out


def narrows(line: str) -> bool:
    """흔한 문구('외국인가구 및 특별조사구 제외')를 지우고도 범위를 좁히는 말이 남는가"""
    return bool(SCOPE_NARROWS.search(SCOPE_COMMON.sub(" ", line)))


def scope(t: dict[str, Any]) -> list[str]:
    """표가 세는 대상과 제외 대상 (최대 MAX_SCOPE 줄).

    대상을 밝힌 줄('…을 대상으로 집계', '○○의 범위 : …')과 바로 뒤의 '단, … 제외' →
    '집계대상' 머리말 아래 줄들을 한 줄로 묶은 것 → 따로 적힌 '… 제외' 순서로 고른다.
    범위를 눈에 띄게 좁히는 말이 하나도 없으면 붙이지 않는다
    """
    states: list[str] = []
    block: list[str] = []
    excludes: list[str] = []
    under_header = in_change = stated = False
    for raw in comment_lines(t):
        line = SCOPE_PREFIX.sub("", HTML.sub("", raw)).strip()
        if not line:
            continue
        if SCOPE_CHANGE.search(line):
            in_change, under_header, stated = True, False, False
            continue
        if SCOPE_NEW_ITEM.match(raw):
            in_change = in_change and raw.lstrip().startswith("ㅇ")  # 'ㅇ'은 변경 안내의 하위 항목
        if SCOPE_HEADER.match(line):
            under_header, in_change, stated = True, False, False
            continue
        if m := SCOPE_INLINE_HEADER.match(line):  # '집계대상 : 상주인구는 …' 한 줄짜리
            line, under_header, in_change = m[1], True, False
        if (
            in_change
            or SCOPE_DATED.search(line)
            or len(line) > MAX_SCOPE_LEN
            or NOT_SCOPE.search(line)
        ):
            stated = False
            continue
        if under_header:
            block.append(line.rstrip("."))
        elif SCOPE_STATES.search(line):
            states.append(line)
            stated = True
        elif EXCLUDED.search(line) and (stated or SCOPE_BUT.match(line)):
            states.append(line)
        elif EXCLUDED.search(line) and not SCOPE_TERM.match(line):
            excludes.append(line)
            stated = False
        else:
            stated = False
    # 머리말 아래에는 정의 · 계산 설명이 섞여 있어 앞의 몇 줄만 묶는다
    joined = (
        ["집계 대상: " + " · ".join(list(dict.fromkeys(block))[:MAX_SCOPE_BLOCK])] if block else []
    )
    found = list(dict.fromkeys([*states, *joined, *excludes]))
    if not any(narrows(x) for x in found):
        return []
    return found[:MAX_SCOPE]


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
        *scope(t),
        *missing(lines),
        *definitions(family, t.get("target")),
        *region_changes(lines, c.get("region")),
        *exclusions(lines, c.get("year")),
    ]
    return list(dict.fromkeys(found))[:MAX_NOTICES]
