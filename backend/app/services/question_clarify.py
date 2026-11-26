import re

VAGUE_REGION = re.compile(
    r"우리\s*(?:동네|지역|구(?=에서|의|는|에|\s|$|[?.])|시(?=에서|의|는|에|\s|$|[?.]))"
    r"|내가\s*사는\s*(?:곳|지역)"
    r"|(?:^|\s)(?:여기|이곳|이\s*동네|이\s*지역)(?:는|은|에는?|에서|서|의|\s)"
    r"|특정\s*(?:읍면동|시군구|시도|지역|동네)"
)
EACH_SIGUNGU = re.compile(r"(?:전국\s*)?(?:각\s*)?시\s*군\s*구\s*(?:별|마다)(?:로)?")
TREND_WORDS = re.compile(r"추이|변화|변했|변해|바뀌|달라졌|늘었|줄었")
NARROWING_WORDS = re.compile(
    r"가장|제일|상위|하위|순위|top|찾|어디|어느|곳[은이을]|이상|이하|넘는|넘은|미만|초과|"
    r"높은|낮은|많은|적은|큰|작은",
    re.IGNORECASE,
)


WHOLE_COUNTRY = ("전체", "전국", "우리나라", "대한민국", "한국")


def too_many_regions(question: str) -> bool:
    return bool(
        EACH_SIGUNGU.search(question)
        and TREND_WORDS.search(question)
        and not NARROWING_WORDS.search(question)
    )


MOVE_COUNT = re.compile(r"에서\s*.{0,25}이사(?!\s*(?:온|오는|오신))")
MOVE_SPECIFIC = re.compile(
    r"순이동|순유입|순유출|전입|전출|사유|이유|왜|직업|주택|교육|"
    r"(?:지역|시|구|군)\s*(?:내|안|밖)|관내|관외|포함|제외|다른\s*(?:지역|시|구)|외부|"
    r"에서.{0,25}(?:으로|로)\s*이사"
)
MOVE_OPTIONS = ["지역 안에서의 이사도 포함", "지역 밖으로 나간 이사만"]
OUT_WORDS = re.compile(r"나간|나가는|나갔|떠난|떠나는|빠져나")
NATIONWIDE = re.compile(r"전국|우리\s*나라|국내\s*전체")
MAX_ASKS = 2

INCLUDE_WORDS = re.compile(r"포함|전부|모두|둘\s*다|(?:^|\s)다(?:\s|$|요|포함)|안에서|내부|안쪽")
EXCLUDE_WORDS = re.compile(r"밖|나간|나가|제외|빼고|빼|외부|떠난")
NOT_INCLUDE = re.compile(r"포함\s*(?:하지|안|말|x|X)|안\s*포함|포함\s*없이")
CHOICE_NUMBER = re.compile(r"^\s*(?:([12])(?!\d)\s*번?|([①②])|(첫|두)\s*번\s*째)")


def detect(question: str, regions: list[str]) -> dict | None:
    if VAGUE_REGION.search(question) and not regions:
        return {
            "kind": "local_region",
            "prompt": "어느 지역을 말씀하시나요? 시·군·구 이름을 알려 주세요 (예: 수원시).",
            "options": [],
        }
    if too_many_regions(question) and not [r for r in regions if r not in WHOLE_COUNTRY]:
        return {
            "kind": "local_region",
            "prompt": "시군구별 추이는 지역이 많아 한 번에 보여 드리기 어렵습니다. "
            "어느 시군구를 볼까요? (예: 수원시)",
            "options": [],
        }
    if (
        MOVE_COUNT.search(question)
        and not MOVE_SPECIFIC.search(question)
        and not stated_scope(question)
    ):
        return {
            "kind": "move_scope",
            "prompt": "이사 범위를 확인할게요. 해당 지역 안에서 이사한 경우도 포함할까요, "
            "그 지역 밖으로 나간 경우만 볼까요?",
            "options": MOVE_OPTIONS,
        }
    return None


def stated_scope(question: str) -> str | None:
    if not MOVE_COUNT.search(question) or MOVE_SPECIFIC.search(question):
        return None
    if NATIONWIDE.search(question):
        return "nation"
    if OUT_WORDS.search(question):
        return "exclude"
    return None


def scope_reply(reply: str) -> str | None:
    text = reply.strip()
    if "말고" in text:
        text = text.rsplit("말고", 1)[1].strip()
    if not text or len(text) > 40:
        return None
    if m := CHOICE_NUMBER.match(text):
        n = m[1] or {"①": "1", "②": "2"}.get(m[2] or "") or {"첫": "1", "두": "2"}[m[3]]
        return "include" if n == "1" else "exclude"
    if NOT_INCLUDE.search(text):
        return "exclude"
    include, exclude = bool(INCLUDE_WORDS.search(text)), bool(EXCLUDE_WORDS.search(text))
    if include != exclude:
        return "include" if include else "exclude"
    return None


def merge_scope(question: str, scope: str) -> str:
    if scope == "nation":
        return question
    condition = (
        "집계 범위: 해당 지역에서 출발한 이동 중 지역 내 이동과 지역 밖으로 나간 이동 모두 포함"
        if scope == "include"
        else "집계 범위: 해당 지역에서 다른 지역으로 나간 전출 이동자수"
    )
    return f"{question} ({condition})"


def region_reply(reply: str, regions: list[str], has_indicator: bool) -> str | None:
    if has_indicator or not regions or len(reply) > 40:
        return None
    rest = re.sub(r"\s+", "", reply)
    for region in sorted(regions, key=len, reverse=True):
        rest = rest.replace(re.sub(r"\s+", "", region), " ")
    rest = re.sub(
        r"입니다|이에요|예요|이요|으로|에서|기준|지역|해주세요|해줘|보여줘|로|요|[\s.!?,/·]",
        "",
        rest,
    )
    return " ".join(regions) if not rest else None


def place_order(regions: list[str], resolves) -> str | None:
    """'수성구, 대구'처럼 시군구를 먼저 말해도 '대구 수성구'로. resolves(이름)이 한 곳이면 참"""
    if len(regions) != 2:
        return None
    for pair in (regions, regions[::-1]):
        if resolves(" ".join(pair)):
            return " ".join(pair)
    return None


def bare_region(reply: str) -> str | None:
    name = re.sub(
        r"(?:이에요|예요|에요|입니다|이요|요|으로|로|기준|지역)$", "", reply.strip(" .?!")
    )
    name = name.strip()
    return name if 0 < len(name) <= 15 and not re.search(r"\d", name) else None


def merge_region(question: str, region: str) -> str:
    if VAGUE_REGION.search(question):
        return VAGUE_REGION.sub(region, question)
    return EACH_SIGUNGU.sub(region, question, count=1)
