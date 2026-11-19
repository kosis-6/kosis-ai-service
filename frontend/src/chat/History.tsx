// 지난 대화 목록 (로그인 유저만). 넓은 화면은 사이드바, 좁은 화면은 머리글 "기록" 버튼의 패널에 그린다
import { useState } from "react";
import type { ConversationRead } from "../api/types";
import { Icon } from "../components/Icon";
import { when } from "./conversations";

type Props = {
  items: ConversationRead[] | null; // null: 아직 불러오는 중이거나 불러오지 못함
  current: string | null; // 지금 보고 있는 대화
  onOpen: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
};

export function History({ items, current, onOpen, onDelete }: Props) {
  // 지우기는 한 번 더 묻는다 (브라우저 확인 창 대신 그 줄에서)
  const [asking, setAsking] = useState<string | null>(null);

  if (items === null) return <p className="history-empty">지난 대화를 불러오고 있어요</p>;
  if (items.length === 0) return <p className="history-empty">질문하면 대화가 여기에 남아요.</p>;
  return (
    <ul className="history-list">
      {items.map((c) =>
        asking === c.session_id ? (
          <li className="history-item asking" key={c.session_id}>
            <span className="history-ask">이 대화를 지울까요?</span>
            <button
              type="button"
              className="text-btn danger"
              onClick={() => {
                setAsking(null);
                onDelete(c.session_id);
              }}
            >
              지우기
            </button>
            <button type="button" className="text-btn" onClick={() => setAsking(null)}>
              취소
            </button>
          </li>
        ) : (
          <li className="history-item" key={c.session_id}>
            <button
              type="button"
              className="history-open"
              aria-current={c.session_id === current ? "page" : undefined}
              title={c.title}
              onClick={() => onOpen(c.session_id)}
            >
              <span className="history-title">{c.title}</span>
              <span className="history-when">{when(c.updated_at)}</span>
            </button>
            <button
              type="button"
              className="history-del"
              aria-label={`'${c.title}' 대화 지우기`}
              onClick={() => setAsking(c.session_id)}
            >
              <Icon name="x" size={14} />
            </button>
          </li>
        ),
      )}
    </ul>
  );
}
