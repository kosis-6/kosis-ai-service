import { useSyncExternalStore } from "react";
import { currentUser, subscribe, type User } from "./client";

export function useUser(): User | null {
  return useSyncExternalStore(subscribe, currentUser);
}
