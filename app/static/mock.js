// 목업 API: POST /search 응답과 같은 형식을 서버·HCX 호출 없이 만든다 (화면 개발용)
// 표 ID·이름·기관·기간은 실제 카탈로그 값. 점수·후보 순위는 화면 확인용으로 지어낸 값이다.
(function () {
  const ORGS = { "101": "국가데이터처", "110": "행정안전부", "111": "법무부" };
  const KOSIS_URL = (org, id) => `https://kosis.kr/statHtml/statHtml.do?orgId=${org}&tblId=${id}`;

  // [org_id, 조사, 표 이름, 기간, 경로]
  const T = {
    DT_1PH2011: ["101", "인구총조사", "거처의종류별/반려동물보유유형별가구-시도", "2020",
      "인구 > 인구총조사 > 가구부문 > 표본(20%)부문 (2020년) > 주거실태"],
    DT_1PH2012: ["101", "인구총조사", "가구구분/가구주연령대별/반려동물보유유형별가구-시도", "2020",
      "인구 > 인구총조사 > 가구부문 > 표본(20%)부문 (2020년) > 주거실태"],
    DT_1PH2013: ["101", "인구총조사", "가구원수별/반려동물 보유 유형별가구-시도", "2020",
      "인구 > 인구총조사 > 가구부문 > 표본(20%)부문 (2020년) > 주거실태"],
    DT_1B81A02: ["101", "인구동향조사", "시도/출생장소별 출생", "1991~2025", "인구 > 인구동향조사 > 출생"],
    DT_1B81A16: ["101", "인구동향조사", "시도/법적혼인상태별 출생", "1981~2025", "인구 > 인구동향조사 > 출생"],
    DT_1PL1502: ["101", "인구총조사", "성 및 연령별 1인가구 - 시군구", "2015~2025",
      "인구 > 인구총조사 > 가구부문 > 1인가구"],
    DT_1PL1501: ["101", "인구총조사", "성 및 거처의 종류별 1인가구 - 시군구", "2015~2025",
      "인구 > 인구총조사 > 가구부문 > 1인가구"],
    DT_1BZ0506: ["101", "장래가구추계", "가구주의 연령/가구원수별 추계가구_시도", "2000~2052", "인구 > 장래가구추계"],
    DT_1JA1501: ["101", "인구총조사", "성별 외국인 - 시군구", "2015~2025", "인구 > 인구총조사 > 인구부문 > 외국인"],
    DT_1B040A10: ["111", "출입국자및체류외국인통계", "시군구별 및 연령별 등록외국인 현황", "2009~2025",
      "인구 > 출입국자및체류외국인통계 > 체류외국인통계"],
    TX_11025_A001_A: ["110", "지방자치단체외국인주민현황", "시군구별 외국인주민 현황", "2015~2024",
      "인구 > 지방자치단체외국인주민현황 > 2015년 이후(11월1일 기준) > 총괄"],
  };

  // 패밀리 '한국국적을 취득한 자': 대상 × 성별 × 판 (판은 [최신, 이전])
  const NAT = {
    "전체": { "계": ["TX_11025_A017_A", "TX_11025_A017"], "남": ["TX_11025_A018_A", "TX_11025_A018"], "여": ["TX_11025_A019_A", "TX_11025_A019"] },
    "혼인 귀화자": { "계": ["TX_11025_A020_A", "TX_11025_A020"], "남": ["TX_11025_A021_A", "TX_11025_A021"], "여": ["TX_11025_A022_A", "TX_11025_A022"] },
    "기타": { "계": ["TX_11025_A023_A", "TX_11025_A023"], "남": ["TX_11025_A024_A", "TX_11025_A024"], "여": ["TX_11025_A025_A", "TX_11025_A025"] },
  };
  const NAT_PATH = "인구 > 지방자치단체외국인주민현황 > 한국국적을 취득한 자";
  for (const [target, bySex] of Object.entries(NAT)) {
    for (const [sex, [latest, old]] of Object.entries(bySex)) {
      const who = target === "전체" ? "" : `-${target}`;
      const newName = target === "전체" ? `이전국적별 한국국적을 취득한 자(${sex})` : `한국국적을 취득한 자${who}(${sex})`;
      const oldPeriod = target === "전체" ? "2006~2015" : "2007~2015";
      T[latest] = ["110", "지방자치단체외국인주민현황", newName, target === "전체" ? "2015~2024" : "2015", NAT_PATH];
      T[old] = ["110", "지방자치단체외국인주민현황", `한국국적을 취득한 자${who}(${sex})(${oldPeriod})`, oldPeriod, NAT_PATH];
    }
  }

  const sessions = {};
  let counter = 0;

  function table(id) {
    const [org, survey, name, period, path] = T[id];
    return { tbl_id: id, org_id: org, tbl_nm: name, survey_nm: survey, org_nm: ORGS[org], period, path: `주제별통계 > ${path}`, url: KOSIS_URL(org, id) };
  }

  function brief(id) {
    const t = table(id);
    return { tbl_id: id, table_name: t.tbl_nm, survey_name: t.survey_nm, edition: t.period, url: t.url };
  }

  function soloFamily(id) {
    return { id: `F_${id}`, name: T[id][2], current: { target: null, sex: null }, members: [], editions: [] };
  }

  function natFamily(target, sex, chosen) {
    const members = [];
    for (const [tg, bySex] of Object.entries(NAT)) {
      for (const [sx, ids] of Object.entries(bySex)) {
        if (tg === target && sx === sex) continue;
        members.push({ tbl_id: ids[0], label: `${tg}(${sx})`, target: tg, sex: sx });
      }
    }
    const editions = NAT[target][sex].filter((id) => id !== chosen)
      .map((id) => ({ tbl_id: id, period: T[id][3], tbl_nm: T[id][2] }));
    return { id: "F_nat", name: "한국국적을 취득한 자", current: { target, sex }, members, editions };
  }

  function rec(rank, id, checks, score, family, notes) {
    return { rank, ...table(id), checks, family: family || soloFamily(id), notes: notes || [], score };
  }

  // search_answer.other_versions 와 같은 규칙
  function otherVersions(f) {
    const sexes = f.members.filter((m) => m.target === f.current.target && m.sex).map((m) => m.sex);
    const targets = [...new Set(f.members.filter((m) => m.target !== f.current.target).map((m) => m.target))];
    const parts = [sexes.join("/"), targets.filter(Boolean).join(", "), f.editions.map((e) => `${e.period}년판`).join(", ")];
    return parts.filter(Boolean).join(" · ");
  }

  const CHECK_LABELS = { year: "연도", region_level: "지역 수준", sex: "성별", target: "대상", projection: "장래 추계" };

  // search_answer.answer_text (ok / partial_match) 와 같은 틀
  function okText(r, notice, targetDefault, labels) {
    const lines = [
      `요청하신 통계는 ‘${r.survey_nm}’에서 확인할 수 있습니다.`,
      `추천 통계표: ${r.tbl_nm} (${r.org_nm}, ${r.period}년)`,
    ];
    const keys = Object.keys(r.checks);
    if (keys.length) {
      lines.push("조건 확인: " + keys.map((k) => `${(labels && labels[k]) || CHECK_LABELS[k] || k} ${r.checks[k] ? "✔" : "✘"}`).join(" · "));
    }
    if (notice) lines.push(`확인 필요: ${notice}`);
    if (targetDefault) lines.push("대상을 지정하지 않아 ‘전체’ 표를 안내합니다.");
    const others = otherVersions(r.family);
    if (others) lines.push(`같은 표의 다른 버전: ${others}`);
    r.notes.forEach((n) => lines.push(`참고: ${n}`));
    lines.push(`바로가기: ${r.url}`);
    return lines.join("\n");
  }

  function parsed(topic, extra) {
    return {
      topic, target: null, query_type: "find_table",
      conditions: { year: null, year_end: null, region_level: null, sex: null },
      ...extra,
    };
  }

  function debugOf(candidates, extra) {
    return {
      mock: true, retrieval: "hybrid", candidates, missing_axes: [], fixes: [], parse_error: false,
      timings_ms: { parse: 420 + Math.round(Math.random() * 200), embed: 160, rank: 12, llm_pick: 610 },
      ...extra,
    };
  }

  function cand(id, score, kw, dense, checks) {
    return { ...brief(id), score, keyword: kw, dense, checks: checks || {} };
  }

  function okResponse(sid, opts) {
    const recs = opts.recs;
    return {
      session_id: sid,
      status: opts.status || "ok",
      answer: okText(recs[0], opts.notice, opts.targetDefault, opts.labels),
      parsed: opts.parsed,
      clarify: null,
      recommendations: recs,
      notice: opts.notice || null,
      debugCands: opts.debugCands,
      debugExtra: opts.debugExtra,
    };
  }

  // ---- 시나리오 -------------------------------------------------------------

  function pets(sid) {
    return okResponse(sid, {
      parsed: parsed("반려동물 보유 가구"),
      recs: [
        rec(1, "DT_1PH2011", {}, 0.812),
        rec(2, "DT_1PH2013", {}, 0.794),
        rec(3, "DT_1PH2012", {}, 0.781),
      ],
      debugCands: [cand("DT_1PH2011", 0.812, 0.64, 0.71), cand("DT_1PH2013", 0.794, 0.61, 0.70), cand("DT_1PH2012", 0.781, 0.58, 0.69)],
    });
  }

  function birthPlace(sid) {
    return okResponse(sid, {
      parsed: parsed("출생장소별 출생"),
      recs: [
        rec(1, "DT_1B81A02", {}, 0.774, null, ["출생장소 '기타'에는 이송 중 출생 등이 포함됩니다."]),
        rec(2, "DT_1B81A16", {}, 0.611),
      ],
      debugCands: [cand("DT_1B81A02", 0.774, 0.55, 0.68), cand("DT_1B81A16", 0.611, 0.12, 0.61)],
    });
  }

  function singleFuture(sid, year) {
    const p = parsed("1인가구", { conditions: { year, year_end: null, region_level: "시군구", sex: null } });
    const labels = { year: `${year}년`, region_level: "시군구" };
    return okResponse(sid, {
      status: "partial_match",
      parsed: p,
      labels,
      notice: `${year}년 조건을 충족하지 않습니다`,
      recs: [
        rec(1, "DT_1PL1502", { year: false, region_level: true }, 0.702),
        rec(2, "DT_1BZ0506", { year: true, region_level: false }, 0.688),
        rec(3, "DT_1PL1501", { year: false, region_level: true }, 0.655),
      ],
      debugCands: [
        cand("DT_1PL1502", 0.702, 0.58, 0.66, { year: false, region_level: true }),
        cand("DT_1BZ0506", 0.688, 0.41, 0.67, { year: true, region_level: false }),
        cand("DT_1PL1501", 0.655, 0.52, 0.62, { year: false, region_level: true }),
      ],
    });
  }

  function natClarify(sid, q) {
    const axes = [
      { axis: "target", options: ["전체", "혼인 귀화자", "기타"] },
      { axis: "sex", options: ["전체", "남", "여"] },
      { axis: "edition", options: ["2015~2024", "2006~2015"] },
    ];
    sessions[sid] = { status: "clarify", topic: "한국국적 취득자", q };
    return {
      session_id: sid,
      status: "clarify",
      answer: [
        "‘한국국적을 취득한 자’ 통계(행정안전부, 지방자치단체외국인주민현황)가 있습니다. 3가지만 확인할게요.",
        "① 대상: 전체 / 혼인 귀화자 / 기타",
        "② 성별: 전체 / 남 / 여",
        "③ 연도: 2015~2024년 / 2006~2015년",
        "예) “혼인 귀화자, 여, 2010년” 또는 “아무거나”",
      ].join("\n"),
      parsed: parsed("한국국적 취득자"),
      clarify: { level: "member", family_id: "F_nat", family_name: "한국국적을 취득한 자", axes },
      recommendations: [],
      notice: null,
      debugExtra: { missing_axes: ["target", "sex"] },
      debugCands: [cand("TX_11025_A017_A", 0.731, 0.62, 0.66), cand("TX_11025_A020_A", 0.725, 0.6, 0.66), cand("TX_11025_A023_A", 0.702, 0.55, 0.64)],
    };
  }

  function natResolve(sid, q, sel) {
    let target = sel.target || ["혼인 귀화자", "기타", "전체"].find((t) => q.includes(t)) || null;
    let sex = sel.sex || (/여자|여성|(^|[\s,])여($|[\s,])/.test(q) ? "여" : /남자|남성|(^|[\s,])남($|[\s,])/.test(q) ? "남" : null);
    const anything = /아무거나|상관\s*없/.test(q);
    if (!target && !sex && !anything && !/\d{4}/.test(q)) return null; // 새 질문으로 본다
    const targetDefault = !target;
    target = target || "전체";
    sex = !sex || sex === "전체" ? "계" : sex;
    const yearMatch = q.match(/(\d{4})\s*~?\s*(\d{4})?/);
    const old = yearMatch && Number(yearMatch[1]) < 2015;
    const id = NAT[target][sex][old ? 1 : 0];
    const fam = natFamily(target, sex, id);
    const checks = yearMatch && !/년판/.test(q) ? { year: true } : {};
    const labels = yearMatch ? { year: `${yearMatch[1]}년` } : {};
    delete sessions[sid];
    return okResponse(sid, {
      parsed: parsed("한국국적 취득자", { target }),
      recs: [rec(1, id, checks, null, fam)],
      targetDefault,
      labels,
      debugCands: [],
    });
  }

  const FOREIGN = ["DT_1JA1501", "DT_1B040A10", "TX_11025_A001_A"];

  function foreignAmbiguous(sid) {
    const families = FOREIGN.map((id) => {
      const b = brief(id);
      return { family_id: `F_${id}`, family_name: b.table_name, tbl_id: id, tbl_nm: b.table_name, survey_nm: b.survey_name, period: b.edition, url: b.url };
    });
    sessions[sid] = { status: "ambiguous", families };
    const lines = ["비슷한 통계표가 여러 개 있습니다. 어떤 자료를 찾으시나요?"];
    families.forEach((f, i) => lines.push(`${"①②③④⑤"[i]} ${f.tbl_nm} (${f.survey_nm}, ${f.period}년)`));
    lines.push("번호나 이름으로 답해 주세요.");
    return {
      session_id: sid,
      status: "ambiguous",
      answer: lines.join("\n"),
      parsed: parsed("외국인 수", { conditions: { year: null, year_end: null, region_level: "시군구", sex: null } }),
      clarify: { level: "family", families },
      recommendations: [],
      notice: null,
      debugCands: [cand("DT_1JA1501", 0.744, 0.6, 0.67, { region_level: true }), cand("DT_1B040A10", 0.741, 0.58, 0.67, { region_level: true }), cand("TX_11025_A001_A", 0.738, 0.57, 0.66, { region_level: true })],
    };
  }

  function foreignResolve(sid, q, sel) {
    const state = sessions[sid];
    let fam = state.families.find((f) => f.family_id === sel.family_id);
    const n = q.match(/^\s*([1-5①②③④⑤])/);
    if (!fam && n) fam = state.families["12345①②③④⑤".indexOf(n[1]) % 5];
    if (!fam) fam = state.families.find((f) => q.includes(f.tbl_nm) || q.includes(f.survey_nm));
    if (!fam) return null;
    delete sessions[sid];
    const others = FOREIGN.filter((id) => id !== fam.tbl_id);
    return okResponse(sid, {
      parsed: parsed("외국인 수", { conditions: { year: null, year_end: null, region_level: "시군구", sex: null } }),
      labels: { region_level: "시군구" },
      recs: [rec(1, fam.tbl_id, { region_level: true }, null), ...others.map((id, i) => rec(i + 2, id, { region_level: true }, 0.74 - i * 0.01))],
      debugCands: [],
    });
  }

  function message(sid, status, q) {
    const answers = {
      out_of_scope: "제공 범위(KOSIS 인구 분야 통계표)에서 질문에 맞는 통계표를 찾지 못했습니다. 찾는 통계의 주제나 표 이름에 들어갈 단어로 다시 질문해 주세요.",
      not_statistics: "통계표를 찾아 드리는 서비스입니다. 이렇게 질문해 보세요.\n예) 2024년 시군구별 외국인 주민 수 / 연령별 혼인 건수 / 1인 가구 통계표",
    };
    return {
      session_id: sid, status, answer: answers[status],
      parsed: parsed(null, { query_type: status === "not_statistics" ? "not_statistics" : "find_table" }),
      clarify: null, recommendations: [], notice: null,
      debugCands: status === "out_of_scope" ? [cand("DT_1PL1502", 0.312, 0.05, 0.41)] : [],
      debugExtra: status === "out_of_scope" ? { llm_pick: { verdict: "none", reason: "비인구 주제 (목업)" } } : {},
    };
  }

  function route(q, sid, sel) {
    const state = sessions[sid];
    if (state && state.status === "clarify") {
      const r = natResolve(sid, q, sel);
      if (r) return r;
    }
    if (state && state.status === "ambiguous") {
      const r = foreignResolve(sid, q, sel);
      if (r) return r;
    }
    delete sessions[sid];
    if (/날씨|안녕|이름이|점심|영화|어떻게 (써|쓰)|사용법/.test(q)) return message(sid, "not_statistics", q);
    if (/집값|매매가|교통사고|범죄|자살|소득|물가|병상|월급/.test(q)) return message(sid, "out_of_scope", q);
    if (/귀화|국적.*(취득|딴|얻)/.test(q)) return natClarify(sid, q);
    if (/외국인/.test(q)) return foreignAmbiguous(sid);
    if (/반려|강아지|고양이/.test(q)) return pets(sid);
    if (/집에서.*(낳|태어)|출생\s*장소|병원 말고/.test(q)) return birthPlace(sid);
    const future = q.match(/20(3\d|4\d|5\d)/);
    if (/1인|혼자/.test(q)) return singleFuture(sid, future ? Number(future[0]) : 2030);
    return message(sid, "out_of_scope", q);
  }

  window.MockApi = {
    examples: [
      "강아지나 고양이 키우는 집이 얼마나 돼?",
      "병원 말고 집에서 아기 낳는 경우도 있어?",
      "국적 취득한 외국인 통계 찾아줘",
      "시군구별 외국인 수",
      "2030년 시군구별 1인가구",
      "요즘 집값 얼마나 올랐어?",
      "오늘 날씨 어때?",
    ],
    async search(body) {
      await new Promise((r) => setTimeout(r, 350 + Math.random() * 400));
      const sid = body.session_id || `M_${Date.now().toString(36)}${counter++}`;
      const res = route(body.question.trim(), sid, body.selections || {});
      const { debugCands, debugExtra, ...out } = res;
      out.debug = body.debug ? debugOf(debugCands || [], debugExtra || {}) : null;
      return JSON.parse(JSON.stringify(out));
    },
  };
})();
