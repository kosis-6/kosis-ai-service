// 보고서 미리보기 탭의 버튼 (report.ts의 buildPreview).
// 새 탭은 채팅 화면의 CSP(인라인 스크립트 금지)를 물려받으므로 onclick 대신 이 파일을 불러온다.
// 저장하는 파일은 미리보기와 같은 내용을 스타일까지 담은 한 파일이다 (#report-file에 들어 있다)
const file = document.getElementById("report-file");

document.getElementById("report-print")?.addEventListener("click", () => window.print());

document.getElementById("report-save")?.addEventListener("click", () => {
  const html = JSON.parse(file.textContent);
  const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = file.dataset.filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
