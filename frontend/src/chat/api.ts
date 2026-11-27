import type { AskProgress, AskRequest, AskResponse } from "../api/types";
import { authFetch } from "../auth/client";

export async function postJson<T>(url: string, body: unknown): Promise<T> {
  const res = await authFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = (await res.json()).detail;
    } catch {
    }
    if (typeof detail === "string") throw new Error(detail);
    throw new Error(`서버 오류 ${res.status}${detail ? `: ${JSON.stringify(detail)}` : ""}`);
  }
  return (await res.json()) as T;
}

export function ask(body: AskRequest): Promise<AskResponse> {
  return postJson<AskResponse>("/ask", body);
}

/** 답을 기다리는 동안 서버가 거친 처리 단계 (마지막이 지금 하는 일). 못 읽으면 빈 목록 */
export async function askProgress(progressId: string): Promise<string[]> {
  const res = await fetch(`/ask/progress/${encodeURIComponent(progressId)}`);
  if (!res.ok) return [];
  return ((await res.json()) as AskProgress).steps;
}
