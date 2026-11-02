# 꼬인 질문 대응: 검색어 재작성 비교 (2026-11-02)

통계표 검색(`/search`)에서 구어·간접 표현 질문("들어온 사람보다 나간 사람이 많은 지역")이 틀리는 문제를
재작성 검색어로 줄일 수 있는지 측정했다.

## 세트

| 파일 | 문항 | 내용 |
|---|---|---|
| `team41.jsonl` | 41 | 팀원셋 (`feature/golden` 브랜치 `eval/dev_draft.jsonl`, id를 d→j로 바꿈) |
| `devfail32.jsonl` | 32 | 오답 리포트(main1102b)의 개발셋·생성셋 오답 32건. 정답 표는 리포트에서 옮겨 적음 |

개발셋에서 원래 맞던 문항(약 145개)은 레포에 없어 회귀 확인은 팀원셋으로만 했다.

## 채점

- 정답: ok·partial_match면 최종 표가 정답 목록에 있음 / clarify면 되묻는 패밀리가 정답 패밀리 /
  ambiguous면 1순위 패밀리가 정답 패밀리 / 범위 밖 문항은 out_of_scope·not_statistics
- 되묻기 답을 이어 넣지 않고 첫 응답만 본다. 그래서 리포트의 점수와 절대값이 다르고, 방식끼리 비교에만 쓴다
- 후보 8위 안: `candidates` 상위 8개(LLM 검토가 보는 수)에 정답 표가 있는지
- 임베딩 호출 제한으로 측정이 흔들리지 않게 실패하면 기다렸다 다시 부른다 (세 회차 모두 임베딩 실패 0)

## 결과 (회차마다 1번씩 실행)

| 방식 | 팀원 41 | 개발 오답 32 | 합계 | 후보 8위 안 |
|---|---|---|---|---|
| base: 지금 코드 | 26 | 2 | 28 | 60/72 |
| v2: HCX-007 재작성 (`SEARCH_REWRITER=hcx007`) | 29 | 5 | **34** | 59/72 |
| v1: 슬롯 프롬프트(DASH-002)가 검색어 1~3개 생성 | 25 | 3 | 28 | 61/72 |

- v2: 얻음 7 (j46, j73, j79, x-d31, x-d53, x-d92, x-d94), 잃음 1 (x-d137: 1위 정답이 4위로).
  재현율은 그대로이고 정답 순위가 1~3위로 올라 최종 답이 맞는 경우가 늘었다
- v1: 얻음 1 (x-d72), 잃음 1 (j65). DASH-002는 프롬프트를 바꿔도 질문을 그대로 옮기는 일이 잦아
  효과 없음 → 슬롯 프롬프트 변경은 되돌림
- v2로도 못 고친 검색 누락: x-d73(어린이집·할머니), x-d72(결혼·출산으로 일 그만둠)는 HCX-007이
  엉뚱한 용어로 바꿔 순위가 떨어졌다

## 남은 일

- HCX 해석이 실행마다 달라 +6이 잡음일 수 있다. v2를 2~3번 더 돌려 같은 결과가 나오면 기본값을 바꾼다
- 질문 하나에 HCX-007 호출 1번(슬롯 추출과 병렬, 약 1.7초)과 임베딩 호출이 최대 3번 더 든다
- 새 테스트셋(t3)으로 한 번 확인

## 다시 돌리기

```bash
# 레포 최상위에서, 컨테이너 안
python eval/rewrite_check/run_eval.py eval/rewrite_check/team41.jsonl eval/rewrite_check/devfail32.jsonl \
  --label base --out eval/rewrite_check/base.jsonl
SEARCH_REWRITER=hcx007 python eval/rewrite_check/run_eval.py eval/rewrite_check/team41.jsonl \
  eval/rewrite_check/devfail32.jsonl --label v2 --out eval/rewrite_check/v2.jsonl
cd eval/rewrite_check && python compare.py base.jsonl v2.jsonl   # 세트별 점수, 문항별 변화
```

문항당 약 20초(HCX 2~3번·임베딩 2~4번)라 73문항에 25분쯤 걸린다.
`--old-slot-prompt`는 v2 측정 때 슬롯 프롬프트를 원래 것(`search_prompt_old.py`)으로 맞추던 옵션으로,
지금은 레포 프롬프트가 원래 것이라 필요 없다.
