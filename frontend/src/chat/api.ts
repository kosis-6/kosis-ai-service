import type { AskRequest, AskResponse } from "../api/types";
import { authFetch } from "../auth/client";

async function postJson<T>(url: string, body: unknown): Promise<T> {
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
