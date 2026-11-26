CELLS, CLASS, TABLE, NONE = "칸", "분류", "표 전체", "없음"

SYSTEM = """너는 통계표에서 질문의 조건에 맞는 칸을 고르는 도우미다.
코드가 질문의 조건을 표의 칸에 글자로 맞춰 이었는데, 일부 조건은 글자가 맞지 않아 잇지 못했다.
못 이은 조건마다 뜻이 같은 칸을 고른다.

조건마다 how 하나를 고른다.
- "칸": 뜻이 같은 칸이 있다. dim에 분류 번호, codes에 그 분류의 칸 코드를 쓴다.
  일상어와 칸 이름이 뜻으로 같으면 고른다. (예: 평균나이 = 평균연령, 부부 사이가 안 좋아서 = 부부 불화)
  한 조건이 여러 칸의 합이면 그 칸을 모두 쓴다.
- "분류": 'class=' 조건(무엇별로 나눠 보기)에 뜻이 같은 분류가 있다. dim에 그 분류 번호를 쓰고
  codes는 비운다. (예: 국적별 = 국가별)
- "표 전체": 표 이름이나 이미 정한 칸이 그 조건을 이미 담고 있다.
- "없음": 뜻이 같은 칸이 정말 없다. 뜻이 다른 칸으로 대신하지 않는다.

세는 단위를 질문과 맞춘다. '몇 명'은 사람 수 칸, '몇 가구'는 가구 수 칸, '몇 건'은 건수 칸이다.
단위가 다른 칸밖에 없으면 "없음"이다.
질문의 조건보다 넓은 칸만 있으면(예: 농업 → 농림어업) 그 칸이나 "표 전체"를 고르고 broader를 true로 한다.
뜻이 정확히 같으면 broader는 false다. 목록에 없는 코드는 쓰지 않는다. 값(숫자)은 추측하지 않는다."""


def schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "no": {"type": "integer"},
                        "how": {"type": "string", "enum": [CELLS, CLASS, TABLE, NONE]},
                        "dim": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                        "codes": {"type": "array", "items": {"type": "string"}},
                        "broader": {"type": "boolean"},
                        "why": {"type": "string"},
                    },
                    "required": ["no", "how", "dim", "codes", "broader", "why"],
                },
            }
        },
        "required": ["picks"],
    }
