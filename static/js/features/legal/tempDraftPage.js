/**
 * Bộ điều khiển trang nháp độc lập `/temp` với Step-up Auth (60s idle auto-lock) và True DOM Destruction.
 * - Tuyệt đối không tải nội dung `/temp` khi chưa xác thực Step-up.
 * - Khi hết 60s hoặc khóa thủ công: xóa sạch toàn bộ DOM bằng `replaceChildren()` và mở lại modal xác thực.
 */

import { setStepUpRequiredHandler } from "../../core/api.js";
import { initCustomSelects } from "../../core/customSelect.js";
import { createStepUpController } from "../../core/stepUp.js";
import { initThemeSwitcher } from "../../core/themeSwitcher.js";
import { createLegalCmsTabController } from "./legalCmsTab.js";

function readBootstrapData() {
  const el = document.getElementById("dashboard-bootstrap");
  if (!el) {
    return {
      isAdmin: false,
      authType: "password",
      discordOauthEnabled: false,
      stepUpTimeoutSeconds: 60,
      activeTosVersion: "v1.0",
    };
  }
  return JSON.parse(el.textContent || "{}");
}

const bootstrap = readBootstrapData();
const isAdmin = Boolean(bootstrap.isAdmin);
let activeController = null;

function renderLockedState(onUnlockClick) {
  const root = document.getElementById("temp-content-root");
  if (!root) return;
  root.replaceChildren();

  const card = document.createElement("div");
  card.className = "stepup-lock-card text-center max-w-lg mx-auto space-y-4";

  const iconWrap = document.createElement("div");
  iconWrap.className = "stepup-shield-icon inline-flex items-center justify-center w-12 h-12 mx-auto";
  iconWrap.setAttribute("aria-hidden", "true");
  iconWrap.innerHTML = `
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
      <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
    </svg>
  `;

  const title = document.createElement("h2");
  title.className = "text-base font-semibold stepup-text-primary";
  title.textContent = "Trang nháp (/temp) đang được khóa";

  const desc = document.createElement("p");
  desc.className = "text-xs stepup-text-muted leading-relaxed";
  desc.textContent =
    "Nội dung nháp được bảo vệ bằng Step-up Auth và tự hủy hoàn toàn khỏi bộ nhớ và DOM sau 60 giây nhàn rỗi.";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn-primary stepup-btn-radius text-xs";
  btn.textContent = "🔓 Xác thực để mở khóa /temp";
  btn.addEventListener("click", onUnlockClick);

  card.appendChild(iconWrap);
  card.appendChild(title);
  card.appendChild(desc);
  card.appendChild(btn);
  root.appendChild(card);
}

function mountTempEditorDom() {
  const root = document.getElementById("temp-content-root");
  if (!root) return;
  root.replaceChildren();

  const disabledAttr = isAdmin ? "" : "disabled";
  const toolbarDisabledClass = isAdmin ? "" : "opacity-60 pointer-events-none";
  const activeVer = bootstrap.activeTosVersion || "v1.0";

  root.innerHTML = `
    <div class="card p-5 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-subtle)] pb-3">
        <div>
          <h2 class="font-semibold text-sm text-rose-400">Trình soạn thảo Rich-Text · Trang nháp (/temp)</h2>
          <p class="text-[11px] text-[#8b95a7] mt-0.5">Lưu trực tiếp trên MongoDB (0 MB RAM đệm) · Đẩy sang 1 trong 5 trang công khai khi sẵn sàng.</p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <span id="legal-active-version-badge" class="text-[11px] font-mono font-semibold px-2.5 py-1 rounded-full bg-rose-500/15 text-rose-300 border border-rose-500/35">Phiên bản: ${activeVer}</span>
          <span id="legal-updated-badge" class="text-[11px] font-mono px-2.5 py-1 rounded-full bg-[var(--bg-elevated)] text-[var(--text-secondary)] border border-[var(--border-subtle)]">Cập nhật: —</span>
        </div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-12 gap-3">
        <div class="lg:col-span-5">
          <label for="legal-doc-title" class="block text-xs text-[#8b95a7] mb-1">Tiêu đề trang nháp</label>
          <input id="legal-doc-title" type="text" maxlength="160" class="inp" placeholder="Nhập tiêu đề bản nháp..." ${disabledAttr} />
        </div>
        <div class="lg:col-span-4">
          <label for="legal-doc-subtitle" class="block text-xs text-[#8b95a7] mb-1">Mô tả ngắn (phụ đề)</label>
          <input id="legal-doc-subtitle" type="text" maxlength="240" class="inp" placeholder="Tóm tắt ngắn..." ${disabledAttr} />
        </div>
        <div class="lg:col-span-3">
          <label for="legal-tos-version" class="block text-xs text-[#8b95a7] mb-1">Phiên bản ToS khi đẩy</label>
          <input id="legal-tos-version" type="text" value="${activeVer}" placeholder="v1.0" class="inp font-mono" ${disabledAttr} />
        </div>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-2 pt-1">
        <div class="inline-flex items-center p-0.5 rounded-lg bg-[var(--bg-input)] border border-[var(--border-subtle)]" role="group" aria-label="Chế độ soạn thảo">
          <button type="button" id="btn-rte-mode-edit" class="ox-theme-btn" aria-pressed="true">✏️ Soạn thảo</button>
          <button type="button" id="btn-rte-mode-preview" class="ox-theme-btn" aria-pressed="false">👁️ Xem trước</button>
        </div>
        <div class="flex items-center gap-2 text-[11px] font-mono text-[#8b95a7]">
          <span id="legal-current-route-badge" class="text-rose-400">/temp</span>
          <span>·</span>
          <span id="legal-body-chars">0 ký tự HTML</span>
        </div>
      </div>

      <div id="legal-rte-container" class="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-base)] overflow-hidden">
        <div id="legal-rte-toolbar" class="ox-rte-toolbar ${toolbarDisabledClass}">
          <div class="relative" id="rte-font-picker">
            <button type="button" id="rte-font-family" class="rt-font-trigger" aria-haspopup="listbox" aria-expanded="false" title="Chọn phông chữ">
              <span id="rte-font-trigger-label" class="truncate">Sans-serif (Inter)</span>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><polyline points="6 9 12 15 18 9"></polyline></svg>
            </button>
            <div id="rte-font-popover" class="rt-popover hidden" role="listbox" aria-label="Danh sách phông chữ">
              <div class="px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-wider stepup-text-dim">Phông chữ có sẵn</div>
              <div id="rte-builtin-fonts-list" class="space-y-0.5"></div>
              <div id="rte-custom-fonts-section" class="hidden mt-1.5 pt-1.5 border-t border-[var(--border-subtle)]">
                <div class="px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider stepup-text-dim">Google Fonts đã thêm</div>
                <div id="rte-custom-fonts-list" class="space-y-0.5"></div>
              </div>
              <div class="mt-2 pt-2 border-t border-[var(--border-subtle)]">
                <button type="button" id="btn-open-google-font-modal" class="btn-primary rt-btn w-full text-xs py-1.5 flex items-center justify-center gap-1.5"><span>+ Thêm Google Font</span></button>
              </div>
            </div>
          </div>

          <select id="rte-block-format" class="ox-rte-select min-w-[152px]" title="Kiểu đoạn / Tiêu đề" aria-label="Kiểu đoạn">
            <option value="p">Đoạn văn (&lt;p&gt;)</option>
            <option value="h1">Tiêu đề H1</option>
            <option value="h2">Tiêu đề H2</option>
            <option value="h3">Tiêu đề H3</option>
            <option value="h4">Tiêu đề H4</option>
          </select>

          <div class="rt-fontsize-group flex items-center gap-1" title="Cỡ chữ tùy ý (10–72px)">
            <button type="button" id="rte-fontsize-dec" class="ox-rte-btn w-7 px-0" aria-label="Giảm cỡ chữ" title="Giảm 1px">−</button>
            <div class="rt-fontsize-input-wrap flex items-center">
              <input id="rte-font-size" type="text" inputmode="numeric" autocomplete="off" aria-label="Cỡ chữ (px)" placeholder="14" class="rt-fontsize-input font-mono" />
              <span class="rt-fontsize-unit font-mono">px</span>
            </div>
            <button type="button" id="rte-fontsize-inc" class="ox-rte-btn w-7 px-0" aria-label="Tăng cỡ chữ" title="Tăng 1px">+</button>
          </div>

          <span class="ox-rte-sep" aria-hidden="true"></span>
          <button type="button" class="ox-rte-btn font-bold" data-rte-cmd="bold" title="In đậm (Ctrl+B)">B</button>
          <button type="button" class="ox-rte-btn italic" data-rte-cmd="italic" title="In nghiêng (Ctrl+I)">I</button>
          <button type="button" class="ox-rte-btn underline" data-rte-cmd="underline" title="Gạch chân (Ctrl+U)">U</button>
          <button type="button" class="ox-rte-btn line-through" data-rte-cmd="strikeThrough" title="Gạch ngang">S</button>
          <label class="ox-rte-btn cursor-pointer gap-1" title="Màu chữ">
            <span>A</span>
            <input id="rte-text-color" type="color" value="#f1f5f9" class="w-4 h-4 p-0 border-0 bg-transparent cursor-pointer" aria-label="Màu chữ" />
          </label>
          <span class="ox-rte-sep" aria-hidden="true"></span>
          <button type="button" class="ox-rte-btn" data-rte-cmd="justifyLeft" title="Căn trái">⬅</button>
          <button type="button" class="ox-rte-btn" data-rte-cmd="justifyCenter" title="Căn giữa">⬌</button>
          <button type="button" class="ox-rte-btn" data-rte-cmd="justifyRight" title="Căn phải">➡</button>
          <button type="button" class="ox-rte-btn" data-rte-cmd="justifyFull" title="Căn đều">☰</button>
          <span class="ox-rte-sep" aria-hidden="true"></span>
          <button type="button" class="ox-rte-btn" data-rte-cmd="insertUnorderedList" title="Danh sách chấm tròn">• List</button>
          <button type="button" class="ox-rte-btn" data-rte-cmd="insertOrderedList" title="Danh sách đánh số">1. List</button>
          <button type="button" class="ox-rte-btn" data-rte-action="blockquote" title="Trích dẫn (Blockquote)">❝</button>
          <button type="button" class="ox-rte-btn" data-rte-cmd="insertHorizontalRule" title="Đường kẻ ngang">―</button>
          <button type="button" class="ox-rte-btn" id="btn-rte-toggle-link" title="Chèn / sửa liên kết">🔗 Link</button>
          <span class="ox-rte-sep" aria-hidden="true"></span>
          <button type="button" class="ox-rte-btn" data-rte-cmd="undo" title="Hoàn tác (Ctrl+Z)">↶</button>
          <button type="button" class="ox-rte-btn" data-rte-cmd="redo" title="Làm lại (Ctrl+Y)">↷</button>
          <button type="button" class="ox-rte-btn" data-rte-cmd="removeFormat" title="Xóa định dạng">🧹</button>
        </div>

        <div id="rte-link-builder" class="hidden px-3 py-2.5 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)] flex flex-wrap items-center gap-2 text-xs">
          <select id="rte-link-preset" class="ox-rte-select">
            <option value="">— Chọn nhanh tuyến nội bộ —</option>
            <option value="/toslinks">/toslinks (Cổng danh mục)</option>
            <option value="/legal">/legal (Pháp lý)</option>
            <option value="/terms">/terms (Điều khoản sử dụng)</option>
            <option value="/policies">/policies (Chính sách bảo mật)</option>
            <option value="/guidelines">/guidelines (Chính sách cộng đồng)</option>
            <option value="/me">/me (Cổng người dùng)</option>
          </select>
          <input id="rte-link-url" type="text" placeholder="Hoặc nhập https://... hay /route" class="inp text-xs flex-1 min-w-[180px] py-1" />
          <input id="rte-link-text" type="text" placeholder="Nhãn hiển thị (tùy chọn)" class="inp text-xs w-44 py-1" />
          <button type="button" id="btn-rte-apply-link" class="btn-primary text-xs py-1 px-2.5">Chèn liên kết</button>
          <button type="button" id="btn-rte-cancel-link" class="btn-ghost text-xs py-1 px-2">Đóng</button>
        </div>

        <div id="legal-rte-editor" class="ox-rte-editor ox-legal-prose" contenteditable="${isAdmin ? "true" : "false"}" role="textbox" aria-multiline="true" aria-label="Nội dung Rich-Text trang nháp"></div>
      </div>

      <div id="legal-rte-preview-wrap" class="hidden rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-base)] p-5 space-y-3">
        <div class="border-b border-[var(--border-subtle)] pb-3 space-y-1">
          <span id="legal-preview-version" class="inline-block text-[10px] font-mono px-2 py-0.5 rounded-full bg-rose-500/15 text-rose-400 border border-rose-500/35">${activeVer}</span>
          <h3 id="legal-preview-title" class="text-base font-bold">—</h3>
          <p id="legal-preview-subtitle" class="text-xs text-[var(--text-secondary)]"></p>
        </div>
        <div id="legal-preview-body" class="ox-legal-prose text-sm leading-7"></div>
      </div>

      ${
        isAdmin
          ? `<div class="flex flex-wrap items-center justify-between gap-2 pt-1">
              <div id="legal-temp-push-wrap" class="flex flex-wrap items-center gap-2">
                <select id="legal-temp-push-target" class="ox-rte-select text-xs" aria-label="Chọn trang đích để ghi đè">
                  <option value="toslinks">/toslinks · Danh mục</option>
                  <option value="legal">/legal · Pháp lý</option>
                  <option value="terms">/terms · Điều khoản</option>
                  <option value="policies">/policies · Bảo mật</option>
                  <option value="guidelines">/guidelines · Cộng đồng</option>
                </select>
                <button type="button" id="btn-push-temp-legal" class="btn-primary text-xs">🚀 Đẩy sang trang</button>
              </div>
              <div class="flex flex-wrap items-center justify-end gap-2 ml-auto">
                <button type="button" id="btn-reload-legal" class="btn-ghost text-xs">↻ Tải lại từ MongoDB</button>
                <button type="button" id="btn-save-legal" class="btn-primary text-xs">💾 Lưu bản nháp /temp</button>
              </div>
            </div>`
          : ""
      }
    </div>
  `;
}

const stepUp = createStepUpController({
  authType: bootstrap.authType || "password",
  timeoutSeconds: Number(bootstrap.stepUpTimeoutSeconds) || 60,
  getActiveTab: () => "temp-page",
  onLockAll: () => {
    activeController = null;
    renderLockedState(() => stepUp.openModal("temp-page"));
  },
  onUnlockedTab: async (_tab, afterUnlock) => {
    mountTempEditorDom();
    activeController = createLegalCmsTabController({
      isAdmin,
      isStepUpUnlocked: () => stepUp.isUnlocked(),
      onRequestUnlock: (_t, opts) => stepUp.openModal("temp-page", opts),
    });
    // Khởi tạo editor và tải nội dung /temp từ MongoDB sau khi xác thực thành công
    activeController.initEditor();
    await activeController.loadPage("temp");
    if (typeof afterUnlock === "function") afterUnlock();
  },
});

setStepUpRequiredHandler(() => {
  stepUp.lock({ notifyServer: false, openModalIfSensitive: true, reason: "server_403" });
});

async function initTempDraftPage() {
  initThemeSwitcher();
  initCustomSelects();
  stepUp.bindEvents();
  renderLockedState(() => stepUp.openModal("temp-page"));

  const cb = stepUp.handleCallbackQueryParams();
  const unlockedNow = await stepUp.syncStatusFromServer("temp-page");
  if (!unlockedNow) {
    stepUp.openModal("temp-page", { errorMessage: cb.errorMessage || "" });
  }
}

initTempDraftPage();
