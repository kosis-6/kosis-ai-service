"""통계표 찾기를 LangGraph 흐름으로 옮긴 것.

graph.py  지금의 통계표 찾기(dialog.handle)를 노드로 나눈 흐름. 결과는 dialog.handle과 같다
plan.py   질문을 지표로 나누는 계획(HCX 1회)과 코드 보정
multi.py  graph.py에 계획과 지표별 검색을 얹은 흐름. 지표가 여럿이면 지표마다 후보 표를 덧붙인다
"""
