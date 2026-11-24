import json
import logging
import re
import sys
from functools import cache
from pathlib import Path

from app.services import agegroup

SYNONYMS_PATH = Path(__file__).resolve().parents[2] / "config" / "synonyms.json"

FILLER = re.compile(
    r"(?:제일|가장)\s*(?:최신|최근)|최신(?:\s*(?:데이터|자료|버전|기준))?|최근(?!\s*\d)|"
    r"(?:통계표|통계|자료|데이터|현황)(?:으로|로|를|을|는|은|가|이|도)?(?![가-힣])|통계표|통계|자료|데이터|현황|찾아\s*줘|찾아\s*주세요|알려\s*줘|알려\s*주세요|알고\s*싶어|"
    r"보여\s*줘|볼\s*수\s*있는|있는\s*표|(?<![가-힣])표(?![가-힣])|"
    r"얼마나|몇\s*명|몇|되나요|하나요|인지|는지|어때|궁금|사람들|사람|퍼센트|비율이|정도|"
    r"어떻게|어디|어느|어떤|제일|가장|보통|요즘|경우|우리|되나|있나|되는|있는|"
    r"다른지|많은지|늘어나는지|하는지|싶어|해마다|1년에|얼마|쯤|나라"
)


@cache
def load(path: Path = SYNONYMS_PATH) -> dict[str, str]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


try:
    from kiwipiepy import Kiwi

    _KIWI = Kiwi()
except Exception as e:  # noqa: BLE001  설치 안 됐으면 정규식만
    _KIWI = None
    logging.getLogger(__name__).warning(
        "Kiwi 형태소 분석기를 쓸 수 없어 검색어에서 조사를 떼지 못합니다 (%s: %s). "
        "uv sync로 kiwipiepy를 설치하세요",
        type(e).__name__,
        e,
    )


def morph_backend() -> str:
    return "kiwi" if _KIWI is not None else "regex"


def tokenize(text: str) -> list:
    return _KIWI.tokenize(text) if _KIWI is not None else []


KIWI_DROP = {"MAG", "MAJ", "MM", "IC", "VX", "NNB"}
KIWI_NOUNISH = ("NN", "NR", "NP", "XR", "SN", "SL", "SH", "XP")


def kiwi_clean(text: str) -> str:
    if _KIWI is None:
        return text
    toks = _KIWI.tokenize(text)
    out = []
    for m in re.finditer(r"\S+", text):
        ws, we = m.span()
        mine = [t for t in toks if ws <= t.start < we]
        content = [
            t
            for t in mine
            if not (t.tag.startswith(("J", "E", "S")) and t.tag not in ("SN", "SL", "SH"))
            and t.tag != "VCP"
        ]
        if not content or all(t.tag in KIWI_DROP for t in content):
            continue
        if not mine[0].tag.startswith(KIWI_NOUNISH):
            continue
        out.append(text[ws : max(t.start + t.len for t in content)])
    return " ".join(out)


def clean_query(question: str) -> str:
    words = FILLER.sub(" ", kiwi_clean(question)).split()
    return " ".join(w for w in words if (len(w) > 1 or w.isdigit()) and w != "1년")


NEGATION_MARKS = ("아닌", "아니고", "아니라", "말고", "빼고", "제외", "않은", "안 ")
FERTILITY = re.compile(r"출산율|출산률")


def expand(
    q: str,
    table: dict[str, str] | None = None,
    exclude: list[str] | None = None,
    source: str | None = None,
) -> str:
    table = load() if table is None else table
    rest = q if source is None else source
    extra: list[str] = []
    for phrase in sorted(table, key=len, reverse=True):
        if phrase in rest:
            if (
                exclude
                and not any(m in phrase for m in NEGATION_MARKS)
                and any(phrase in w or w in phrase for w in exclude)
            ):
                rest = rest.replace(phrase, " ")
                continue
            extra.append(table[phrase])
            rest = rest.replace(phrase, " ")
    terms = list(dict.fromkeys(tok for e in extra for tok in e.split()))
    return q + (" " + " ".join(terms) if terms else "")


def keyword_text(
    question: str, table: dict[str, str] | None = None, exclude: list[str] | None = None
) -> str:
    question = agegroup.native_to_digits(question)
    table = load() if table is None else table
    if agegroup.said(question):
        table = {k: "연령별 출산율" if FERTILITY.fullmatch(k) else v for k, v in table.items()}
    return expand(clean_query(question), table, exclude, source=question)


if __name__ == "__main__":
    for q in sys.argv[1:] or ["결혼 안 하고 낳은 아기가 1년에 몇 명인지"]:
        print(f"{q}\n  → {keyword_text(q)}")
