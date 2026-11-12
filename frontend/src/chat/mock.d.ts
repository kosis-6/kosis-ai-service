import type { SearchRequest, SearchResponse } from "../api/types";

/** 목업 API: POST /search 와 같은 형식의 응답을 서버·HCX 호출 없이 만든다 (화면 개발용) */
export const MockApi: {
  search(body: SearchRequest): Promise<SearchResponse>;
};
