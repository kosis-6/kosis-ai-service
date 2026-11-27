import { useEffect, useState } from "react";
import { Icon } from "../components/Icon";
import { askProgress } from "./api";

const POLL_MS = 700;

/** POST /ask 와 함께 보내는 진행 id. randomUUID 는 https · localhost 에서만 있어 없으면 시각+난수 */
export function newProgressId(): string {
  return (
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10).padEnd(8, "0")}`
  );
}

/** 답을 기다리는 동안 서버가 지금 하는 처리 단계를 보여 준다. 기다리는 동안만 화면에 있다 */
export function Waiting({ progressId, fallback }: { progressId: string | null; fallback: string }) {
  const [steps, setSteps] = useState<string[]>([]);
  const [seconds, setSeconds] = useState(0);

  useEffect(() => {
    if (!progressId) return;
    const started = Date.now();
    let stopped = false;
    let timer = 0;
    const tick = async () => {
      const got = await askProgress(progressId).catch(() => []);
      if (stopped) return;
      // 답이 끝나면 서버가 단계를 지운다. 화면이 닫히기 전 잠깐 빈 목록이 와도 보던 단계를 둔다
      if (got.length > 0) setSteps(got);
      setSeconds(Math.floor((Date.now() - started) / 1000));
      timer = window.setTimeout(tick, POLL_MS);
    };
    timer = window.setTimeout(tick, POLL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [progressId]);

  if (steps.length === 0) {
    return (
      <div className="bubble">
        {fallback}
        <span className="dots" />
      </div>
    );
  }
  const now = steps[steps.length - 1];
  return (
    <div className="bubble">
      <ol className="wait-steps" aria-label="처리 과정">
        {steps.slice(0, -1).map((step) => (
          <li className="done" key={step}>
            <Icon name="check" size={14} stroke={2} />
            {step}
          </li>
        ))}
        <li className="now" key={now}>
          <span className="wait-mark" aria-hidden="true" />
          <span>
            {`${now} 중`}
            <span className="dots" />
          </span>
          {seconds > 0 && <span className="wait-secs">{seconds}초</span>}
        </li>
      </ol>
    </div>
  );
}
