import asyncio
import json
import logging
import math
import re
import sys
import time
from datetime import date
from typing import Any

from sqlalchemy import text

from app.services import agegroup, search_llm, synonyms, timeref, vector_search
from app.services import slots as slot_parser
from app.services import slots as slots_mod

logger = logging.getLogger(__name__)
startup_log = logging.getLogger("uvicorn.error")

FIELD_WEIGHTS = {"name": 1.0, "path": 0.6, "meta": 0.4}
NAME_WEIGHT = 0.3
PASS_BONUS = 0.08
FAIL_PENALTY = 0.15
SOFT_CHECKS = ("target", "class", "item", "sex", "spouse", "period")
REGION_SHORT = {name: aliases[0] for name, aliases in slots_mod.SIDO.items()}
CLASS_ALIAS = {
    "나이": "연령",
    "나이대": "연령",
    "연령대": "연령",
    "연령층": "연령",
    "연령계층": "연령",
    "학력": "교육정도",
    "국적": "국적",
    "부모국적": "국적",
    "부모의국적": "국적",
}
SURVEY_POPULATION = {
    "영주귀국사할린동포실태조사": ("사할린", "동포"),
    "고령친화용품제조업실태조사": ("고령친화", "노인용품", "용품", "실버"),
}
SURVEY_PENALTY = 0.1
SOFT_FAIL_PENALTY = 0.05
CENSUS_CYCLE = 5
PUBLISH_LAG = 2

MIN_SCORE = 0.45
AMBIGUOUS_GAP = 0.03
TOP_FAMILIES = 4

SUBSET_MODIFIERS = {
    "초혼": re.compile(r"초혼|처음\s*(?:한\s*)?결혼|첫\s*결혼"),
    "재혼": re.compile(r"재혼|다시\s*(?:한\s*)?결혼|두\s*번째\s*결혼"),
    "쌍태아": re.compile(r"쌍태아|쌍둥이|다태아"),
    "발생기준": re.compile(r"발생\s*기준"),
    "북한": re.compile(r"북한|탈북|새터민"),
}
MODIFIER_PENALTY = 0.03

DENSE_TOP_K = 50
KEYWORD_GATE = 0.5
KEYWORD_WEIGHT = 0.2
HYBRID_MIN_SCORE = 0.5
NOT_STATISTICS_OVERRIDE = 0.55
HYBRID_AMBIGUOUS_GAP = 0.005

RECENT_END_YEAR = 2020
RECENT_BONUS = 0.05
LONG_SPAN_YEARS = 10
LONG_SPAN_BONUS = 0.05
STALE_END_YEAR = 2008
STALE_PENALTY = 0.08
PAST = re.compile(r"옛날|예전|과거|당시|그때|그\s*시절|\d+\s*년대|\d+\s*년\s*" + timeref.AGO)

STOPWORDS = {
    "수", "통계", "통계표", "자료", "데이터", "현황", "관련", "표", "정보", "찾아줘", "알려줘",
    "보여줘", "몇", "명", "얼마", "얼마나", "몇명", "추이", "변화", "있어", "있나요",
}  # fmt: skip
SUFFIXES = ("한", "된", "을", "를", "의", "은", "는", "이", "가", "에", "별", "인", "자")
MIGRATION = re.compile(r"이동|전입|전출|이주|이사")
FEMALE_ONLY_TABLE = re.compile(r"여성|부인|출산율|출산률|모의연령|가임")
MALE_VALUES = frozenset({"남자", "남성", "남편"})
FEMALE_VALUES = frozenset({"여자", "여성", "아내"})
PROJECTION_TABLE = re.compile(r"추계|장래")

TABLES_SQL = """
SELECT s.tbl_id, s.org_id, s.table_name, s.survey_name, s.organization, s.period, s.path_texts,
       s.classifications, s.items, s.start_year, s.end_year, s.region_levels,
       s.prev_region_levels, s.sex_coverage, s.has_total, s.url, s.comments,
       f.family_id, f.family_name, f.target, f.sex, f.edition_order, f.edition, f.renamed_from
FROM catalog.table_meta s JOIN catalog.table_families f USING (tbl_id)
"""

_tables: list[dict[str, Any]] | None = None
_vectors: Any = None


def norm(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


TARGET_PARTS = re.compile(r"[,·/()]|\s및\s")


def mentioned_targets(targets: list[str], said: str) -> list[str]:
    names = [t for t in dict.fromkeys(targets) if t and t != "전체"]
    whole = [t for t in names if norm(t) in said]
    if whole:
        return [t for t in whole if not any(norm(t) in norm(o) and t != o for o in whole)]

    def parts(t: str) -> list[str]:
        return [p for p in (norm(x) for x in TARGET_PARTS.split(t)) if len(p) >= 2]

    return [t for t in names if any(p in said for p in parts(t))]


WORD_SPLIT = re.compile(r"[_\s/·,()\[\]–\-]+")
TOTAL_WORDS = {"계", "합계", "전체", "소계"}


def stem(word: str) -> str:
    w = norm(word)
    return w[:-1] if len(w) > 2 and w.endswith("수") else w


def word_set(names: list[str]) -> set[str]:
    out = set()
    for name in names:
        for part in WORD_SPLIT.split(name or ""):
            if (w := stem(part)) and w not in TOTAL_WORDS:
                out.add(w)
    return out


RANK_VALUES_MAX = 30


def rank_values(c: dict[str, Any]) -> list[str]:
    values = c.get("values") or []
    if values and len(values) <= RANK_VALUES_MAX:
        return values
    return c.get("sample_values") or []


VALUE_PARTS = re.compile(r"[^0-9A-Za-z가-힣\s]+")


def value_keys(value: str) -> set[str]:
    keys = {norm(value)} | {norm(part) for part in VALUE_PARTS.split(value)}
    keys.discard("")
    return keys


AGE_AXIS = re.compile(r"연령|나이|각세|\d+세")
AGE_INDEX = re.compile(r"노령화지수|부양비|중위연령|평균연령")


def has_age_axis(t: dict[str, Any]) -> bool:
    return (
        any(AGE_AXIS.search(n) for n in t["class_n"])
        or len(set(re.findall(r"\d+세", t["meta_n"]))) >= 3
        or bool(AGE_INDEX.search(t["meta_n"]))
    )


def has_value(t: dict[str, Any], key: str) -> bool:
    return key in t["meta_n"] or key in t.get("values_n", ())


# '입국연도가 2024년'의 항목 '2024년'. 입국연도별 표는 칸 이름이 '2024'라 글자로는 맞지 않아,
# 항목 검사가 ✘로 보고 '2024년 조건을 충족하지 않습니다'를 붙였다 (LLM 검토가 exact 라고 할 때만
# 가려졌다, 11/28)
YEAR_ITEM = re.compile(r"^((?:19|20)\d{2})년?$")


def has_year_value(t: dict[str, Any], year: str) -> bool:
    """표가 그 해를 분류 값(입국연도 · 귀화연도 같은 칸)으로 가졌는가.
    이름이 정확히 그 해인 칸만 본다"""
    names = {norm(i["name"]) for i in t.get("items") or []}
    values = t.get("values_n", ())
    return any(k in names or k in values for k in (year, f"{year}년"))


def _set_family_last_year(tables: list[dict[str, Any]]) -> None:
    fam_last: dict[str, int] = {}
    for t in tables:
        k = t.get("family_id") or t["tbl_id"]
        if t.get("end_year") is not None:
            fam_last[k] = max(fam_last.get(k, t["end_year"]), t["end_year"])
    for t in tables:
        t["family_last_year"] = fam_last.get(t.get("family_id") or t["tbl_id"], t.get("end_year"))


def prepare(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                + [v for c in classes for v in rank_values(c) if v]
                + [i["name"] for i in t["items"] or []]
            )
        )
        t["values_n"] = {k for c in classes for v in c.get("values") or [] for k in value_keys(v)}
        named = {
            k.removesuffix("수")
            for name in [v for c in classes for v in c.get("values") or []]
            + [i["name"] for i in t["items"] or []]
            for k in value_keys(name)
        }
        t["male_value"], t["female_value"] = bool(named & MALE_VALUES), bool(named & FEMALE_VALUES)
        t["age_cov"] = age_coverage(
            t["name_n"], t["class_n"], t["meta_n"], t.get("table_name") or ""
        )
        t["words"] = word_set(
            [t["table_name"]]
            + [c["name"] for c in classes]
            + [v for c in classes for v in rank_values(c) if v]
            + [i["name"] for i in t["items"] or []]
        )
        tables.append(t)
    _set_family_last_year(tables)
    return tables


RAW_AGE_RANGE = re.compile(r"(\d+)\s*세?\s*[~\-]\s*(?:만\s*)?(\d+)\s*세")


OUR_PLACE = re.compile(r"우리\s*(?:구|군|시|동네)|동네")
REGION_ASK = "어느 지역인지 알려 주세요"
SIDO_PREFIX = re.compile(
    r"(서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|세종특별자치시|경기도|강원(?:특별자치)?도|"
    r"충청북도|충청남도|전라북도|전북특별자치도|전라남도|경상북도|경상남도|제주특별자치도)\s*"
)


def age_coverage(
    name_n: str, class_n: list[str], meta_n: str, raw_name: str = ""
) -> tuple[int, int | None] | None:
    if m := RAW_AGE_RANGE.search(raw_name):
        return (int(m[1]), int(m[2]))
    text = name_n + " " + " ".join(class_n)
    if m := re.search(r"(\d+)세이상", text):
        return (int(m[1]), None)
    if m := re.search(r"(\d+)[~\-](\d+)세", text):
        return (int(m[1]), int(m[2]))
    if "연령" in text or "각세" in text or "연령" in meta_n:
        return (0, None)
    if "고령자" in text or "고령인구" in text or "노인" in text:
        return (65, None)
    return None


PERIODS_SQL = """
SELECT s.tbl_id, s.start_year, s.end_year, f.edition
FROM catalog.table_meta s JOIN catalog.table_families f USING (tbl_id)
"""
_periods_stale = False


def mark_periods_stale() -> None:
    global _periods_stale
    _periods_stale = True


async def _check_reloaded() -> None:
    from app.services import lookup

    try:
        await asyncio.to_thread(lookup.refresh_if_reloaded)
    except Exception:
        logging.getLogger(__name__).warning("표 적재 상태 확인 실패", exc_info=True)


async def _refresh_periods(tables: list[dict[str, Any]]) -> None:
    from app.core.db import engine

    async with engine.connect() as conn:
        rows = (await conn.execute(text(PERIODS_SQL))).mappings().all()
    got = {r["tbl_id"]: r for r in rows}
    for t in tables:
        if (r := got.get(t["tbl_id"])) is not None:
            t["start_year"], t["end_year"] = r["start_year"], r["end_year"]
            t["edition"] = r["edition"]
    _set_family_last_year(tables)


async def load_tables() -> list[dict[str, Any]]:
    global _tables, _vectors, _periods_stale
    await _check_reloaded()
    if not _tables:
        _periods_stale = False
        from app.core.db import engine

        async with engine.connect() as conn:
            rows = (await conn.execute(text(TABLES_SQL))).mappings().all()
            tables = prepare([dict(r) for r in rows])
            _vectors = await vector_search.load_matrix(conn, [t["tbl_id"] for t in tables])
        if _vectors is not None:
            vector_search.load_adapter([t["tbl_id"] for t in tables])
        _tables = tables
    elif _periods_stale:
        _periods_stale = False
        try:
            await _refresh_periods(_tables)
        except Exception:
            _periods_stale = True
            logging.getLogger(__name__).warning("수록 기간 다시 읽기 실패", exc_info=True)
    return _tables


async def warm_up() -> None:
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
    return {
        "tables": len(_tables or []),
        "vectors": _vectors is not None,
        "embedding_backend": vector_search.get_settings().embedding_backend,
        "retrieval": "hybrid" if _vectors is not None else "keyword",
        "adapter": vector_search.adapter_loaded(),
        "morph": synonyms.morph_backend(),
        "search_llm": search_llm.provider(),
        "search_llm_fallback": search_llm.falling_back(),
    }


async def dense_scores(question: str) -> list[float] | None:
    await load_tables()
    if _vectors is None:
        return None
    query = await vector_search.embed_query(question)
    return None if query is None else vector_search.scores(_vectors, query)


def keyword_query(slots: dict[str, Any]) -> str:
    question = slots.get("question") or slots["topic"]
    question = timeref.RECENT_N_YEARS.sub(" ", question)
    return synonyms.keyword_text(question, exclude=slots.get("exclude") or None)


def query_tokens(slots: dict[str, Any], text: str | None = None) -> list[dict[str, Any]]:
    words = re.findall(r"[0-9A-Za-z가-힣]+", keyword_query(slots) if text is None else text)
    values = norm(" ".join(slots["conditions"]["items"]))
    excluded = {v for w in slots.get("exclude") or [] for v in variants(w)}
    tokens = []
    for w in dict.fromkeys(words):
        if w in STOPWORDS or excluded & set(variants(w)):
            continue
        tokens.append({"variants": variants(w), "value": bool(values) and w in values})
    return tokens


def variants(word: str) -> list[str]:
    if len(word) >= 3 and word.endswith(SUFFIXES):
        return [word, word[:-1]]
    return [word]


def _found(variants: list[str], field: str) -> bool:
    return any(v in field for v in variants)


_value_index: dict[str, set[str]] | None = None
_value_index_for: list[dict[str, Any]] | None = None


def value_index(tables: list[dict[str, Any]]) -> dict[str, set[str]]:
    global _value_index, _value_index_for
    if _value_index is None or _value_index_for is not tables:
        idx: dict[str, set[str]] = {}
        for t in tables:
            for c in t.get("classifications") or []:
                key = f"{t['tbl_id']}|{c.get('id') or c.get('name')}"
                for v in rank_values(c):
                    if v and (nv := norm(v)):
                        idx.setdefault(nv, set()).add(key)
        _value_index, _value_index_for = idx, tables
    return _value_index


VALUE_LIKE_MIN = 20
VALUE_LIKE_RATIO = 5
_value_like: dict[str, bool] = {}
_value_like_for: list[dict[str, Any]] | None = None


def value_like(tables: list[dict[str, Any]], word: str) -> bool:
    global _value_like_for
    if _value_like_for is not tables:
        _value_like.clear()
        _value_like_for = tables
    vs = variants(word)
    key = vs[-1]
    if key not in _value_like:
        in_name = sum(_found(vs, t["name_n"]) for t in tables)
        only_value = sum(_found(vs, t["meta_n"]) and not _found(vs, t["name_n"]) for t in tables)
        _value_like[key] = only_value >= max(VALUE_LIKE_MIN, VALUE_LIKE_RATIO * in_name)
    return _value_like[key]


def contrast_words(tables: list[dict[str, Any]], slots: dict[str, Any]) -> set[str]:
    idx = value_index(tables)
    words = re.findall(r"[0-9A-Za-z가-힣]+", slots.get("question") or "")
    out = set()
    for w in slots.get("exclude") or []:
        keys = set().union(*(idx.get(v, set()) for v in variants(w)))
        if not keys or not value_like(tables, w):
            continue
        for o in words:
            if o == w or set(variants(o)) & set(variants(w)) or len(o) < 2:
                continue
            if any(keys & idx.get(v, set()) for v in variants(o)):
                out.add(w)
                break
    return out


def _in_any(tables: list[dict[str, Any]], vs: list[str]) -> bool:
    return any(
        _found(vs, t["name_n"]) or _found(vs, t["path_n"]) or _found(vs, t["meta_n"])
        for t in tables
    )


def split_compounds(
    tables: list[dict[str, Any]], tokens: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    idx = value_index(tables)
    have = {v for t in tokens for v in t["variants"]}
    out = list(tokens)
    for token in tokens:
        word = token["variants"][0]
        if len(word) < 4:
            continue
        for cut in range(len(word) - 2, 1, -1):
            head, rest = word[:cut], word[cut:]
            if norm(head) in idx and _in_any(tables, variants(rest)):
                if head not in have:
                    out.append({"variants": [head], "value": True})
                    have.add(head)
                break
    return out


def keyword_scores(tables: list[dict[str, Any]], tokens: list[dict[str, Any]]) -> list[float]:
    tokens = split_compounds(tables, tokens)
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


CLASS_SAID = {
    "연령": re.compile(r"나이|연령|살|세|\d+\s*대|어린|노인|청년|청소년|고령|아동|유아|미성년"),
    "성별": re.compile(r"남|여|성별"),
    "교육정도": re.compile(r"학력|교육|졸업|학교|대학|대졸|고졸|나온"),
}


def ground_conditions(slots: dict[str, Any]) -> dict[str, Any]:
    c = slots.get("conditions") or {}
    if not (c.get("classes") or c.get("items")):
        return slots
    question = slots.get("question") or ""
    said = norm(question)
    text = norm(keyword_query(slots))

    age_said = bool(CLASS_SAID["연령"].search(question)) or agegroup.span(question) is not None

    def seen(w: str) -> bool:
        return w in said or w in text or stem(w) in said

    def grounded(word: str, kind: str) -> bool:
        key = norm(word).removesuffix("별")
        if not key:
            return False
        if seen(key):
            return True
        parts = [norm(w).removesuffix("별") for w in re.split(r"[\s·/,_]+", word)]
        parts = [w for w in parts if len(w) >= 2]
        if len(parts) > 1 and all(seen(w) for w in parts):
            return True
        if kind == "class":
            axis = next(
                (a for k in (norm(word), key) if (a := CLASS_ALIAS.get(k, k)) in CLASS_SAID), key
            )
            if axis == "연령" and age_said:
                return True
            if (pat := CLASS_SAID.get(axis)) and pat.search(question):
                return True
        return age_said and (
            agegroup.span(word) is not None or bool(re.search(r"\d+\s*(세|살|대)", word))
        )

    dropped = []
    for kind, field in (("class", "classes"), ("item", "items")):
        keep = []
        for w in c.get(field) or []:
            (keep if grounded(w, kind) else dropped).append(w)
        c[field] = keep
    if dropped:
        slots["dropped_conditions"] = dropped
    return slots


def check(t: dict[str, Any], slots: dict[str, Any]) -> dict[str, bool]:
    c = slots["conditions"]
    out: dict[str, bool] = {}
    if c["year"] is not None:
        last = c["year_end"] or c["year"]
        if (ref := slots.get("time_ref")) and ref.get("open_end") and c["year_end"] is None:
            last = date.today().year
        out["year"] = t["start_year"] <= c["year"] and last <= t["end_year"]
        if not out["year"] and slots.get("time_ref") and last > t["end_year"]:
            out["year"] = t["start_year"] <= c["year"] and last - t["end_year"] <= PUBLISH_LAG
        if (
            not out["year"]
            and (ref := slots.get("time_ref"))
            and ref.get("open_end")
            and c["year_end"] is None
            and t.get("end_year") is not None
            and t["end_year"] >= (t.get("family_last_year") or t["end_year"])
            and date.today().year - t["end_year"] <= CENSUS_CYCLE + PUBLISH_LAG
        ):
            out["year"] = True
            out["period"] = t["start_year"] <= c["year"] <= t["end_year"]
    if c["region_level"]:
        levels = set(t["region_levels"])
        if MIGRATION.search(slots["topic"]):
            levels |= set(t["prev_region_levels"])
        out["region_level"] = c["region_level"] in levels
        if not out["region_level"] and c.get("region"):
            short = norm(REGION_SHORT.get(c["region"], c["region"]))
            out["region_level"] = bool(short) and (
                short in t["name_n"] or short in norm(t["survey_name"]) or short in t["path_n"]
            )
    sex = c["sex"]
    if sex in ("남", "여"):
        own = "male_only" if sex == "남" else "female_only"
        out["sex"] = (
            t["sex_coverage"] in ("both", own)
            or t["sex"] == sex
            or (sex == "여" and bool(FEMALE_ONLY_TABLE.search(t["name_n"])))
            or has_value(t, "남자" if sex == "남" else "여자")
            or bool(t.get("male_value" if sex == "남" else "female_value"))
        )
    elif sex == "전체" and t["sex_coverage"] != "none":
        out["sex"] = t["has_total"] is not False
    for cls in c["classes"]:
        if cls == "성별":
            ok = t["sex_coverage"] == "both"
        elif cls == "가구주 성별":
            ok = any("가구주" in n and "성" in n for n in t["class_n"])
        else:
            key = CLASS_ALIAS.get(norm(cls).removesuffix("별"), norm(cls).removesuffix("별"))
            ok = any(key in n for n in t["class_n"]) or key in t["name_n"] or has_value(t, key)
            if key == "연령" and not ok:
                ok = any("각세" in n for n in t["class_n"]) or "각세" in t["name_n"]
            if key == "연령" and ok and not has_age_axis(t):
                ok = False
        if cls == "성별" and not ok:
            ok = (has_value(t, "남자") and has_value(t, "여자")) or bool(
                t.get("male_value") and t.get("female_value")
            )
        out[f"class:{cls}"] = ok
    question = slots.get("question") or ""
    for item in c["items"]:
        key = norm(item)
        if counted := agegroup.count_words(item, question):
            ok = all(
                w in t["name_n"] or has_value(t, w) or stem(w) in t.get("words", ())
                for w in counted
            )
        elif agegroup.count_digits(item).strip().isdigit():
            ok = False
        else:
            ok = has_value(t, key) or key in t["name_n"]
        if not ok and not counted:
            ok = stem(key) in t.get("words", ())
        if not ok and (y := YEAR_ITEM.match(key)):
            ok = has_year_value(t, y[1])
        if not ok and (a := agegroup.span(item)) and t["age_cov"] is not None:
            lo, hi = t["age_cov"]
            rest = norm(item.replace(a.expr, " "))
            for w in ("인구", "이상", "층"):
                rest = rest.replace(w, "")
            rest_ok = (
                not rest
                or rest in t["name_n"]
                or has_value(t, rest)
                or stem(rest) in t.get("words", ())
            )
            ok = rest_ok and lo <= a.lo and (hi is None or (a.hi is not None and a.hi <= hi))
        out[f"item:{item}"] = ok
    if (age := slots.get("age")) and t["age_cov"] is not None:
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
    if slots.get("target") and slots["target"] != "전체":
        key = norm(slots["target"])
        out["target"] = (
            key in t["name_n"]
            or has_value(t, key)
            or key in norm(t["target"] or "")
            or key in t["path_n"]
        )
    for k, ok in out.items():
        kind, _, word = k.partition(":")
        if not ok and kind in ("item", "class") and by_dictionary(t, word):
            out[k] = True
    return out


DICT_TERM_MIN = 3
_dict_terms: dict[str, tuple[str, ...]] = {}
_dict_index: dict[str, str] = {}


def dict_terms(word: str) -> tuple[str, ...]:
    if word not in _dict_terms:
        if not _dict_index:
            _dict_index.update({norm(k): v for k, v in synonyms.load().items()})
        own = norm(word)
        extra = (_dict_index.get(own) or _dict_index.get(own.removesuffix("별")) or "").split()
        keys = {norm(x).removesuffix("별") for x in extra}
        _dict_terms[word] = tuple(
            sorted(k for k in keys if len(k) >= DICT_TERM_MIN and k not in own)
        )
    return _dict_terms[word]


def by_dictionary(t: dict[str, Any], word: str) -> bool:
    return any(
        k in t["name_n"] or any(k in n for n in t["class_n"]) or has_value(t, k)
        for k in dict_terms(word)
    )


def bigrams(value: str) -> set[str]:
    return {value[i : i + 2] for i in range(len(value) - 1)}


def name_similarity(query: str, name: str) -> float:
    a, b = bigrams(query), bigrams(name)
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def relevance(keyword: float, name_sim: float, dense: float | None) -> float:
    lexical = keyword + NAME_WEIGHT * name_sim
    if dense is None:
        return lexical
    return dense + (KEYWORD_WEIGHT * lexical if keyword >= KEYWORD_GATE else 0.0)


def wants_current(slots: dict[str, Any]) -> bool:
    c = slots["conditions"]
    if (ref := slots.get("time_ref")) and ref.get("open_end") and c["year_end"] is None:
        return True
    return (
        c["year"] is None and c["year_end"] is None and not PAST.search(slots.get("question", ""))
    )


def recency_bonus(t: dict[str, Any], slots: dict[str, Any]) -> float:
    if not slots.get("projection") and PROJECTION_TABLE.search(t["survey_name"] + t["name_n"]):
        return 0.0
    long_span = t["end_year"] - t["start_year"] >= LONG_SPAN_YEARS
    stale = not long_span and t["end_year"] <= STALE_END_YEAR
    return (
        RECENT_BONUS * (t["end_year"] >= RECENT_END_YEAR)
        + LONG_SPAN_BONUS * long_span
        - STALE_PENALTY * stale
    )


CYCLE_BONUS = 0.08
CYCLE_MONTH = re.compile(
    r"월별|월간|매달|매월|달마다|다달이|지난\s*달|이번\s*달|저번\s*달|전월"
    r"|(?<!\d)(?:1[0-2]|[1-9])\s*월(?!\s*\d{1,2}\s*일)(?!\s*(?:말|초)?\s*기준)"
)
CYCLE_QUARTER = re.compile(r"분기")
CYCLE_HALF = re.compile(r"[상하]반기|반기별")
TABLE_CYCLE = re.compile(r"(?:^|,\s*)(월|분기|반기)\s*\(")


def asked_cycle(question: str) -> str | None:
    if CYCLE_QUARTER.search(question):
        return "분기"
    if CYCLE_HALF.search(question):
        return "반기"
    if CYCLE_MONTH.search(question):
        return "월"
    return None


def cycle_bonus(t: dict[str, Any], want: str) -> float:
    have = set(TABLE_CYCLE.findall(t.get("period") or ""))
    if want == "월":
        return CYCLE_BONUS if "월" in have else 0.0
    if want == "분기":
        return CYCLE_BONUS if "분기" in have else (CYCLE_BONUS / 2 if "월" in have else 0.0)
    return CYCLE_BONUS if have else 0.0


def _population_unmentioned(t: dict[str, Any], said_n: str) -> bool:
    words = SURVEY_POPULATION.get(t["survey_name"])
    return bool(words) and not any(norm(w) in said_n for w in words)


def unasked_modifiers(t: dict[str, Any], said: str) -> list[str]:
    return [
        m for m, asked in SUBSET_MODIFIERS.items() if m in t["name_n"] and not asked.search(said)
    ]


def rank(
    tables: list[dict[str, Any]], slots: dict[str, Any], dense: list[float] | None = None
) -> list[dict[str, Any]]:
    if slots.get("exclude"):
        contrast = contrast_words(tables, slots)
        if contrast:
            slots = {**slots, "exclude": [w for w in slots["exclude"] if w not in contrast]}
    text = keyword_query(slots)
    tokens = query_tokens(slots, text)
    query = norm(text)
    said = f"{slots.get('question', '')} {slots['topic']} {slots.get('target') or ''}"
    keywords = keyword_scores(tables, tokens)
    if dense is not None:
        top = max(keywords, default=0.0) or 1.0
        keywords = [k / top for k in keywords]
    cut = sorted(dense, reverse=True)[min(DENSE_TOP_K, len(dense)) - 1] if dense else None
    timeless = dense is not None and wants_current(slots)
    cycle = asked_cycle(slots.get("question") or "") if dense is not None else None
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
            + (cycle_bonus(t, cycle) if cycle else 0.0)
        )
        ranked.append({"table": t, "keyword": kw, "dense": d, "checks": checks, "score": score})
    ranked.sort(key=lambda r: (-r["score"], r["table"]["edition_order"], r["table"]["tbl_id"]))
    return ranked


def fold(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for r in ranked:
        seen.setdefault(r["table"]["family_id"], r)
    return list(seen.values())


def family_axes(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    def options(key: str) -> list[str]:
        values = {r[key] for r in rows if r[key] is not None}
        return sorted(values, key=lambda v: (v != "전체" and v != "계", v))

    axes = {"target": options("target"), "sex": options("sex")}
    editions = sorted({(-r["end_year"], r["start_year"], r["edition"]) for r in rows})
    axes["edition"] = list(dict.fromkeys(e for _, _, e in editions))
    return {k: v for k, v in axes.items() if len(v) > 1}


def choose(
    family: list[dict[str, Any]], slots: dict[str, Any]
) -> tuple[dict[str, Any], list[str], list[str]]:
    c = slots["conditions"]
    picked = [r for r in family if r["tbl_id"] == slots.get("tbl_id")]
    if picked:
        return picked[0], [], []
    axes = family_axes(family)
    missing = []
    rows = family

    if "target" in axes:
        raw = f"{slots.get('question', '')} {slots.get('target') or ''}"
        said = norm(f"{synonyms.keyword_text(raw)} {raw} {c.get('region') or ''}")
        named = set(mentioned_targets([r["target"] for r in rows], said))
        excluded = [v for w in slots.get("exclude") or [] for v in variants(w)]
        named = {n for n in named if not (n and any(v in norm(n) for v in excluded))}
        match = [r for r in rows if r["target"] in named]
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
    if c.get("region_level"):
        rows = [r for r in rows if c["region_level"] in (r.get("region_levels") or [])] or rows

    notes = []
    year = c["year"]
    if year is None:
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
    result: dict[str, Any] = {"status": None, "reason": None, "slots": slots, "table": None}
    if slots["query_type"] == "not_statistics" and scope_by_llm:
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
        in_scope = bool(families) and (scope_by_llm or families[0]["score"] >= MIN_SCORE)
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
    others = [_family_brief(f) for f in families[1:TOP_FAMILIES]]
    result["other_families"] = others
    if (
        allow_clarify
        and not slots["conditions"].get("region")
        and OUR_PLACE.search(slots.get("question") or "")
        and (sido := SIDO_PREFIX.match(top["table"]["table_name"]))
    ):
        shape = top["table"]["table_name"][sido.end() :].strip()
        same = [
            f
            for f in families[:30]
            if (m := SIDO_PREFIX.match(f["table"]["table_name"]))
            and f["table"]["table_name"][m.end() :].strip() == shape
        ]
        if len(same) > 1:
            result["options"] = [
                {"family_id": f["table"]["family_id"], "family_name": f["table"]["family_name"],
                 "score": round(f["score"], 3), **_brief(f["table"])}
                for f in same[:17]
            ]  # fmt: skip
            return {**result, "status": "ambiguous", "reason": REGION_ASK}
    if len(families) > 1 and top["score"] - families[1]["score"] < gap:
        # 점수가 거의 같은 묶음은 사용자에게 거기서 거기로 보여 되묻지 않는다 (11/26).
        # 조건 ✘가 적고 ✔가 많은 묶음을 고르고, 같으면 점수 순 (= 원래 1위)
        close = [f for f in families[:TOP_FAMILIES] if top["score"] - f["score"] < gap]
        best = max(close, key=_condition_fit)
        result["close_families"] = [
            {"family_id": f["table"]["family_id"], "family_name": f["table"]["family_name"],
             "score": round(f["score"], 3), "checks": f["checks"]}
            for f in close
        ]  # fmt: skip
        if best is not top:
            chosen = best["table"]["family_id"]
            rest = [_family_brief(top), *others]
            result["other_families"] = [o for o in rest if o["family_id"] != chosen]
        top = best

    return finish(result, tables, top["table"]["family_id"], top["score"], allow_clarify)


def _condition_fit(f: dict[str, Any]) -> tuple[int, int, float]:
    checks = f.get("checks") or {}
    failed = sum(1 for v in checks.values() if v is False)
    passed = sum(1 for v in checks.values() if v is True)
    return -failed, passed, f["score"]


def _family_brief(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "family_id": f["table"]["family_id"],
        "family_name": f["table"]["family_name"],
        "score": round(f["score"], 3),
        "checks": f["checks"],
        **_brief(f["table"]),
    }


def confirmed_guesses(
    t: dict[str, Any], slots: dict[str, Any], checks: dict[str, bool]
) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for word in slots.get("dropped_conditions") or []:
        for field, kind in (("items", "item"), ("classes", "class")):
            key = f"{kind}:{word}"
            if key in checks or key in out:
                break
            probe = {**slots, "conditions": {**slots["conditions"], "items": [], "classes": []}}
            probe["conditions"][field] = [word]
            if check(t, probe).get(key):
                out[key] = True
                break
    return out


def resolve(
    tables: list[dict[str, Any]],
    slots: dict[str, Any],
    family_id: str,
    allow_clarify: bool = True,
) -> dict[str, Any]:
    result = {"status": None, "reason": None, "slots": slots, "table": None, "candidates": []}
    return finish(result, tables, family_id, None, allow_clarify)


def clarify_axes(
    family: list[dict[str, Any]], slots: dict[str, Any], missing: list[str]
) -> list[dict[str, Any]]:
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
    checks.update(confirmed_guesses(table, slots, checks))
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
    trace = {} if trace is None else trace
    start = time.perf_counter()

    async def timed(coro: Any) -> tuple[Any, float]:
        value = await coro
        return value, time.perf_counter()

    (slots, parsed_at), (dense, embedded_at) = await asyncio.gather(
        timed(slot_parser.extract(question, trace)), timed(dense_scores(question))
    )
    rewrite = (slots.get("search_query") or "").strip()
    trace["embedding"] = {"question": dense is not None, "rewrite": None}
    if dense is not None and rewrite and rewrite != question.strip():
        dense2, embedded_at = await timed(dense_scores(rewrite))
        trace["embedding"]["rewrite"] = dense2 is not None
        if dense2 is not None:
            dense = [(a + b) / 2 for a, b in zip(dense, dense2, strict=True)]
    ground_conditions(slots)
    ready = max(parsed_at, embedded_at)
    tables = await load_tables()
    result = find(
        tables, slots, allow_clarify, dense, scope_by_llm=vector_search.get_settings().llm_pick
    )
    searched = time.perf_counter()
    from app.services import llm_pick

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
