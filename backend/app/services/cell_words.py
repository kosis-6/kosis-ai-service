"""동적 지표 2단계: 질문의 일상어를 표의 칸 이름으로 잇는다 (LLM 보조)

규칙(dynamic_indicator._text_hits)은 칸 이름이 질문에 글자 그대로 있을 때만 칸을 고른다.
"자가용"(표: 승용차 소형 승합차), "낮·밤"(표: 주간 인구·상주인구)처럼 말이 다르면 계산하지 못한다.
그때만 HCX에게 "질문의 어느 말이 표의 어느 칸인가"를 한 번 묻는다.

LLM은 말과 칸 이름의 짝만 낸다. 숫자는 만들지 않는다.
  - 칸 이름은 표에 실제로 있는 이름과 똑같아야 하고, 말은 질문에 그대로 있어야 한다 (아니면 버림)
  - 받은 짝으로 질문의 말을 칸 이름으로 바꿔 쓴 뒤, 칸 고르기·검증·계산은 규칙이 그대로 한다
  - 답에는 "'자가용'을 '승용차 소형 승합차' 칸으로 보고 계산했습니다"를 붙인다
"""

import logging
import re

from app.services.hcx_client import HcxError
from app.services.josa import josa
from app.services.memory_cache import MISSING, MemoryCache

logger = logging.getLogger(__name__)

MAX_CODES = 40  # 칸이 이보다 많은 분류(세부 지역 등)는 묻지 않는다 (프롬프트가 길어진다)
MAX_PAIRS = 4
CACHE = MemoryCache(
    "cell_words", maxsize=512, ttl=3600
)  # (표, 질문) → 짝. 같은 질문은 다시 묻지 않음

SYSTEM = """너는 통계표의 칸 이름을 찾아 주는 도우미다.
질문에 나온 말 가운데, 아래 통계표의 칸과 뜻이 같은데 글자가 다른 말을 찾아 그 칸 이름과 짝지어라.

규칙:
- word는 질문에 나온 글자 그대로 (조사는 빼고)
- cell은 아래 목록의 칸 이름을 글자 하나 바꾸지 말고 그대로
- 뜻이 분명히 같은 것만. 비슷하기만 하거나 더 넓은·좁은 뜻이면 짝짓지 마라
- 질문에 이미 칸 이름이 그대로 나온 말, 지역·연도·성별·연령을 뜻하는 말은 짝짓지 마라
- 짝이 없으면 빈 목록
- 설명 없이 JSON 하나만: {"pairs": [{"word": "질문의 말", "cell": "칸 이름"}]}"""


def _menu(dims, skip: set[int]) -> tuple[str, set[str]]:
    """프롬프트에 넣을 분류별 칸 이름과, 쓸 수 있는 칸 이름 모음"""
    lines, names = [], set()
    for d in dims:
        if d.no in skip or len(d.codes) > MAX_CODES:
            continue
        cells = [n for c, n in d.codes.items() if c != d.total()]
        if len(cells) < 2:
            continue
        names.update(cells)
        lines.append(f"- {d.name}: " + " | ".join(cells))
    return "\n".join(lines), names


def _key(name: str) -> str:
    return re.sub(r"[\s·,.()\[\]_/-]", "", name)


def _pairs(text: str, question: str, names: set[str]) -> list[tuple[str, str]]:
    """HCX 출력 → 검증한 (말, 칸 이름) 짝. 하나라도 틀린 짝이 있으면 모두 버린다
    (짝 하나만 쓰면 "낮이 밤보다 몇 배"를 낮 인구 하나로 답하게 된다)"""
    exact = {_key(n): n for n in names}  # 띄어쓰기·기호는 무시하고 표의 이름으로 ("상주 인구")
    from app.services.qa import _json_objects  # qa가 dynamic_indicator를 불러오므로 여기서

    objects = _json_objects(text or "")
    raw = (objects[0].get("pairs") if objects else None) or []
    out: list[tuple[str, str]] = []
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict):
            continue
        word = str(p.get("word") or "").strip()
        cell = exact.get(_key(str(p.get("cell") or "")))
        if word and cell and _key(word) == _key(cell):
            continue  # 이미 칸 이름 그대로인 말: 바꿀 것이 없다
        # 말은 질문에 그대로, 칸 이름은 표에 있어야 한다. 겹치는 말도 틀린 것으로 본다
        if not word or word not in question or cell is None:
            return []
        bare = re.sub(r"(계|합계|소계|전체)$", "", _key(cell))
        # 합계 칸("가구-계")은 어떤 말과도 같은 뜻이 아니다. 말이 칸 이름에 조건을 더 붙인 것이면
        # ("반려견 키우는 가구" ⊃ "가구") 그 조건을 버리게 된다 → 모두 버림
        if bare != _key(cell) or not bare or bare in _key(word):
            return []
        if any(word in w or w in word for w, _ in out):
            return []
        out.append((word, cell))
    return out[:MAX_PAIRS]


async def match(tbl_id: str, tbl_nm: str, question: str, dims, skip: set[int], trace: dict):
    """(바꿔 쓴 질문, [(말, 칸 이름)]) 또는 None. HCX 오류는 None (규칙만으로 답한 것과 같게)"""
    from app.services import qa  # qa가 dynamic_indicator를 불러오므로 여기서

    menu, names = _menu(dims, skip)
    if not menu:
        return None
    key = (tbl_id, question)
    pairs = CACHE.get(key)
    if pairs is MISSING:
        prompt = f"통계표: {tbl_nm}\n칸:\n{menu}\n\n질문: {question}"
        try:
            text = await qa._chat(prompt, SYSTEM, temperature=0.1)
        except HcxError as e:
            logger.warning("칸 이름 잇기 실패: %s", e)
            return None
        pairs = _pairs(text, question, names)
        CACHE.set(key, pairs)
        trace.setdefault("cell_words", []).append(
            {"tbl_id": tbl_id, "raw": (text or "")[:300], "pairs": pairs}
        )
    if not pairs:
        return None
    rewritten = question
    for word, cell in pairs:
        rewritten = rewritten.replace(word, cell)
    return rewritten, pairs


def note(pairs: list[tuple[str, str]]) -> str:
    """'자가용'을 '승용차 소형 승합차' 칸으로 보고 계산했습니다"""
    said = ", ".join(f"{josa(f"'{w}'", '을를')} '{c}'" for w, c in pairs)
    return f"{said} 칸으로 보고 계산했습니다"
