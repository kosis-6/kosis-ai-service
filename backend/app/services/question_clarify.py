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


def detect(question: str, regions: list[str]) -> dict | None:
    if VAGUE_REGION.search(question) and not regions:
        return {
            "kind": "local_region",
            "prompt": "어느 지역을 말씀하시나요? 시·군·구 이름을 알려 주세요 (예: 수원시).",
            "options": [],
        }
    if MOVE_COUNT.search(question) and not MOVE_SPECIFIC.search(question):
        return {
            "kind": "move_scope",
            "prompt": "이사 범위를 확인할게요. 해당 지역 안에서 이사한 경우도 포함할까요, "
            "그 지역 밖으로 나간 경우만 볼까요?",
            "options": MOVE_OPTIONS,
        }
    return None


def scope_reply(reply: str) -> str | None:
    q = re.sub(r"\s+", "", reply).strip(" .?!")
    forms = {q, re.sub(r"(?:이요|요|입니다)$", "", q)}
    if forms & {
        "1",
        "1번",
        "포함",
        "포함해줘",
        "포함해주세요",
        "포함해",
        "지역안에서의이사도포함",
        "내부도포함",
        "둘다",
    }:
        return "include"
    if forms & {
        "2",
        "2번",
        "제외",
        "제외해줘",
        "제외해주세요",
        "제외해",
        "지역밖으로나간이사만",
        "밖으로나간것만",
        "외부만",
    }:
        return "exclude"
    return None


def merge_scope(question: str, scope: str) -> str:
    # 선택의 뜻을 질문에 명시해 통계표 찾기에도 그대로 전달한다.
    condition = (
        "집계 범위: 해당 지역에서 출발한 이동 중 지역 내 이동과 지역 밖으로 나간 이동 모두 포함"
        if scope == "include"
        else "집계 범위: 해당 지역에서 다른 지역으로 나간 전출만, 지역 내 이동 제외"
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


def merge_region(question: str, region: str) -> str:
    return VAGUE_REGION.sub(region, question)
