/* Local-first verification overlay. Loaded after app.js so existing UI helpers stay shared. */

resultRow = function localFirstResultRow(row) {
  const tr = document.createElement("tr");
  tr.className = `result-row ${row.verdict || "review"}`;
  const gatePending = row.gate_status === "pending";
  const printable = !!row.cpm_id && !gatePending;
  const gateContainer = row.gate_container_no || row.input_container || "";
  const gateMissing = row.gate_status === "not_found";
  let gateContainerCell;
  let gateSealCell;
  if (gatePending) {
    gateContainerCell = `<strong>${escapeHtml(gateContainer)}</strong>${spinner("正在后台查询门证")}`;
    gateSealCell = spinner("正在后台查询门证");
  } else {
    gateContainerCell = gateMissing
      ? missingGate(gateContainer)
      : `${row.gate_container_no ? gateContainerCopy(row.gate_container_no, row.container_matches) : `<strong class="${comparisonClass(row.container_matches).trim()}">${escapeHtml(gateContainer)}</strong>`}${gateDateBadge(row)}`;
    gateSealCell = gateMissing
      ? missingGate()
      : row.gate_seal_no
      ? `<strong class="${comparisonClass(row.seal_matches).trim()}">${escapeHtml(row.gate_seal_no)}</strong>`
        : noData("未查询到门证铅封号");
  }
  const pendingPhoto = row.needs_photo_refresh
    ? spinner("正在后台准备照片和 OCR")
    : null;
  const containerCell = row.container_ocr
    ? ocrCell(row.container_ocr, "箱号", row.container_matches)
    : pendingPhoto || noData("无箱号照片识别结果");
  const sealCell = row.seal_ocr
    ? ocrCell(row.seal_ocr, "铅封号", row.seal_matches)
    : pendingPhoto || noData("无铅封号照片识别结果");
  const otherCell = row.other_photos?.length
    ? otherPhotos(row.other_photos)
    : pendingPhoto || noData("没有其他四阶段照片");
  tr.innerHTML = `<td><button class="print-button" ${printable ? "" : "disabled"}>${gatePending ? "等待" : row.print_status ? "重打" : "打印"}</button></td><td class="status-number">${row.business_stage ?? "—"}</td><td>${gateContainerCell}</td><td>${gateSealCell}</td><td>${containerCell}</td><td>${sealCell}</td><td>${otherCell}</td><td>${escapeHtml(row.last_printed_at || "")}</td>`;
  bindGateContainerCopy(tr);
  if (printable) {
    tr.querySelector(".print-button").addEventListener("click", () => printRow(row, tr));
  }
  tr.querySelectorAll(".ocr-preview,.other-photo-preview").forEach((image) =>
    image.addEventListener("click", () => openImageViewer(image.dataset.originalUrl)),
  );
  return tr;
};

$("#verify").addEventListener(
  "click",
  async (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    if (busy) return;
    const containers = parseInputs();
    if (!containers.length) return toast("请先输入箱号");
    busy = true;
    const button = $("#verify");
    button.disabled = true;
    button.textContent = "正在读取本地照片…";
    renderLoading(containers);
    let localRows = [];
    try {
      const local = await api("/api/verify/local", {
        method: "POST",
        body: JSON.stringify({ containers }),
      });
      localRows = local.rows || [];
      containers.forEach((container, index) => {
        replaceLoadingRow(
          index,
          localRows[index] || {
            input_container: container,
            gate_status: "pending",
            verdict: "review",
            message: "未取得本地结果",
          },
        );
      });
      button.textContent = "正在后台刷新门证…";
      for (let index = 0; index < containers.length; index += 1) {
        const localRow = localRows[index] || {};
        if (localRow.gate_status === "invalid") continue;
        if (
          localRow.gate_status === "found" &&
          !localRow.gate_needs_refresh &&
          !localRow.needs_photo_refresh
        ) continue;
        try {
          const completed = await api("/api/verify/gate", {
            method: "POST",
            body: JSON.stringify({ container: containers[index] }),
          });
          const current = $("#rows").children[index];
          if (current) current.replaceWith(resultRow(completed.row || localRow));
        } catch (error) {
          const current = $("#rows").children[index];
          if (current) {
            current.replaceWith(
              resultRow({
                ...localRow,
                gate_status: "error",
                verdict: "review",
                message: error.message,
              }),
            );
          }
          if (/登录|会话/.test(error.message)) {
            setSession("logged_out", error.message);
            throw error;
          }
        }
      }
    } catch (error) {
      toast(error.message);
    } finally {
      busy = false;
      button.disabled = false;
      button.textContent = "识别并核验";
    }
  },
  true,
);
