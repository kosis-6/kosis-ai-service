import type { components } from "./schema";

type Schemas = components["schemas"];

export type Token = Schemas["Token"];
export type UserRead = Schemas["UserRead"];
export type EmailCodeSent = Schemas["EmailCodeSent"];
export type AskRequest = Schemas["AskRequest"];
export type AskResponse = Omit<Schemas["AskResponse"], "tables"> & {
  tables?: (Partial<SearchResponse> & { recommendations: Recommendation[] }) | null;
};
export type Chart = Schemas["Chart"];
export type ChartRow = Schemas["ChartRow"];
export type Source = Schemas["Source"];
export type Applied = Schemas["Applied"];

export type ConversationRead = Schemas["ConversationRead"];
export type ConversationList = Schemas["ConversationList"];
export type ConversationDetail = Schemas["ConversationDetail"];
export type ConversationTurn = Schemas["ConversationTurn"];

export type LogSummary = Schemas["LogSummary"];
export type PersonList = Schemas["PersonList"];
export type PersonRead = Schemas["PersonRead"];
export type SessionList = Schemas["SessionList"];
export type SessionRead = Schemas["SessionRead"];
export type SessionDetail = Schemas["SessionDetail"];
export type TurnDetail = Schemas["TurnDetail"];
export type FlowStep = Schemas["FlowStep"];
export type SearchLogList = Schemas["SearchLogList"];
export type SearchLogRead = Schemas["SearchLogRead"];
export type SearchLogDetail = Schemas["SearchLogDetail"];

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
  basis?: string | null;
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
export type FamilyChoice = { family_id: string; tbl_nm: string; survey_nm: string; period: string; diff?: string | null };
export type Clarify =
  | { level: "member"; axes: Axis[] }
  | { level: "family"; families: FamilyChoice[] };

export type SearchResponse = {
  session_id: string;
  status: string;
  answer: string;
  parsed: Parsed;
  clarify: Clarify | null;
  recommendations: Recommendation[];
  together?: Recommendation[];
  notice: string | null;
};
