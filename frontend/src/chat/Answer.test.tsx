import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { AskResponse } from "../api/types";
import { Answer } from "./Answer";

function render(data: AskResponse, busy = false) {
  return renderToStaticMarkup(
    <Answer data={data} busy={busy} onSend={vi.fn()} onOthers={vi.fn()} onAskAgain={vi.fn()} />,
  );
}

const base: AskResponse = {
  status: "clarify_question", answer: "이사 범위를확인할게요.", parsed: {}, result: {}, sources: [],
};

describe("질문 의미 확인", () => {
  it("이사 범위 선택지를 표시하고 처리 중이면 비활성화한다", () => {
    const data: AskResponse = {
      ...base,
      clarification: {
        kind: "move_scope", prompt: base.answer,
        options: ["지역 안에서의 이사도 포함", "지역 밖으로 나간 이사만"],
      },
    };
    const html = render(data, true);
    expect(html).toContain("지역 안에서의 이사도 포함");
    expect(html).toContain("지역 밖으로 나간 이사만");
    expect(html.match(/disabled=""/g)).toHaveLength(2);
  });

  it("지역 이름을 묻는 경우 임의의 선택지를 만들지 않는다", () => {
    const html = render({
      ...base, answer: "어느 지역인가요?",
      clarification: { kind: "local_region", prompt: "어느 지역인가요?", options: [] },
    });
    expect(html).toContain("어느 지역인가요?");
    expect(html).not.toContain("<button");
  });
});

it("답마다 보고서 버튼을 두지 않는다 (보고서는 대화창 [보고서]에서 답을 골라 만든다)", () => {
  const data: AskResponse = {
    status: "ok", answer: "인구는 10명입니다.", parsed: {}, sources: [],
    result: { status: "ok", value: 10 }, reportable: true, turn_id: 1,
  };
  expect(render(data)).not.toContain("보고서");
});

describe("직전 답을 다시 쓰는 응답", () => {
  const chart = { type: "line" as const, title: "출생아 수", unit: "명", category: "연도", rows: [] };
  const answer = "핵심 문장입니다.\n\n- 2020년: 1명\n출처: 통계청 (기준)";
  const data: AskResponse = {
    status: "ok", answer, parsed: {}, sources: [], chart, result: { status: "ok", reused: true },
  };

  it("더 자세히(detail)는 차트가 있어도 수치와 계산식을 펼친다", () => {
    expect(render(data)).not.toContain("open=\"\"");
    expect(render({ ...data, request_type: "detail" })).toContain("open=\"\"");
  });

  it("출처(source)는 말풍선의 통계표 주소를 링크로 만든다", () => {
    const html = render({
      ...data, chart: null, request_type: "source",
      answer: "직전 답은 아래 통계표의 수치로 만들었습니다.\n통계표: https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_X",
    });
    expect(html).toContain("href=\"https://kosis.kr/statHtml/statHtml.do?orgId=101&amp;tblId=DT_X\"");
    expect(html).not.toContain("수치와 계산식 보기");
  });

  it("인사(chat)는 말풍선만 보여 준다", () => {
    const html = render({ ...data, status: "chat", request_type: "chat", chart: null, answer: "네. 이어서 질문해 주세요." });
    expect(html).toContain("네. 이어서 질문해 주세요.");
    expect(html).not.toContain("보고서 보기");
  });
});
