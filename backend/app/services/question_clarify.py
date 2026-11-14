"""수치 계산 전에 확인해야 할 지역·이사 범위. 추가 LLM 호출 없이 판별한다."""

import re

VAGUE_REGION = re.compile(
    r"우리\s*(?:동네|지역|구(?=에서|의|는|에|\s|$|[?.])|시(?=에서|의|는|에|\s|$|[?.]))"
    r"|내가\s*사는\s*(?:곳|지역)"
)
# 출발 지역을 말한 이사 질문만 다룬다. '수원으로 이사 온' 질문을 전출로 바꾸지 않는다.
MOVE_COUNT = re.compile(r"에서\s*.{0,25}이사(?!\s*(?:온|오는|오신))")
MOVE_SPECIFIC = re.compile(
    r"순이동|순유입|순유출|전입|전출|사유|이유|왜|직업|주택|교육|"
    r"(?:지역|시|구|군)\s*(?:내|안|밖)|관내|관외|포함|제외|다른\s*(?:지역|시|구)|외부|"
    r"에서.{0,25}(?:으로|로)\s*이사"
)
MOVE_OPTIONS = ["지역 안에서의 이사도 포함", "지역 밖으로 나간 이사만"]
OUT_WORDS = re.compile(r"나간|나가는|나갔|떠난|떠나는|빠져나")  # 밖으로 나간 이사만
NATIONWIDE = re.compile(r"전국|우리\s*나라|국내\s*전체")
MAX_ASKS = 2  # 같은 되묻기는 이만큼까지 (통계표 찾기 dialog.MAX_CLARIFY_TURNS와 같다)

# 이사 범위 답: 정해진 문구가 아니라 핵심 낱말로 읽는다
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
    # 범위를 이미 말했거나("이사 나간") 전국이면(지역 밖이 없음) 묻지 않는다
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
    """이사 건수 질문에서 범위를 이미 말했으면 그 범위 (되묻지 않는다).
    "수원에서 이사 나간 사람" → "exclude". "전국에서 이사한 사람"은 지역 밖이 없어 "nation".
    순이동·전입 사유처럼 다른 지표를 물은 질문이면 None"""
    if not MOVE_COUNT.search(question) or MOVE_SPECIFIC.search(question):
        return None
    if NATIONWIDE.search(question):
        return "nation"
    if OUT_WORDS.search(question):
        return "exclude"
    return None


def scope_reply(reply: str) -> str | None:
    """이사 범위 답 → "include"(지역 안 이사도 포함) / "exclude"(밖으로 나간 것만) / None(모름).
    "1번·첫 번째"는 첫 선택지, "2번·두 번째"는 둘째 선택지. "A 말고 B"는 B로 읽는다.
    포함·제외 낱말이 함께 있으면(예: "밖으로 나간 것도 포함") 짐작하지 않는다"""
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
    # 선택의 뜻을 질문에 명시해 통계표 찾기에도 그대로 전달한다.
    # "제외·말고"는 쓰지 않는다: 통계표 찾기가 그 말이 든 표(이동 표 전체)를 감점한다
    if scope == "nation":  # 전국은 지역 안팎 구분이 없다
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
        r"입니다|이에요|예요|이요|으로|에서|기준|지역|해주세요|해줘|보여줘|로|요|[\s.!?]", "", rest
    )
    return " ".join(regions) if not rest else None


def bare_region(reply: str) -> str | None:
    """지역 답에서 지역 이름만 남긴다 ("수원이요" → "수원").
    짧은 답만 (긴 문장은 새 질문으로 본다)"""
    name = re.sub(
        r"(?:이에요|예요|에요|입니다|이요|요|으로|로|기준|지역)$", "", reply.strip(" .?!")
    )
    name = name.strip()
    return name if 0 < len(name) <= 15 and not re.search(r"\d", name) else None


def merge_region(question: str, region: str) -> str:
    return VAGUE_REGION.sub(region, question)
