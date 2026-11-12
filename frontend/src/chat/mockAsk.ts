// 목업 API: POST /ask 응답과 같은 형식을 서버·HCX 호출 없이 돌려준다 (화면 개발용)
// 아래 답은 2026-11-12 로컬 서버가 실제로 돌려준 응답을 그대로 옮긴 것이다 (수치를 지어내지 않았다).
// 질문을 해석하지 않고 낱말로 고른다: 추이 / 비교 / 순위 / 구성 / 과거(연도 되묻기) / 그 밖(값 하나)
// 대화 이어 가기는 그림 바꾸기("막대그래프로", "표로")와 요약만 흉내 낸다. 조건 변경은 실제 서버로 본다.
// 수치로 답할 수 없는 질문(반려동물, 국적 취득, 외국인, 날씨 등)은 통계표 찾기 목업(mock.js)으로 답한다
import type { AskRequest, AskResponse, SearchResponse } from "../api/types";
import { MockApi } from "./mock.js";

const ANSWERS: Record<string, AskResponse> = {
  "trend": {
    "status": "ok",
    "answer": "서울특별시의 합계출산율은 2015년 가임여성 1명당 1.001명, 2024년 0.581명입니다. 2015년 대비 0.42명 감소했습니다(증감률 -41.96%). 합계출산율은 2023년까지 감소하다가 이후 증가했습니다.\n\n서울특별시 합계출산율 (계)\n- 2015년: 1.001 (가임여성 1명당 명)\n- 2016년: 0.94 (가임여성 1명당 명) (전년 대비 -0.061명)\n- 2017년: 0.836 (가임여성 1명당 명) (전년 대비 -0.104명)\n- 2018년: 0.761 (가임여성 1명당 명) (전년 대비 -0.075명)\n- 2019년: 0.717 (가임여성 1명당 명) (전년 대비 -0.044명)\n- 2020년: 0.642 (가임여성 1명당 명) (전년 대비 -0.075명)\n- 2021년: 0.626 (가임여성 1명당 명) (전년 대비 -0.016명)\n- 2022년: 0.593 (가임여성 1명당 명) (전년 대비 -0.033명)\n- 2023년: 0.552 (가임여성 1명당 명) (전년 대비 -0.041명)\n- 2024년: 0.581 (가임여성 1명당 명) (전년 대비 +0.029명)\n- 2015년 → 2024년 전체 변화: -0.42명, -41.96%\n※ 계산식: KOSIS 공표 값 (시도/합계출산율, 모의 연령별 출산율)\n출처: 국가데이터처 (구 통계청), 시도/합계출산율, 모의 연령별 출산율 (DT_1B81A21) (인구동향조사 출생통계, 각 연도 1월 1일 ~ 12월 31일)\n통계표: https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B81A21",
    "parsed": {},
    "result": {},
    "sources": [
      {
        "name": "국가데이터처 (구 통계청), 시도/합계출산율, 모의 연령별 출산율 (DT_1B81A21)",
        "basis": "인구동향조사 출생통계, 각 연도 1월 1일 ~ 12월 31일",
        "tbl_id": "DT_1B81A21",
        "url": "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B81A21"
      }
    ],
    "chart": {
      "type": "line",
      "title": "서울특별시 합계출산율 2015~2024년",
      "unit": "가임여성 1명당 명",
      "category": "연도",
      "rows": [
        {
          "label": "2015",
          "value": 1.001,
          "count": null
        },
        {
          "label": "2016",
          "value": 0.94,
          "count": null
        },
        {
          "label": "2017",
          "value": 0.836,
          "count": null
        },
        {
          "label": "2018",
          "value": 0.761,
          "count": null
        },
        {
          "label": "2019",
          "value": 0.717,
          "count": null
        },
        {
          "label": "2020",
          "value": 0.642,
          "count": null
        },
        {
          "label": "2021",
          "value": 0.626,
          "count": null
        },
        {
          "label": "2022",
          "value": 0.593,
          "count": null
        },
        {
          "label": "2023",
          "value": 0.552,
          "count": null
        },
        {
          "label": "2024",
          "value": 0.581,
          "count": null
        }
      ],
      "count_unit": null,
      "note": null
    },
    "session_id": null,
    "question": "2015년부터 2024년까지 서울 합계출산율 추이"
  },
  "compare": {
    "status": "ok",
    "answer": "2024년 고령인구 비율이 가장 높은 지역은 부산광역시(23.9%), 가장 낮은 지역은 서울특별시(19.44%)이며, 차이는 4.46%p입니다.\n\n2024년 고령인구 비율 (계, 높은 순)\n- 부산광역시: 23.9%\n- 대구광역시: 20.87%\n- 서울특별시: 19.44%\n- 가장 높은 부산광역시와 가장 낮은 서울특별시의 차이: 4.46%p\n※ 계산식: 65세 이상 인구 ÷ 총인구 × 100\n출처: 국가데이터처 (구 통계청), 행정구역(시군구)별/1세별 주민등록인구 (DT_1B04006) (주민등록인구, 각 연도 12월 31일 기준)\n통계표: https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B04006",
    "parsed": {},
    "result": {},
    "sources": [
      {
        "name": "국가데이터처 (구 통계청), 행정구역(시군구)별/1세별 주민등록인구 (DT_1B04006)",
        "basis": "주민등록인구, 각 연도 12월 31일 기준",
        "tbl_id": "DT_1B04006",
        "url": "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B04006"
      }
    ],
    "chart": {
      "type": "bar",
      "title": "2024년 고령인구 비율",
      "unit": "%",
      "category": "지역",
      "rows": [
        {
          "label": "부산광역시",
          "value": 23.9,
          "count": null
        },
        {
          "label": "대구광역시",
          "value": 20.87,
          "count": null
        },
        {
          "label": "서울특별시",
          "value": 19.44,
          "count": null
        }
      ],
      "count_unit": null,
      "note": null
    },
    "session_id": null,
    "question": "2024년 서울 부산 대구 고령인구 비율 비교"
  },
  "rank": {
    "status": "ok",
    "answer": "2024년 합계출산율이 가장 높은 시도 5곳은 세종특별자치시(1.028), 전라남도(1.028), 경상북도(0.897), 강원특별자치도(0.889), 충청남도(0.883)입니다.\n\n2024년 합계출산율 시도 순위 (높은 순, 전체 17곳 중)\n1. 세종특별자치시: 1.028 (가임여성 1명당 명)\n1. 전라남도: 1.028 (가임여성 1명당 명)\n3. 경상북도: 0.897 (가임여성 1명당 명)\n4. 강원특별자치도: 0.889 (가임여성 1명당 명)\n5. 충청남도: 0.883 (가임여성 1명당 명)\n※ 계산식: KOSIS 공표 값 (시도/합계출산율, 모의 연령별 출산율)\n출처: 국가데이터처 (구 통계청), 시도/합계출산율, 모의 연령별 출산율 (DT_1B81A21) (인구동향조사 출생통계, 각 연도 1월 1일 ~ 12월 31일)\n통계표: https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B81A21",
    "parsed": {},
    "result": {},
    "sources": [
      {
        "name": "국가데이터처 (구 통계청), 시도/합계출산율, 모의 연령별 출산율 (DT_1B81A21)",
        "basis": "인구동향조사 출생통계, 각 연도 1월 1일 ~ 12월 31일",
        "tbl_id": "DT_1B81A21",
        "url": "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B81A21"
      }
    ],
    "chart": {
      "type": "bar",
      "title": "2024년 합계출산율 (높은 순)",
      "unit": "가임여성 1명당 명",
      "category": "지역",
      "rows": [
        {
          "label": "세종특별자치시",
          "value": 1.028,
          "count": null
        },
        {
          "label": "전라남도",
          "value": 1.028,
          "count": null
        },
        {
          "label": "경상북도",
          "value": 0.897,
          "count": null
        },
        {
          "label": "강원특별자치도",
          "value": 0.889,
          "count": null
        },
        {
          "label": "충청남도",
          "value": 0.883,
          "count": null
        }
      ],
      "count_unit": null,
      "note": null
    },
    "session_id": null,
    "question": "2024년 합계출산율이 가장 높은 시도 5곳"
  },
  "parts": {
    "status": "ok",
    "answer": "2024년 서울특별시 인구 9,331,828명의 연령대별 구성은 유소년인구(0~14세) 8.96%, 생산연령인구(15~64세) 71.61%, 고령인구(65세 이상) 19.44%입니다. 가장 큰 비중은 생산연령인구(15~64세)입니다.\n\n서울특별시 2024년 인구 구성 (계, 전체 9,331,828명)\n- 유소년인구(0~14세): 835,816명 (8.96%)\n- 생산연령인구(15~64세): 6,682,364명 (71.61%)\n- 고령인구(65세 이상): 1,813,648명 (19.44%)\n※ 계산식: 항목별 인구 ÷ 총인구 × 100\n출처: 국가데이터처 (구 통계청), 행정구역(시군구)별/1세별 주민등록인구 (DT_1B04006) (주민등록인구, 각 연도 12월 31일 기준)\n통계표: https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B04006",
    "parsed": {},
    "result": {},
    "sources": [
      {
        "name": "국가데이터처 (구 통계청), 행정구역(시군구)별/1세별 주민등록인구 (DT_1B04006)",
        "basis": "주민등록인구, 각 연도 12월 31일 기준",
        "tbl_id": "DT_1B04006",
        "url": "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B04006"
      }
    ],
    "chart": {
      "type": "parts",
      "title": "2024년 서울특별시 인구 구성",
      "unit": "%",
      "category": "항목",
      "rows": [
        {
          "label": "유소년인구(0~14세)",
          "value": 8.96,
          "count": 835816.0
        },
        {
          "label": "생산연령인구(15~64세)",
          "value": 71.61,
          "count": 6682364.0
        },
        {
          "label": "고령인구(65세 이상)",
          "value": 19.44,
          "count": 1813648.0
        }
      ],
      "count_unit": "명",
      "note": null
    },
    "session_id": null,
    "question": "2024년 서울 인구 연령대별 구성비"
  },
  "value": {
    "status": "ok",
    "answer": "2024년 부산광역시의 고령인구 비율은 23.9%입니다.\n\n※ 계산식: 65세 이상 인구 ÷ 총인구 × 100\n출처: 국가데이터처 (구 통계청), 행정구역(시군구)별/1세별 주민등록인구 (DT_1B04006) (주민등록인구, 각 연도 12월 31일 기준)\n통계표: https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B04006",
    "parsed": {},
    "result": {},
    "sources": [
      {
        "name": "국가데이터처 (구 통계청), 행정구역(시군구)별/1세별 주민등록인구 (DT_1B04006)",
        "basis": "주민등록인구, 각 연도 12월 31일 기준",
        "tbl_id": "DT_1B04006",
        "url": "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B04006"
      }
    ],
    "chart": null,
    "session_id": null,
    "question": "2024년 부산 고령인구 비율 알려줘"
  },
  "refuse": {
    "status": "missing_year",
    "answer": "과거 어느 연도와 비교할지 질문에 없습니다. 미혼 비율 자료는 1970~2020년(5년마다), 2022~2025년이 있습니다. 비교할 연도를 알려 주세요 (예: 1970년과 2025년).",
    "parsed": {},
    "result": {},
    "sources": [],
    "chart": null,
    "session_id": null,
    "question": "과거와 최근 전국 미혼 비율 비교해줘"
  }
};

// 적용한 조건 (실제 서버는 app/services/ask_session.py 가 만든다)
const APPLIED: Record<string, AskResponse["applied"]> = {
  trend: [
    { name: "지표", value: "합계출산율", source: "user" },
    { name: "지역", value: "서울", source: "user" },
    { name: "기간", value: "2015~2024년", source: "user" },
    { name: "성별", value: "계", source: "default" },
  ],
  compare: [
    { name: "지표", value: "고령인구 비율", source: "user" },
    { name: "지역", value: "서울, 부산, 대구", source: "user" },
    { name: "기간", value: "2024년", source: "user" },
    { name: "성별", value: "계", source: "default" },
  ],
  rank: [
    { name: "지표", value: "합계출산율", source: "user" },
    { name: "지역", value: "전국 시도별", source: "default" },
    { name: "기간", value: "2024년", source: "user" },
    { name: "성별", value: "계", source: "default" },
  ],
  parts: [
    { name: "지표", value: "총인구", source: "user" },
    { name: "지역", value: "서울", source: "user" },
    { name: "기간", value: "2024년", source: "user" },
    { name: "성별", value: "계", source: "default" },
  ],
  value: [
    { name: "지표", value: "고령인구 비율", source: "user" },
    { name: "지역", value: "부산", source: "user" },
    { name: "기간", value: "2024년", source: "user" },
    { name: "성별", value: "계", source: "default" },
  ],
};

const VIEWS: [RegExp, "line" | "bar" | "table", string][] = [
  [/표\s*로/, "table", "표"],
  [/막대/, "bar", "막대그래프"],
  [/꺾은\s*선|선\s*그래프/, "line", "꺾은선 그래프"],
];

function pick(question: string): string {
  if (/추이|변화|부터/.test(question)) return "trend";
  if (/순위|가장|상위/.test(question)) return "rank";
  if (/구성|비중|나뉘/.test(question)) return "parts";
  if (/과거|예전/.test(question)) return "refuse";
  if (/비교|중에|와|과/.test(question)) return "compare";
  return "value";
}

let last: AskResponse | null = null; // 직전 답 ("막대그래프로", "요약해줘"는 이것으로 답한다)
let searchSession: string | null = null; // 통계표 찾기 목업의 대화 id
let waitingChoice = false; // 통계표 선택지를 물었다 (다음 말은 그 답)

// 서버의 table_guide.SEARCH_INSTEAD 처럼: 수치 지표가 아닌 질문은 통계표 찾기로
const TABLE_ONLY = /반려|강아지|고양이|집에서|병원 말고|국적|귀화|외국인|날씨|집값|안녕|1인/;

async function findTables(body: AskRequest): Promise<AskResponse> {
  const found = (await MockApi.search({
    question: body.question,
    session_id: searchSession,
    selections: body.selections ?? null,
    top_k: 3,
    debug: false,
  })) as SearchResponse;
  searchSession = found.session_id;
  waitingChoice = found.status === "clarify" || found.status === "ambiguous";
  const res: AskResponse = {
    status: "tables",
    answer: found.answer,
    parsed: {},
    result: { status: "tables" },
    sources: [],
    chart: null,
    tables: found,
    session_id: body.session_id ?? `M_${Date.now().toString(36)}`,
    request_type: body.selections ? "reply" : "new",
    applied: [],
  };
  last = null;
  return res;
}

export const MockAsk = {
  async ask(body: AskRequest): Promise<AskResponse> {
    await new Promise((r) => setTimeout(r, 350 + Math.random() * 400));
    const q = body.question;
    if (body.selections || (waitingChoice && body.session_id) || TABLE_ONLY.test(q)) {
      if (!body.session_id) searchSession = null; // 새 대화
      return findTables(body);
    }
    waitingChoice = false;
    const view = VIEWS.find(([pattern]) => pattern.test(q));
    if (last?.chart && body.session_id && view && q.length <= 25) {
      const drawable = view[1] === "table" || view[1] === last.chart.type || last.chart.type === "line";
      if (drawable) {
        return {
          ...structuredClone(last),
          answer: `${view[2]}로 바꿨습니다.`,
          chart: { ...structuredClone(last.chart), view: view[1] },
          request_type: "chart",
        };
      }
    }
    if (last && body.session_id && /^(한\s*줄로\s*)?(요약|정리)/.test(q.trim())) {
      return { ...structuredClone(last), answer: last.answer.split("\n\n")[0], request_type: "summary" };
    }
    const key = pick(q);
    const res = structuredClone(ANSWERS[key]);
    res.session_id = body.session_id ?? `M_${Date.now().toString(36)}`;
    res.request_type = "new";
    res.applied = res.status === "ok" ? (APPLIED[key] ?? []) : [];
    last = res;
    return res;
  },
};
