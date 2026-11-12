import { useLayoutEffect } from "react";

/** 화면마다 body 배치(styles/*.css 의 body[data-page])와 탭 제목을 맞춘다 */
export function usePage(page: "chat" | "account", title: string): void {
  useLayoutEffect(() => {
    document.body.dataset.page = page;
    document.title = `${title} · TEAM SIX`;
  }, [page, title]);
}
