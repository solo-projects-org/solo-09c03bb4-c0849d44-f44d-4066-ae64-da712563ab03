/* 港池疏浚验收计量网页前端 (原生 JS, 无构建依赖) */
"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("map");
const ctx = canvas.getContext("2d");

let view = { minX: 0, minY: 0, maxX: 0, maxY: 0, cell: 20 };
let cellData = new Map();   // "x,y" -> {before, after}
let rect = null;             // 当前框选 {x0,y0,x1,y1} (格坐标, 闭区间)
let drag = null;             // 拖选状态
let exRect = null;           // 导出选区 {x0,y0,x1,y1} (米, 8 米格线, 半开区间)
let exDrag = null;           // 导出选区拖选起点

async function api(path, opts) {
  const resp = await fetch(path, opts);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
  return body;
}

function setMsg(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className = "msg " + (cls || "mut");
}

/* ---------------- 上传 ---------------- */
document.querySelectorAll(".up button").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const phase = btn.dataset.phase;
    const input = $("file-" + phase);
    if (!input.files.length) {
      setMsg("up-msg", "请先选择 " + (phase === "before" ? "前测" : "后测") + " CSV 文件", "err");
      return;
    }
    const fd = new FormData();
    fd.append("file", input.files[0]);
    btn.disabled = true;
    try {
      const r = await api(`/api/surveys/${phase}`, { method: "POST", body: fd });
      setMsg(
        "up-msg",
        `${phase === "before" ? "前测" : "后测"}已接收: 收 ${r.rows_received} 行, `
        + `写入 ${r.rows_applied} 格, 旧测次跳过 ${r.rows_skipped_stale} 格, `
        + `重算块 ${r.blocks_touched.length} 个, 数据版本 v${r.data_version}`,
        "ok"
      );
      await refresh();
    } catch (e) {
      setMsg("up-msg", "上传被退回: " + e.message, "err");
    } finally {
      btn.disabled = false;
    }
  });
});

/* ---------------- 数据加载与绘制 ---------------- */
async function refresh() {
  const ext = await api("/api/extent");
  const any = [ext.before, ext.after].filter(Boolean);
  if (!any.length) {
    cellData = new Map();
    draw();
    await loadBatches();
    return;
  }
  const minX = Math.min(...any.map((e) => e.x0));
  const maxX = Math.max(...any.map((e) => e.x1));
  const minY = Math.min(...any.map((e) => e.y0));
  const maxY = Math.max(...any.map((e) => e.y1));
  view = { minX, minY, maxX, maxY, cell: 0 };

  const data = await api(`/api/cells?x0=${minX}&y0=${minY}&x1=${maxX}&y1=${maxY}`);
  cellData = new Map();
  for (const c of data.cells) {
    const key = `${c.x},${c.y}`;
    if (!cellData.has(key)) cellData.set(key, {});
    cellData.get(key)[c.phase] = c.depth_cm; // null = 空水深
  }
  draw();
  await loadBatches();
}

function colorFor(d) {
  if (d.before === undefined || d.after === undefined) return "#e8edf2";
  if (d.before === null || d.after === null) return "#94a3b8";
  const diff = d.after - d.before; // 正: 挖深; 负: 回淤
  if (diff === 0) return "#f1f5f9";
  const mag = Math.min(Math.abs(diff) / 50, 1);
  if (diff > 0) {
    const g = Math.round(185 - mag * 130), b = Math.round(120 - mag * 90);
    return `rgb(185,${g},${b})`;
  }
  const g = Math.round(78 + (1 - mag) * 120), r = Math.round(29 + (1 - mag) * 150);
  return `rgb(${r},${g},216)`;
}

function draw() {
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const nx = view.maxX - view.minX + 1 || 1;
  const ny = view.maxY - view.minY + 1 || 1;
  const cell = Math.max(4, Math.floor(Math.min((W - 40) / nx, (H - 40) / ny)));
  view.cell = cell;
  const ox = 20, oy = 20;
  const px = (x) => ox + (x - view.minX) * cell;
  const py = (y) => oy + (y - view.minY) * cell;

  // 格底色
  for (const [key, d] of cellData) {
    const [x, y] = key.split(",").map(Number);
    ctx.fillStyle = colorFor(d);
    ctx.fillRect(px(x), py(y), cell, cell);
  }

  // 块边界 (每 16 格粗线, 由后端 /api/blocks 给出块大小)
  ctx.strokeStyle = "#475569";
  ctx.lineWidth = 1.5;
  const bs = Number($("bsize").textContent);
  for (let bx = Math.floor(view.minX / bs); bx <= Math.floor(view.maxX / bs); bx++) {
    const x = bx * bs;
    if (x < view.minX || x > view.maxX) continue;
    ctx.beginPath(); ctx.moveTo(px(x), oy); ctx.lineTo(px(x), oy + ny * cell); ctx.stroke();
  }
  for (let by = Math.floor(view.minY / bs); by <= Math.floor(view.maxY / bs); by++) {
    const y = by * bs;
    if (y < view.minY || y > view.maxY) continue;
    ctx.beginPath(); ctx.moveTo(ox, py(y)); ctx.lineTo(ox + nx * cell, py(y)); ctx.stroke();
  }

  // 细格线
  ctx.strokeStyle = "rgba(100,116,139,.25)";
  ctx.lineWidth = 1;
  ctx.strokeRect(ox, oy, nx * cell, ny * cell);

  // 框选区
  if (rect) {
    const r = normalize(rect);
    ctx.fillStyle = "rgba(253,230,138,.45)";
    ctx.strokeStyle = "#b45309";
    ctx.lineWidth = 2;
    const rw = (r.x1 - r.x0 + 1) * cell, rh = (r.y1 - r.y0 + 1) * cell;
    ctx.fillRect(px(r.x0), py(r.y0), rw, rh);
    ctx.strokeRect(px(r.x0), py(r.y0), rw, rh);
  }

  // 导出选区 (8 米格线, 半开区间, 坐标即米)
  if (exRect) {
    const r = normalize(exRect);
    ctx.fillStyle = "rgba(134,239,172,.30)";
    ctx.strokeStyle = "#15803d";
    ctx.lineWidth = 2;
    const rw = (r.x1 - r.x0) * cell, rh = (r.y1 - r.y0) * cell;
    ctx.fillRect(px(r.x0), py(r.y0), rw, rh);
    ctx.strokeRect(px(r.x0), py(r.y0), rw, rh);
  }
}

function normalize(r) {
  return {
    x0: Math.min(r.x0, r.x1), x1: Math.max(r.x0, r.x1),
    y0: Math.min(r.y0, r.y1), y1: Math.max(r.y0, r.y1),
  };
}

/* ---------------- 鼠标沿格线框选 ---------------- */
function eventCell(ev) {
  const box = canvas.getBoundingClientRect();
  const sx = canvas.width / box.width, sy = canvas.height / box.height;
  const cx = (ev.clientX - box.left) * sx - 20;
  const cy = (ev.clientY - box.top) * sy - 20;
  const x = view.minX + Math.floor(cx / view.cell);
  const y = view.minY + Math.floor(cy / view.cell);
  return { x, y };
}

/* 连续格线坐标 (米), 供 8 米格线吸附 */
function eventLine(ev) {
  const box = canvas.getBoundingClientRect();
  const sx = canvas.width / box.width, sy = canvas.height / box.height;
  const cx = (ev.clientX - box.left) * sx - 20;
  const cy = (ev.clientY - box.top) * sy - 20;
  return {
    x: view.minX + Math.round(cx / view.cell),
    y: view.minY + Math.round(cy / view.cell),
  };
}
const snap8 = (v) => Math.round(v / 8) * 8;

canvas.addEventListener("mousedown", (ev) => {
  if ($("snap8").checked) {
    const l = eventLine(ev);
    exDrag = { x: snap8(l.x), y: snap8(l.y) };
    exRect = { x0: exDrag.x, y0: exDrag.y, x1: exDrag.x, y1: exDrag.y };
    syncExInputs();
    draw();
    return;
  }
  const c = eventCell(ev);
  if (c.x < view.minX || c.x > view.maxX || c.y < view.minY || c.y > view.maxY) return;
  drag = { x0: c.x, y0: c.y };
  rect = { x0: c.x, y0: c.y, x1: c.x, y1: c.y };
  draw();
});
canvas.addEventListener("mousemove", (ev) => {
  if (exDrag) {
    const l = eventLine(ev);
    exRect = { x0: exDrag.x, y0: exDrag.y, x1: snap8(l.x), y1: snap8(l.y) };
    syncExInputs();
    draw();
    return;
  }
  if (!drag) return;
  const c = eventCell(ev);
  rect = { x0: drag.x0, y0: drag.y0, x1: c.x, y1: c.y };
  syncRectInputs();
  draw();
});
window.addEventListener("mouseup", () => { drag = null; exDrag = null; });

function syncRectInputs() {
  if (!rect) return;
  const r = normalize(rect);
  $("r-x0").value = r.x0; $("r-y0").value = r.y0;
  $("r-x1").value = r.x1; $("r-y1").value = r.y1;
}

/* ---------------- 核量 ---------------- */
async function runSelection(cellwise) {
  const r = {
    x0: Number($("r-x0").value), y0: Number($("r-y0").value),
    x1: Number($("r-x1").value), y1: Number($("r-y1").value),
  };
  if ([r.x0, r.y0, r.x1, r.y1].some((v) => Number.isNaN(v))) {
    setMsg("sel-msg", "请在画布拖选或填写完整的整数格坐标", "err");
    return;
  }
  rect = r;
  draw();
  try {
    const q = new URLSearchParams({ ...r, cellwise: String(cellwise) });
    const res = await api("/api/selection?" + q);
    $("r-area").textContent = `${res.area_cells_m2} m²`;
    $("r-vol").textContent = `${res.net_volume_m3.toFixed(3)}`;
    $("r-blocks").textContent = res.whole_blocks;
    $("r-edge").textContent =
      `${res.edge_cells} 格 (其中可计量 ${res.edge_matched_cells})`;
    $("r-ver").textContent = "v" + res.data_version;
    $("r-mode").textContent = res.mode === "cellwise" ? "全部细格(校验)" : "整块+边缘";
    setMsg(
      "sel-msg",
      `选区 [${res.x0},${res.y0}]–[${res.x1},${res.y1}] 共 `
      + `${(res.x1-res.x0+1)*(res.y1-res.y0+1)} 格; `
      + `整块与边缘取自同一快照 v${res.data_version}`,
      "ok"
    );
  } catch (e) {
    setMsg("sel-msg", "核量失败: " + e.message, "err");
  }
}

$("btn-select").addEventListener("click", () => runSelection(false));
$("btn-cellwise").addEventListener("click", () => runSelection(true));
$("btn-clear").addEventListener("click", () => {
  rect = null; draw();
  ["r-x0", "r-y0", "r-x1", "r-y1"].forEach((id) => ($(id).value = ""));
  setMsg("sel-msg", "", "mut");
});

/* ---------------- 浅点筛查图导出 (GeoTIFF) ---------------- */
function syncExInputs() {
  if (!exRect) return;
  const r = normalize(exRect);
  $("e-x0").value = r.x0; $("e-y0").value = r.y0;
  $("e-x1").value = r.x1; $("e-y1").value = r.y1;
}

$("btn-export").addEventListener("click", async () => {
  const body = {
    x0: Number($("e-x0").value), y0: Number($("e-y0").value),
    x1: Number($("e-x1").value), y1: Number($("e-y1").value),
  };
  if (Object.values(body).some((v) => Number.isNaN(v))) {
    setMsg("ex-msg", "请拖选或填写完整的 8 米格线坐标 (米)", "err");
    return;
  }
  if (Object.values(body).some((v) => v % 8 !== 0)) {
    setMsg("ex-msg", "坐标必须是 8 的整数倍 (沿 8 米格线)", "err");
    return;
  }
  $("btn-export").disabled = true;
  $("ex-link").innerHTML = "";
  try {
    const job = await api("/api/exports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    setMsg("ex-msg", `任务 ${job.id} 已创建, 正在生成…`, "mut");
    pollExport(job.id);
  } catch (e) {
    setMsg("ex-msg", "创建导出失败: " + e.message, "err");
    $("btn-export").disabled = false;
  }
});

async function pollExport(id) {
  try {
    const j = await api("/api/exports/" + id);
    if (j.status === "done") {
      const kb = (j.file_bytes / 1024).toFixed(0);
      setMsg(
        "ex-msg",
        `完成: ${j.width_px}×${j.height_px} 像元, 有效像元 ${j.valid_pixels}, `
        + `数据版本 v${j.data_version}, 文件 ${kb} KB `
        + `(瓦片 ${j.tiles_total} 个, 含像元数据的瓦片已写入)`,
        "ok"
      );
      $("ex-link").innerHTML =
        `<a href="/api/exports/${id}/file">下载 ${j.file_name}</a>`;
      $("btn-export").disabled = false;
    } else if (j.status === "failed") {
      setMsg("ex-msg", "生成失败 (残件已清理): " + j.error, "err");
      $("btn-export").disabled = false;
    } else {
      setMsg("ex-msg", `生成中… 瓦片 ${j.tiles_done}/${j.tiles_total}`, "mut");
      setTimeout(() => pollExport(id), 800);
    }
  } catch (e) {
    setMsg("ex-msg", "查询导出状态失败: " + e.message, "err");
    $("btn-export").disabled = false;
  }
}

$("btn-exclear").addEventListener("click", () => {
  exRect = null; draw();
  ["e-x0", "e-y0", "e-x1", "e-y1"].forEach((id) => ($(id).value = ""));
  setMsg("ex-msg", "", "mut");
  $("ex-link").innerHTML = "";
});

/* ---------------- 批次留痕 ---------------- */
async function loadBatches() {
  const data = await api("/api/batches?limit=20");
  const tb = document.querySelector("#batch-table tbody");
  tb.innerHTML = "";
  for (const b of data.batches) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${b.id}</td><td>${b.phase === "before" ? "前" : "后"}</td>`
      + `<td>${b.survey_no}</td><td>${b.row_count}</td>`
      + `<td class="tag-${b.status}">${b.status === "accepted" ? "接收" : "退回"}</td>`
      + `<td style="text-align:left">${b.reject_reason || ""}</td>`;
    tb.appendChild(tr);
  }
}

(async function init() {
  try {
    const blk = await api("/api/blocks");
    $("bsize").textContent = blk.block_size;
    $("bsize2").textContent = blk.block_size;
    await refresh();
  } catch (e) {
    setMsg("up-msg", "初始化失败: " + e.message, "err");
  }
})();
