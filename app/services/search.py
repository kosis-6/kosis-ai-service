"""통계표 검색: 질문 → 표 하나의 링크 (실전 2 F-04 · F-05 · F-18 · F-06 1차)

  1. 질의 분석 (slots.extract)       질문 → 슬롯
  2. 후보 검색 (keyword_scores)      질문 단어가 표명·경로·분류·항목에 얼마나 있는지.
                                     검색어는 질문 원문 정리 + 일상어 사전 확장 (synonyms.py).
                                     단어마다 희귀도(IDF) 가중치 ('인구'처럼 흔한 단어는 덜 침)
     + 임베딩 (vector_search)        질문 원문과 표 문서의 코사인 유사도 (bge-m3).
                                     임베딩 점수를 기본으로 두고 키워드 점수가 높을 때만 더한다
                                     (하이브리드. 키워드는 질문마다 1위=1로 정규화한 뒤 비교.
                                     질문 임베딩이 없으면 키워드만)
  3. 조건 검증·재순위 (check)        표(판) 단위로 연도·지역 수준·성별·분류·값·대상을 판정해 가감점.
                                     질문에 없는 조건(null)은 판정하지 않는다
  4. 패밀리 묶기 (fold)              같은 패밀리는 한 자리만 차지, 패밀리 점수 = 소속 표 최고 점수
  5. 멤버·판 선택 (choose)           대상·성별로 멤버, 연도로 판을 고른다 (검색 규칙 2.4)
  6. 상태 판단                       ok / partial_match / clarify / ambiguous / out_of_scope
                                     / not_statistics. 대상이 빠지면 clarify (되묻기, 규칙 2.3 가안)

표 메타데이터(stat_tables + table_families, 2천여 행)는 처음 한 번 읽어 메모리에 둔다.
표 벡터(table_embeddings)도 함께 읽어 둔다. 되묻기 다음 턴(답 맞추기·세션)은 dialog.py가 맡는다.

실행: python -m app.services.search "2024년 부산 시군구별 혼인 귀화자 수"
"""

import asyncio
import json
import logging
import math
import re
import sys
import time
from typing import Any

from sqlalchemy import text

from app.services import slots as slot_parser
from app.services import synonyms, vector_search

logger = logging.getLogger(__name__)
# 앱에 로그 설정이 없어 INFO가 안 보이므로, 서버 시작 메시지는 uvicorn 로그로 낸다
startup_log = logging.getLogger("uvicorn.error")

# 가중치·기준값은 개발셋으로 조정할 값 (기능 정의서 F-05, F-06 미결)
FIELD_WEIGHTS = {"name": 1.0, "path": 0.6, "meta": 0.4}
NAME_WEIGHT = 0.3
PASS_BONUS = 0.08
FAIL_PENALTY = 0.15
# 대상·분류·항목·성별은 HCX 추출과 표 메타의 표현 차이로 자주 어긋나는 '부드러운' 조건이라 감점을
# 작게 둔다
# (실험실에는 이 판정이 없음. 어댑터 실측 후보 재계산: 팀원 R@1 62→70, 생성 75→82, 개발 65→67, 손해
# 없음)
SOFT_CHECKS = ("target", "class", "item", "sex", "spouse")
# 특정 집단만 조사한 통계는 질문에 그 집단 말이 없으면 감점 (개발셋 d57 '평균 몇 살까지 사는지'에
# 사할린 '결혼 여부' 표,
# d79 '동호회 모임 참여'에 사할린 '모임 및 단체 참여' 표가 1위로 오던 문제)
SURVEY_POPULATION = {
    "영주귀국사할린동포실태조사": ("사할린", "동포"),
    "고령친화용품제조업실태조사": ("고령친화", "노인용품", "용품", "실버"),
}
SURVEY_PENALTY = 0.1
SOFT_FAIL_PENALTY = 0.05
PUBLISH_LAG = (
    2  # 상대시점이 가리키는 해가 아직 공표 전이어도 이 햇수 안이면 최신 판을 맞는 표로 본다
)

MIN_SCORE = 0.45  # 1순위 패밀리 점수가 이보다 낮으면 out_of_scope
AMBIGUOUS_GAP = 0.03  # 1·2순위 패밀리 점수 차가 이보다 작으면 ambiguous
TOP_FAMILIES = 4

# 모집단을 좁히는 수식어. 질문에 없으면 수식어 없는 형제 표를 기본값으로 앞세운다
# ('결혼 건수' → '연령/교육정도별 혼인' > '…혼인(초혼)', 팀원 검색 실험 10/31 제안 5-B).
# 키는 표명에 쓰인 말, 값은 질문에서 그 수식어를 말한 것으로 볼 표현
SUBSET_MODIFIERS = {
    "초혼": re.compile(r"초혼|처음\s*(?:한\s*)?결혼|첫\s*결혼"),
    "재혼": re.compile(r"재혼|다시\s*(?:한\s*)?결혼|두\s*번째\s*결혼"),
    "쌍태아": re.compile(r"쌍태아|쌍둥이|다태아"),
    "발생기준": re.compile(r"발생\s*기준"),
    "북한": re.compile(r"북한|탈북|새터민"),
}
MODIFIER_PENALTY = 0.03  # 형제 표끼리 임베딩 점수 차(0.01 안팎)를 뒤집을 만큼만

# 하이브리드: 임베딩 우선 + 키워드 조건부 가산 (팀원 검색 실험 10/31: 고정 RRF는 약한 키워드의
# 오답이 임베딩 정답을 끌어내림). 질문 임베딩이 있을 때만 쓰고, 없으면 위 키워드 기준을 쓴다
DENSE_TOP_K = 50  # 키워드 점수가 0이어도 임베딩 상위 이만큼은 후보에 넣는다
KEYWORD_GATE = 0.5  # 키워드 점수가 이 이상일 때만 키워드·표명 유사도를 더한다
# 0.3이면 엉뚱한 단어로 기준을 겨우 넘은 키워드('우리 동네'의 동네·주민 → 사할린동포 조사 표)가
# 임베딩 1위를 뒤집었다. 0.2는 일상어 질문 10개(scripts/check_search.py) 기준 1차 값
KEYWORD_WEIGHT = 0.2
# 범위 밖 판정은 조건 가감점 전 임베딩 원점수(1위)로 한다 (골든셋 설계 8장). 가감점 후 점수로 보면
# HCX가 뽑은 대상('쌍둥이')이 표명에 없다는 감점만으로 맞는 표를 버린다.
# 범위 밖 질문의 원점수(0.50~0.65)가 범위 안 질문과 겹쳐 점수만으로는 못 가른다.
# 명백한 경우만 거르고, 나머지는 HCX query_type(not_statistics)에 맡긴다
HYBRID_MIN_SCORE = 0.5
# HCX가 not_statistics라고 해도 임베딩 1위 점수가 이 이상이면 검색한다. HCX는 일상어 질문
# ('연탄으로 난방하는 집이 있나', '누가 돌보는지')을 잡담으로 자주 본다 (개발셋 범위 안 6~10건,
# 임베딩 0.53~0.66). 진짜 통계 아닌 질문은 날씨 0.47~0.51. 프롬프트로 고치면 다른 질문이
# 새로 거절돼(11/1 실험: 오판 9→12) 점수와 함께 본다. 신청 방법('월세 지원금 신청', 0.57)은
# 넘어가지만 관련 표를 안내하는 정도다. 기준은 개발셋으로 정한 1차 값
NOT_STATISTICS_OVERRIDE = 0.55
# 같은 주제의 형제 패밀리('고령자인구' 4개)는 임베딩 점수 차가 0.01 안쪽이라 거의 동점일 때만 묻는다
HYBRID_AMBIGUOUS_GAP = 0.005

# 연도를 말하지 않은 질문은 지금 표를 찾는 것으로 본다. 다른 패밀리의 옛날 단일 연도 표
# ('2005년 연령/성별 인구')가 현행 표('2015~2025')를 이기는 일이 많아(개발셋 '다른 패밀리 1위'
# 31건의 주된 유형) 최신 판·긴 수록 기간에 가산한다. choose는 패밀리 안에서만 최신 판을
# 고르므로 패밀리가 다르면 못 잡는다. 값은 팀원 실험실 재현(11/1, 흐름 평균 +2.6p)
RECENT_END_YEAR = 2023  # 이 해 이후까지 담은 표가 최신 판
RECENT_BONUS = 0.05
LONG_SPAN_YEARS = 10  # 수록 기간이 이 이상이면 긴 표 (단일 연도 표가 2,364개 중 1,926개)
LONG_SPAN_BONUS = 0.05
# 연도 없는 질문에서 수록이 오래전에 끝난 단일 연도 표 감점 (2005년 10% 표본 블록 등).
# 최신판 가산은 2023년 이후 표에만 붙어 5년 주기 총조사(최신 2020)는 2005년 표와 동점이었다
# (개발셋 d12 '노인 생활비': 2005년 65세 표 0.907 > 2020년 60세 표 0.862)
STALE_YEARS = 15
STALE_PENALTY = 0.08  # 0.05 로는 d12 가 0.004 차로 2005년 표에 밀렸다 (v6 측정)
# 연도 대신 옛날을 가리키는 말이 있으면 가산하지 않는다
PAST = re.compile(r"옛날|예전|과거|당시|그때|그\s*시절|\d+\s*년대|\d+\s*년\s*전")

STOPWORDS = {
    "수", "통계", "통계표", "자료", "데이터", "현황", "관련", "표", "정보", "찾아줘", "알려줘",
    "보여줘", "몇", "명", "얼마", "얼마나", "몇명", "추이", "변화", "있어", "있나요",
}  # fmt: skip
# '취득한'·'취득자' → '취득', '국적을' → '국적'. 조사·어미 한 글자만 떼어 본다
SUFFIXES = ("한", "된", "을", "를", "의", "은", "는", "이", "가", "에", "별", "인", "자")
MIGRATION = re.compile(r"이동|전입|전출|이주|이사")
PROJECTION_TABLE = re.compile(r"추계|장래")

TABLES_SQL = """
SELECT s.tbl_id, s.org_id, s.table_name, s.survey_name, s.organization, s.period, s.path_texts,
       s.classifications, s.items, s.start_year, s.end_year, s.region_levels,
       s.prev_region_levels, s.sex_coverage, s.has_total, s.url, s.comments,
       f.family_id, f.family_name, f.target, f.sex, f.edition_order, f.edition, f.renamed_from
FROM stat_tables s JOIN table_families f USING (tbl_id)
"""

_tables: list[dict[str, Any]] | None = None
_vectors: Any = None  # _tables 순서의 표 벡터 행렬 (numpy). 없으면 None


def norm(value: str) -> str:
    """띄어쓰기·기호를 지운다 ('1인 가구' = '1인가구')"""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


def prepare(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """검색에 쓸 정규화 문자열을 붙인다"""
    tables = []
    for r in rows:
        t = dict(r)
        classes = t["classifications"] or []
        folders = [p.rsplit(">", 1)[0] for p in t["path_texts"] or []]
        t["name_n"] = norm(t["table_name"])
        t["path_n"] = norm(" ".join([t["survey_name"], *folders]))
        t["class_n"] = [norm(c["name"]) for c in classes]
        t["meta_n"] = norm(
            " ".join(
                [c["name"] for c in classes]
                + [v for c in classes for v in c.get("sample_values") or [] if v]
                + [i["name"] for i in t["items"] or []]
            )
        )
        t["age_cov"] = age_coverage(t["name_n"], t["class_n"], t["meta_n"])
        tables.append(t)
    return tables


def age_coverage(name_n: str, class_n: list[str], meta_n: str) -> tuple[int, int | None] | None:
    """표가 담는 연령 범위. 연령 축이 없으면 None, 있으면 (시작, 끝 또는 None=이상)"""
    text = name_n + " " + " ".join(class_n)
    if m := re.search(r"(\d+)세이상", text):
        return (int(m[1]), None)
    if m := re.search(r"(\d+)[~\-](\d+)세", text):
        return (int(m[1]), int(m[2]))
    # '주요 연령계층별 추계인구(생산연령인구, 고령인구 등)'처럼 연령 축이 있는 표는 전 연령을
    # 담는다.
    # 고령자·고령인구·노인은 연령 축 표시가 없을 때만 65세 이상 전용으로 본다
    if "연령" in text or "각세" in text or "연령" in meta_n:
        return (0, None)
    if "고령자" in text or "고령인구" in text or "노인" in text:
        return (65, None)
    return None


async def load_tables() -> list[dict[str, Any]]:
    global _tables, _vectors
    if _tables is None:
        from app.core.db import engine

        async with engine.connect() as conn:
            rows = (await conn.execute(text(TABLES_SQL))).mappings().all()
            tables = prepare([dict(r) for r in rows])
            _vectors = await vector_search.load_matrix(conn, [t["tbl_id"] for t in tables])
        if _vectors is not None:
            vector_search.load_adapter([t["tbl_id"] for t in tables])
        _tables = tables
    return _tables


async def warm_up() -> None:
    """서버 시작 때 표·벡터를 미리 읽는다. 실패해도(DB가 비었거나 아직 안 뜸) 서버는 뜨고,
    첫 /search에서 다시 읽는다 (실전 1 /ask는 이 데이터와 무관)"""
    start = time.perf_counter()
    try:
        tables = await load_tables()
        await vector_search.warm_up()
    except Exception as e:
        startup_log.warning(
            "검색 데이터 미리 읽기 실패 → 첫 /search에서 다시 읽음: %s: %s", type(e).__name__, e
        )
        return
    startup_log.info(
        "검색 데이터 준비: 표 %d개, 표 벡터 %s, %.1f초",
        len(tables), "있음" if _vectors is not None else "없음(키워드 검색만)",
        time.perf_counter() - start,
    )  # fmt: skip


def status() -> dict[str, Any]:
    """검색 준비 상태 (/health/search)"""
    return {
        "tables": len(_tables or []),
        "vectors": _vectors is not None,
        "embedding_backend": vector_search.get_settings().embedding_backend,
        "retrieval": "hybrid" if _vectors is not None else "keyword",
        "adapter": vector_search.adapter_loaded(),
    }


async def dense_scores(question: str) -> list[float] | None:
    """load_tables() 순서대로 표마다 질문과의 코사인 유사도. 표 벡터나 질문 벡터가 없으면 None"""
    await load_tables()
    if _vectors is None:
        return None
    query = await vector_search.embed_query(question)
    return None if query is None else vector_search.scores(_vectors, query)


# ---- 2. 후보 검색 ---------------------------------------------------------------


def keyword_query(slots: dict[str, Any]) -> str:
    """키워드 검색어: 질문 원문 정리 + 사전 확장.

    HCX topic·target은 요약하면서 지역·분류 단어('시군구별', '연령대별')를 빠뜨려 원문보다 나쁘다
    (팀원 키워드 비교 실험 10/31: 팀원셋 흐름 59→65, 생성셋 79→86, 사전까지 넣으면 78·89)
    """
    question = slots.get("question") or slots["topic"]
    # 빼 달라고 한 말('귀화한 사람 말고')은 사전으로 늘리면 오히려 그 표(국적취득)를 찾는다
    for word in slots.get("exclude") or []:
        question = question.replace(word, " ")
    return synonyms.keyword_text(question)


def query_tokens(slots: dict[str, Any], text: str | None = None) -> list[dict[str, Any]]:
    """검색어(text, 없으면 keyword_query) → 단어별 변형 목록 ('취득한' → ['취득한', '취득']).

    value: 슬롯 items에 든 단어('미혼 인구'의 '미혼'). 분류 값이라 표명에 없어도 되므로
    어느 필드에 있든 같은 점수를 준다.
    """
    words = re.findall(r"[0-9A-Za-z가-힣]+", keyword_query(slots) if text is None else text)
    values = norm(" ".join(slots["conditions"]["items"]))
    excluded = {v for w in slots.get("exclude") or [] for v in variants(w)}
    tokens = []
    for w in dict.fromkeys(words):
        # 빼 달라고 한 단어는 찾는 단어가 아니다 ('귀화한 사람 말고'의 '귀화')
        if w in STOPWORDS or excluded & set(variants(w)):
            continue
        tokens.append({"variants": variants(w), "value": bool(values) and w in values})
    return tokens


def variants(word: str) -> list[str]:
    """'취득한' → ['취득한', '취득']"""
    if len(word) >= 3 and word.endswith(SUFFIXES):
        return [word, word[:-1]]
    return [word]


def _found(variants: list[str], field: str) -> bool:
    return any(v in field for v in variants)


def keyword_scores(tables: list[dict[str, Any]], tokens: list[dict[str, Any]]) -> list[float]:
    """표마다 0~1 점수: 단어 희귀도(IDF) 가중 평균으로 어느 필드에 있는지 본다"""
    n = len(tables)
    idf = []
    for token in tokens:
        variants = token["variants"]
        df = sum(
            _found(variants, t["name_n"])
            or _found(variants, t["path_n"])
            or _found(variants, t["meta_n"])
            for t in tables
        )
        # 어느 표에도 없는 단어('아파트 매매 가격'의 '매매')는 가장 희귀한 단어로 쳐서 점수를 깎는다
        idf.append(math.log((n + 1) / (df + 1)) if df else math.log(n + 1))
    total = sum(idf)
    if total == 0:
        return [0.0] * n
    scores = []
    for t in tables:
        s = 0.0
        for token, w in zip(tokens, idf, strict=True):
            s += w * max(
                (
                    1.0 if token["value"] else FIELD_WEIGHTS[f]
                    for f in ("name", "path", "meta")
                    if _found(token["variants"], t[f"{f}_n"])
                ),
                default=0.0,
            )
        scores.append(s / total)
    return scores


# ---- 3. 조건 검증 ---------------------------------------------------------------


def check(t: dict[str, Any], slots: dict[str, Any]) -> dict[str, bool]:
    """판(표) 단위 조건 판정. 질문에 없는 조건은 결과에 넣지 않는다"""
    c = slots["conditions"]
    out: dict[str, bool] = {}
    if c["year"] is not None:
        last = c["year_end"] or c["year"]
        out["year"] = t["start_year"] <= c["year"] and last <= t["end_year"]
        if not out["year"] and slots.get("time_ref") and last > t["end_year"]:
            # "작년"·"올해"는 아직 공표 전일 수 있다. 공표 시차(PUBLISH_LAG) 안이면 최신 판을 맞는
            # 표로 본다
            out["year"] = t["start_year"] <= c["year"] and last - t["end_year"] <= PUBLISH_LAG
    if c["region_level"]:
        levels = set(t["region_levels"])
        if MIGRATION.search(slots["topic"]):
            levels |= set(t["prev_region_levels"])
        out["region_level"] = c["region_level"] in levels
    sex = c["sex"]
    if sex in ("남", "여"):
        own = "male_only" if sex == "남" else "female_only"
        out["sex"] = t["sex_coverage"] in ("both", own) or t["sex"] == sex
    elif sex == "전체" and t["sex_coverage"] != "none":
        out["sex"] = t["has_total"] is not False
    for cls in c["classes"]:
        if cls == "성별":
            ok = t["sex_coverage"] == "both"
        elif cls == "가구주 성별":
            ok = any("가구주" in n and "성" in n for n in t["class_n"])
        else:
            key = norm(cls).removesuffix("별")
            # HCX 가 '미혼'처럼 분류 값을 classes 에 넣기도 하므로 항목·표본값(meta_n)도 본다
            ok = any(key in n for n in t["class_n"]) or key in t["name_n"] or key in t["meta_n"]
        if (
            cls == "성별" and not ok
        ):  # 항목이 남자/여자로 갈라진 표도 성별을 담는다 (주민등록 시군구별 성별 인구수)
            ok = "남자" in t["meta_n"] and "여자" in t["meta_n"]
        out[f"class:{cls}"] = ok
    for item in c["items"]:
        key = norm(item)
        out[f"item:{item}"] = key in t["meta_n"] or key in t["name_n"]
    if (age := slots.get("age")) and t["age_cov"] is not None:
        # 연령 집단("노인")을 담을 수 있는 표인가: 표의 연령 범위가 집단 범위를 포함해야 한다. 연령
        # 축이 없는 표는 판정하지 않는다
        lo, hi = t["age_cov"]
        out["age"] = lo <= age["lo"] and (hi is None or (age["hi"] is not None and age["hi"] <= hi))
    if slots.get("projection"):
        out["projection"] = bool(PROJECTION_TABLE.search(t["survey_name"] + t["name_n"]))
    if slots.get("spouse"):
        out["spouse"] = norm(slots["spouse"]) in t["name_n"]
    if slots.get("exclude"):
        out["exclude"] = not any(
            _found(variants(w), t["name_n"]) for w in slots["exclude"]
        )  # fmt: skip
    # '전체'는 멤버 선택값이지 표가 갖춰야 할 조건이 아니다
    if slots.get("target") and slots["target"] != "전체":
        key = norm(slots["target"])
        # 조사 이름·경로에 있는 대상('사할린 동포', '고령친화')은 그 조사의 모든 표가 담는다
        out["target"] = (
            key in t["name_n"]
            or key in t["meta_n"]
            or key in norm(t["target"] or "")
            or key in t["path_n"]
        )
    return out


def bigrams(value: str) -> set[str]:
    return {value[i : i + 2] for i in range(len(value) - 1)}


def name_similarity(query: str, name: str) -> float:
    """주제와 표명의 글자 2개 단위 겹침 (Dice). 같은 단어를 가진 표 중 이름이 가까운 표를 앞으로"""
    a, b = bigrams(query), bigrams(name)
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def relevance(keyword: float, name_sim: float, dense: float | None) -> float:
    """후보 점수의 바탕 (조건 가감점 전).

    키워드만: 키워드 + 표명 유사도.
    하이브리드: 임베딩 + (키워드가 높을 때만) 키워드·표명 유사도. 표명 유사도도 글자 겹침이라
    키워드와 같이 묶는다 ('결혼 몇 건'이 짧은 표명 '결혼 여부'에 끌려가지 않게)
    """
    lexical = keyword + NAME_WEIGHT * name_sim
    if dense is None:
        return lexical
    return dense + (KEYWORD_WEIGHT * lexical if keyword >= KEYWORD_GATE else 0.0)


def wants_current(slots: dict[str, Any]) -> bool:
    """연도도, 옛날을 가리키는 말도 없는 질문 → 지금 표를 찾는다"""
    c = slots["conditions"]
    return (
        c["year"] is None and c["year_end"] is None and not PAST.search(slots.get("question", ""))
    )


def recency_bonus(t: dict[str, Any], slots: dict[str, Any]) -> float:
    """최신 판(RECENT_END_YEAR 이후까지)·긴 수록 기간(LONG_SPAN_YEARS 이상) 가산.
    장래 추계 표(~2072)는 늘 최신·긴 표라, 미래를 묻지 않으면 가산하지 않는다
    ('애를 더 낳을 생각이 있는 여성' → 시나리오별 추계인구가 1위로 오던 문제)"""
    if not slots.get("projection") and PROJECTION_TABLE.search(t["survey_name"] + t["name_n"]):
        return 0.0
    long_span = t["end_year"] - t["start_year"] >= LONG_SPAN_YEARS
    stale = not long_span and t["end_year"] <= RECENT_END_YEAR - STALE_YEARS
    return (
        RECENT_BONUS * (t["end_year"] >= RECENT_END_YEAR)
        + LONG_SPAN_BONUS * long_span
        - STALE_PENALTY * stale
    )


def _population_unmentioned(t: dict[str, Any], said_n: str) -> bool:
    """특정 집단 조사의 표인데 질문에 그 집단 말이 없다"""
    words = SURVEY_POPULATION.get(t["survey_name"])
    return bool(words) and not any(norm(w) in said_n for w in words)


def unasked_modifiers(t: dict[str, Any], said: str) -> list[str]:
    """표명에 있지만 질문(said)에서 말하지 않은 모집단 수식어"""
    return [
        m for m, asked in SUBSET_MODIFIERS.items() if m in t["name_n"] and not asked.search(said)
    ]


def rank(
    tables: list[dict[str, Any]], slots: dict[str, Any], dense: list[float] | None = None
) -> list[dict[str, Any]]:
    """dense: tables 순서의 임베딩 점수 (없으면 키워드만)"""
    text = keyword_query(slots)
    tokens = query_tokens(slots, text)
    query = norm(text)
    said = f"{slots.get('question', '')} {slots['topic']} {slots.get('target') or ''}"
    keywords = keyword_scores(tables, tokens)
    if dense is not None:
        # 키워드 원점수는 IDF 가중 평균이라 질문 단어 하나만 표에 없어도 게이트(0.5)를 못 넘는다
        # (1위 원점수 중앙값 0.37, 0.5 이상 27%). 질문마다 1위를 1로 맞춘 뒤 게이트를 적용한다.
        # 키워드만 쓸 때는 원점수로 범위 밖(MIN_SCORE)을 판정하므로 정규화하지 않는다
        top = max(keywords, default=0.0) or 1.0
        keywords = [k / top for k in keywords]
    cut = sorted(dense, reverse=True)[min(DENSE_TOP_K, len(dense)) - 1] if dense else None
    # 키워드만 쓸 때는 패밀리 점수로 범위 밖을 판정하므로 가산하지 않는다
    timeless = dense is not None and wants_current(slots)
    said_n = norm(said)
    ranked = []
    for i, (t, kw) in enumerate(zip(tables, keywords, strict=True)):
        d = dense[i] if dense else None
        if kw == 0 and (d is None or d < cut):
            continue
        checks = check(t, slots)
        passed = sum(checks.values())
        failed = [k for k, ok in checks.items() if not ok]
        soft_failed = sum(k.split(":")[0] in SOFT_CHECKS for k in failed)
        score = (
            relevance(kw, name_similarity(query, t["name_n"]), d)
            + PASS_BONUS * passed
            - FAIL_PENALTY * (len(failed) - soft_failed)
            - SOFT_FAIL_PENALTY * soft_failed
            - MODIFIER_PENALTY * len(unasked_modifiers(t, said))
            - (SURVEY_PENALTY if _population_unmentioned(t, said_n) else 0.0)
            + (recency_bonus(t, slots) if timeless else 0.0)
        )
        ranked.append({"table": t, "keyword": kw, "dense": d, "checks": checks, "score": score})
    ranked.sort(key=lambda r: (-r["score"], r["table"]["edition_order"], r["table"]["tbl_id"]))
    return ranked


# ---- 4·5. 패밀리 묶기, 멤버·판 선택 ----------------------------------------------


def fold(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 패밀리는 최고 점수 표 하나로 접는다"""
    seen: dict[str, dict[str, Any]] = {}
    for r in ranked:
        seen.setdefault(r["table"]["family_id"], r)
    return list(seen.values())


def family_axes(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    def options(key: str) -> list[str]:
        values = {r[key] for r in rows if r[key] is not None}
        return sorted(values, key=lambda v: (v != "전체" and v != "계", v))

    axes = {"target": options("target"), "sex": options("sex")}
    # 최신 연도를 담은 판 → 기간이 긴 판 순 (규칙 2.4, '2006~2015 / 2007~2015 / 2015')
    editions = sorted({(-r["end_year"], r["start_year"], r["edition"]) for r in rows})
    axes["edition"] = list(dict.fromkeys(e for _, _, e in editions))
    return {k: v for k, v in axes.items() if len(v) > 1}


def choose(
    family: list[dict[str, Any]], slots: dict[str, Any]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """패밀리 안에서 표 하나 → (표, 빠진 축, 연도 메모)

    되묻기 답이나 화면 버튼으로 고른 값은 slots의 target('전체' 포함)·edition·tbl_id로 들어온다.
    """
    c = slots["conditions"]
    picked = [r for r in family if r["tbl_id"] == slots.get("tbl_id")]
    if picked:  # 화면에서 표를 직접 고른 경우
        return picked[0], [], []
    axes = family_axes(family)
    missing = []
    rows = family

    if "target" in axes:
        # 대상 슬롯이 패밀리명이어도('한국국적 취득자') 질문 속 멤버 이름('혼인 귀화자')으로 고른다
        # 실험실 §47 멤버 매칭: 질문·대상을 사전(synonyms)으로 넓힌 뒤 멤버 이름과 대조
        said = norm(
            synonyms.keyword_text(f"{slots.get('question', '')} {slots.get('target') or ''}")
        )
        match = [r for r in rows if r["target"] != "전체" and norm(r["target"]) in said]
        if slots.get("target") == "전체":
            match = [r for r in rows if r["target"] == "전체"]
        if not match:
            missing.append("target")
            match = [r for r in rows if r["target"] == "전체"] or rows
        rows = match
    if "sex" in axes:
        want = {"남": "남", "여": "여", "전체": "계"}.get(c["sex"] or "")
        match = [r for r in rows if r["sex"] == want]
        if not match:
            if c["sex"] is None:
                missing.append("sex")
            match = [r for r in rows if r["sex"] == "계"] or rows
        rows = match
    rows = [r for r in rows if r["edition"] == slots.get("edition")] or rows

    notes = []
    year = c["year"]
    if year is None:
        # 가장 최신 연도를 담은 판 → 그중 기간이 긴 판 (edition_order 1)
        best = min(rows, key=lambda r: (r["edition_order"], r["tbl_id"]))
    else:
        last = c["year_end"] or year
        covering = [r for r in rows if r["start_year"] <= year and last <= r["end_year"]]
        if covering:
            best = max(covering, key=lambda r: (r["end_year"] - r["start_year"], r["end_year"]))
        else:
            best = min(
                rows,
                key=lambda r: (
                    max(r["start_year"] - year, year - r["end_year"], 0),
                    r["edition_order"],
                ),
            )
            said = f"'{ref['expr']}'({year}년)" if (ref := slots.get("time_ref")) else f"{year}년"
            notes.append(f"{said}을 담은 판이 없어 가장 가까운 {best['edition']}년 판을 골랐습니다")
    return best, missing, notes


# ---- 6. 상태 판단 ---------------------------------------------------------------


def _brief(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "tbl_id": t["tbl_id"],
        "table_name": t["table_name"],
        "survey_name": t["survey_name"],
        "edition": t["edition"],
        "url": t["url"],
    }


def find(
    tables: list[dict[str, Any]],
    slots: dict[str, Any],
    allow_clarify: bool = True,
    dense: list[float] | None = None,
    scope_by_llm: bool = False,
) -> dict[str, Any]:
    """슬롯 → 검색 결과 (DB·HCX 없이 동작해서 테스트·실험에 쓴다).
    dense(tables 순서의 임베딩 점수)를 주면 하이브리드, 없으면 키워드만.
    scope_by_llm: 범위 밖 판정(HCX not_statistics·점수 문턱)을 뒤의 LLM 검토에 맡기고 여기서는
    거절하지 않는다
    (실험실 §61-3: 점수 문턱은 범위 안 질문을 잘못 거르고, 007 거르기가 범위 밖 30/30·
    거짓 거절 1/102)"""
    result: dict[str, Any] = {"status": None, "reason": None, "slots": slots, "table": None}
    if slots["query_type"] == "not_statistics" and scope_by_llm and dense is not None:
        slots["query_type"] = "find_table"
        slots.setdefault("fixes", []).append(
            {
                "key": "query_type",
                "from": "not_statistics",
                "to": "find_table",
                "reason": "LLM 검토가 범위를 판정",
            }
        )
    if slots["query_type"] == "not_statistics":
        top_dense = max(dense, default=0.0) if dense else None
        if top_dense is None or top_dense < NOT_STATISTICS_OVERRIDE:
            return {**result, "status": "not_statistics", "reason": "통계표를 찾는 질문이 아닙니다"}
        slots["query_type"] = "find_table"
        slots.setdefault("fixes", []).append(
            {"key": "query_type", "from": "not_statistics", "to": "find_table",
             "reason": f"임베딩 1위 {top_dense:.3f} ≥ {NOT_STATISTICS_OVERRIDE}"}
        )  # fmt: skip

    ranked = rank(tables, slots, dense)
    families = fold(ranked)
    if dense is None:
        gap = AMBIGUOUS_GAP
        in_scope = bool(families) and families[0]["score"] >= MIN_SCORE
    else:
        gap = HYBRID_AMBIGUOUS_GAP
        in_scope = bool(families) and (scope_by_llm or max(dense) >= HYBRID_MIN_SCORE)
    result["retrieval"] = "keyword" if dense is None else "hybrid"
    result["candidates"] = [
        {
            **_brief(r["table"]),
            "score": round(r["score"], 3),
            "keyword": round(r["keyword"], 3),
            "dense": None if r["dense"] is None else round(r["dense"], 3),
            "checks": r["checks"],
        }
        for r in ranked[:10]
    ]
    if not in_scope:
        return {
            **result,
            "status": "out_of_scope",
            "reason": "질문에 맞는 통계표를 찾지 못했습니다",
        }

    top = families[0]
    others = [
        {
            "family_id": f["table"]["family_id"],
            "family_name": f["table"]["family_name"],
            "score": round(f["score"], 3),
            "checks": f["checks"],
            **_brief(f["table"]),
        }
        for f in families[1:TOP_FAMILIES]
    ]
    result["other_families"] = others
    if len(families) > 1 and top["score"] - families[1]["score"] < gap:
        close = [o for o in others if top["score"] - o["score"] < gap]
        result["options"] = [
            {"family_id": top["table"]["family_id"], "family_name": top["table"]["family_name"],
             "score": round(top["score"], 3), **_brief(top["table"])},
            *close,
        ]  # fmt: skip
        return {**result, "status": "ambiguous", "reason": "비슷한 통계표 묶음이 여럿입니다"}

    return finish(result, tables, top["table"]["family_id"], top["score"], allow_clarify)


def resolve(
    tables: list[dict[str, Any]],
    slots: dict[str, Any],
    family_id: str,
    allow_clarify: bool = True,
) -> dict[str, Any]:
    """패밀리가 정해진 뒤(되묻기 답) 검색 없이 멤버·판만 고른다"""
    result = {"status": None, "reason": None, "slots": slots, "table": None, "candidates": []}
    return finish(result, tables, family_id, None, allow_clarify)


def clarify_axes(
    family: list[dict[str, Any]], slots: dict[str, Any], missing: list[str]
) -> list[dict[str, Any]]:
    """되묻기 축 (규칙 2.3 가안). 기본값이 없는 대상이 빠졌을 때만 묻고,
    기본값이 있는 성별·연도는 같은 메시지에 함께 넣는다"""
    if "target" not in missing:
        return []
    axes = family_axes(family)
    asked = [{"axis": "target", "options": axes["target"]}]
    if "sex" in missing:
        asked.append({"axis": "sex", "options": ["전체" if v == "계" else v for v in axes["sex"]]})
    no_year = slots["conditions"]["year"] is None and not slots.get("edition")
    if "edition" in axes and no_year:
        asked.append({"axis": "edition", "options": axes["edition"]})
    return asked


def finish(
    result: dict[str, Any],
    tables: list[dict[str, Any]],
    family_id: str,
    score: float | None,
    allow_clarify: bool,
) -> dict[str, Any]:
    """패밀리 확정 후: 멤버·판 선택 → 되묻기 또는 표 하나"""
    slots = result["slots"]
    family = [t for t in tables if t["family_id"] == family_id]
    table, missing, notes = choose(family, slots)
    info = {
        "family_id": family_id,
        "family_name": table["family_name"],
        "survey_name": table["survey_name"],
        "organization": table["organization"],
        "axes": family_axes(family),
    }
    asked = clarify_axes(family, slots, missing) if allow_clarify else []
    if asked:
        return {
            **result,
            "status": "clarify",
            "score": score,
            "family": info,
            "clarify_axes": asked,
            "missing_axes": missing,
            "reason": "패밀리 안에서 표를 고를 조건이 부족합니다",
        }
    checks = check(table, slots)
    failed = [k for k, ok in checks.items() if not ok]
    result.update(
        {
            "status": "partial_match" if failed or notes else "ok",
            "score": score,
            "reason": "; ".join([*notes, *(f"조건 불충족: {k}" for k in failed)]) or None,
            "notes": notes,
            "failed": failed,
            "table": {**_brief(table), "period": table["period"], "checks": checks},
            "family": info,
            "missing_axes": missing,
            "other_editions": [
                _brief(t)
                for t in sorted(family, key=lambda t: -t["end_year"])
                if t["tbl_id"] != table["tbl_id"]
                and t["target"] == table["target"]
                and t["sex"] == table["sex"]
            ],
        }
    )
    return result


async def search(
    question: str, trace: dict[str, Any] | None = None, allow_clarify: bool = True
) -> dict[str, Any]:
    """trace(dict)를 주면 HCX 원본 출력과 단계별 시간(ms)을 담는다 (요청 로그용)"""
    trace = {} if trace is None else trace
    start = time.perf_counter()

    async def timed(coro: Any) -> tuple[Any, float]:
        value = await coro
        return value, time.perf_counter()

    # 슬롯 추출(HCX)과 질문 임베딩은 서로 기다릴 필요가 없어 함께 보낸다
    (slots, parsed_at), (dense, embedded_at) = await asyncio.gather(
        timed(slot_parser.extract(question, trace)), timed(dense_scores(question))
    )
    # 재작성 검색어(HCX search_query)가 있으면 그 임베딩 점수와 평균한다 (실험실 §52: 교체는 손해,
    # 평균만
    # 이득). 슬롯이 온 뒤에야 알 수 있어 임베딩 호출이 한 번 더 든다
    rewrite = (slots.get("search_query") or "").strip()
    if dense is not None and rewrite and rewrite != question.strip():
        dense2, embedded_at = await timed(dense_scores(rewrite))
        if dense2 is not None:
            dense = [(a + b) / 2 for a, b in zip(dense, dense2, strict=True)]
    ready = max(parsed_at, embedded_at)
    tables = await load_tables()
    result = find(
        tables, slots, allow_clarify, dense, scope_by_llm=vector_search.get_settings().llm_pick
    )
    searched = time.perf_counter()
    from app.services import llm_pick  # 검색 뒤 HCX-007 최종 검토

    result = await llm_pick.review(question, result, tables, trace)
    trace["timings"] = {
        "slots_ms": round((parsed_at - start) * 1000),
        "embed_ms": round((embedded_at - start) * 1000),
        "search_ms": round((searched - ready) * 1000),
        "llm_pick_ms": round((time.perf_counter() - searched) * 1000),
    }
    return result


def _print(result: dict[str, Any]) -> None:
    keep = {k: v for k, v in result.items() if k not in ("candidates", "slots")}
    print(json.dumps(keep, ensure_ascii=False, indent=2))
    print("\n후보 상위:")
    for c in result.get("candidates", [])[:5]:
        dense = "-" if c["dense"] is None else f"{c['dense']:.3f}"
        print(
            f"  {c['score']:.3f} (키워드 {c['keyword']:.3f} · 임베딩 {dense})"
            f" {c['table_name']} ({c['edition']}) {c['checks']}"
        )


if __name__ == "__main__":
    _print(asyncio.run(search(" ".join(sys.argv[1:]) or "2024년 부산 시군구별 혼인 귀화자 수")))
