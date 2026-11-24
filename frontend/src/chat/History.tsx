import { useState } from "react";
import type { ConversationRead } from "../api/types";
import { Icon } from "../components/Icon";
import { when } from "./conversations";

type Props = {
  items: ConversationRead[] | null;
  current: string | null;
  onOpen: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
};

export function History({ items, current, onOpen, onDelete }: Props) {
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
