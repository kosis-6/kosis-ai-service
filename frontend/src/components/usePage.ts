import { useLayoutEffect } from "react";

export function usePage(page: "chat" | "account" | "admin", title: string): void {
  useLayoutEffect(() => {
    document.body.dataset.page = page;
    document.title = `${title} · LIKE SNAKE`;
  }, [page, title]);
}
