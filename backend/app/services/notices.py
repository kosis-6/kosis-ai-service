import re
from typing import Any

from app.services.search import norm

MAX_NOTICES = 5
MAX_SCOPE = 2
MAX_SCOPE_BLOCK = 3
MAX_SCOPE_LEN = 150

BASIS = re.compile(r"\d{1,2}\s*월\s*\d{1,2}\s*일[^,.\n]{0,8}기준|\d{1,2}\s*월\s*말\s*(?:기준|현재)")
MISSING = re.compile(r"\d\s*인?\s*미만|X\s*로\s*표기|비공개")
REGION_CHANGE = re.compile(r"통합|승격|편입|분리|분구|폐지|신설|명칭\s*변경|관할구역\s*변경|→")
EXCLUDED = re.compile(r"제외")
DEFINITION = re.compile(r"^\s*([^:：()]{1,20}?)\s*[:：]\s*(\S.*)$")
YEAR_RANGE = re.compile(r"((?:19|20)\d{2})\s*(?:년)?\s*(?:~\s*((?:19|20)\d{2}))?")
PREFIX = re.compile(r"^\s*(?:주\s*\d+\)|※|ㅇ|-|·|\*(?!\s*은))\s*")
HTML = re.compile(r"<[^>]+>")

SCOPE_PREFIX = re.compile(r"^\s*(?:주\s*\d+\)|\d+\)|※|ㅇ|○|◦|-|·|\*(?!\s*은))\s*")
SCOPE_HEADER = re.compile(r"^(?:집계\s*대상|조사\s*대상|대상)\s*[:：]?$")
SCOPE_INLINE_HEADER = re.compile(r"^(?:집계\s*대상|조사\s*대상)\s*[:：]\s*(\S.*)$")
SCOPE_STATES = re.compile(r"대상으로\s*집계|(?:을|를)\s*대상|의\s*범위\s*[:：]")
SCOPE_BUT = re.compile(r"^단\s*,")
SCOPE_TERM = re.compile(r"^[^:：]{1,25}[:：]\s*\S")
SCOPE_DATED = re.compile(r"(?:19|20)\d{2}|\d{2}\.\s?\d{1,2}\.\s?\d{1,2}")
SCOPE_CHANGE = re.compile(r"변경\s*사항|주요\s*변경|확대\s*공표|세분화\s*공표|유의\s*사항")
NOT_SCOPE = re.compile(
    r"X\s*로\s*표기|미만\s*자료|사례수|합계와|반올림|통계표|통계현황에서|"
    r"명칭\s*변경|통합|승격|편입|분리|신설|폐지"
)
SCOPE_NEW_ITEM = re.compile(r"^\s*(?:주\s*\d+\)|\d+\)|※|ㅇ|○)")
SCOPE_COMMON = re.compile(r"외국인\s*가구|특별\s*조사구(?:\s*\(군인 등\))?(?:의)?\s*(?:인구)?")
SCOPE_NARROWS = re.compile(
    r"일반\s*가구|가구원|집단\s*(?:시설)?\s*가구|내국인|외국인|거주자|상주|"
    r"\d+\s*세\s*(?:이상|이하|미만)|\d+\s*개월\s*이상|표본|기혼|미혼|취업"
)


def clean(line: str) -> str:
    return PREFIX.sub("", HTML.sub("", line)).strip()


def lines_of(t: dict[str, Any]) -> list[str]:
    out = []
    for c in t.get("comments") or []:
        out += [cleaned for line in c.get("lines") or [] if (cleaned := clean(line))]
    return list(dict.fromkeys(out))


def comment_lines(t: dict[str, Any]) -> list[str]:
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
    return bool(SCOPE_NARROWS.search(SCOPE_COMMON.sub(" ", line)))


def scope(t: dict[str, Any]) -> list[str]:
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
            in_change = in_change and raw.lstrip().startswith("ㅇ")
        if SCOPE_HEADER.match(line):
            under_header, in_change, stated = True, False, False
            continue
        if m := SCOPE_INLINE_HEADER.match(line):
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
