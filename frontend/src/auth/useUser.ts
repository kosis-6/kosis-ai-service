import { useSyncExternalStore } from "react";
import { currentUser, subscribe, type User } from "./client";

/** 로그인한 사용자(표시 이름) 또는 null. 로그인·로그아웃·만료로 바뀌면 다시 그린다 */
export function useUser(): User | null {
  return useSyncExternalStore(subscribe, currentUser);
}
