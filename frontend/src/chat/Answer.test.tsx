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

it("수치 계산 결과에는 보고서 보기를 표시하고 재사용 응답에서는 생략한다", () => {
  const data: AskResponse = {
    status: "ok", answer: "인구는 10명입니다.", parsed: {}, sources: [],
    result: { status: "ok", value: 10 },
  };
  expect(render(data)).toContain("보고서 보기");
  expect(render({ ...data, result: { status: "ok", reused: true } })).not.toContain("보고서 보기");
});
