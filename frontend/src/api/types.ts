// 서버 API 타입. schema.d.ts 는 FastAPI 의 OpenAPI 에서 만든다 (npm run gen:api, 직접 고치지 않는다)
import type { components } from "./schema";

type Schemas = components["schemas"];

export type Token = Schemas["Token"];
export type UserRead = Schemas["UserRead"];
export type EmailCodeSent = Schemas["EmailCodeSent"];
export type SearchRequest = Schemas["SearchRequest"];

// /search 응답의 parsed·clarify·recommendations 는 서버 스키마가 자유 형식(dict)이라
// 화면이 읽는 칸만 여기에 적는다 (backend/app/services/search_answer.py 가 만드는 모양)
export type Checks = Record<string, boolean>;

export type Family = {
  members?: { label: string }[];
  editions?: { period: string }[];
};

export type Recommendation = {
  tbl_id: string;
  tbl_nm: string;
  org_nm: string;
  survey_nm: string;
  period: string;
  path?: string | null;
  url: string;
  checks?: Checks;
  notes?: string[];
  family?: Family;
};

export type Parsed = {
  query_type?: string | null;
  topic?: string | null;
  conditions?: {
    year?: number | string | null;
    year_end?: number | string | null;
    region_level?: string | null;
  };
};

export type Axis = { axis: string; options: string[] };
export type FamilyChoice = { family_id: string; tbl_nm: string; survey_nm: string; period: string };
export type Clarify =
  | { level: "member"; axes: Axis[] }
  | { level: "family"; families: FamilyChoice[] };

export type DebugCandidate = { tbl_id: string; table_name: string; score?: number | null };
export type Debug = { retrieval?: string; candidates?: DebugCandidate[] } & Record<string, unknown>;

export type SearchResponse = {
  session_id: string;
  status: string; // ok / partial_match / clarify / ambiguous / out_of_scope / not_statistics / parse_error
  answer: string;
  parsed: Parsed;
  clarify: Clarify | null;
  recommendations: Recommendation[];
  notice: string | null;
  debug?: Debug | null;
};
