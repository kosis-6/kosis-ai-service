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
