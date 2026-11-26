/**
 * Tiện ích chuyển động đóng/mở đối xứng (200ms ease-out) dùng chung cho mọi hộp thoại Modal trên Dashboard:
 * - Lớp phủ nền (backdrop): chuyển mượt `opacity-0` <-> `opacity-100`
 * - Khung hộp thoại (panel): chuyển mượt `opacity-0 scale-95 translate-y-2` <-> `opacity-100 scale-100 translate-y-0`
 * - Khi đóng: gắn `pointer-events-none` chống nhấn đúp, chờ `transitionend` (kèm fallback 220ms) rồi mới gắn lại `hidden`
 * - Chống race-condition bằng token theo từng phần tử khi người dùng đóng rồi mở lại ngay lập tức
 */

const MODAL_TRANSITION_FALLBACK_MS = 220;
const modalStateMap = new WeakMap();

function getModalState(modal) {
  let state = modalStateMap.get(modal);
  if (!state) {
    state = { token: 0, timerId: null, rafId: null, cleanupEnd: null };
    modalStateMap.set(modal, state);
  }
  return state;
}

function clearPendingTransition(state) {
  if (state.timerId !== null) {
    clearTimeout(state.timerId);
    state.timerId = null;
  }
  if (state.rafId !== null && typeof cancelAnimationFrame === "function") {
    cancelAnimationFrame(state.rafId);
    state.rafId = null;
  }
  if (state.cleanupEnd) {
    state.cleanupEnd();
    state.cleanupEnd = null;
  }
}

export function openModalAnimated(modal, panelEl = null) {
  if (!modal) return;
  const panel = panelEl || modal.firstElementChild;
  const state = getModalState(modal);
  clearPendingTransition(state);
  const myToken = ++state.token;

  // Đảm bảo lớp transition và trạng thái đóng ban đầu trước khi bỏ `hidden`
  modal.classList.add("transition-opacity", "duration-200", "ease-out", "opacity-0", "pointer-events-none");
  modal.classList.remove("opacity-100", "hidden");
  modal.classList.add("flex");

  if (panel) {
    panel.classList.remove("ox-modal-enter", "opacity-100", "scale-100", "translate-y-0");
    panel.classList.add(
      "transition-all",
      "duration-200",
      "ease-out",
      "opacity-0",
      "scale-95",
      "translate-y-2"
    );
  }

  // Ép trình duyệt reflow rồi chuyển sang trạng thái mở ở khung hình kế tiếp để hiệu ứng luôn chạy mượt
  void modal.offsetWidth;

  const activate = () => {
    if (state.token !== myToken) return;
    state.rafId = null;
    modal.classList.remove("opacity-0", "pointer-events-none");
    modal.classList.add("opacity-100");
    if (panel) {
      panel.classList.remove("opacity-0", "scale-95", "translate-y-2");
      panel.classList.add("opacity-100", "scale-100", "translate-y-0");
    }
  };

  if (typeof requestAnimationFrame === "function") {
    state.rafId = requestAnimationFrame(activate);
  } else {
    activate();
  }
}

export function closeModalAnimated(modal, panelEl = null, onAfterClose = null) {
  if (!modal) {
    if (typeof onAfterClose === "function") onAfterClose();
    return;
  }
  const panel = panelEl || modal.firstElementChild;
  const state = getModalState(modal);
  clearPendingTransition(state);
  const myToken = ++state.token;

  if (modal.classList.contains("hidden")) {
    if (typeof onAfterClose === "function") onAfterClose();
    return;
  }

  // Khóa tương tác tức thì khi bắt đầu đóng để tránh nhấn đúp kích hoạt lại
  modal.classList.remove("opacity-100");
  modal.classList.add("opacity-0", "pointer-events-none");
  if (panel) {
    panel.classList.remove("opacity-100", "scale-100", "translate-y-0");
    panel.classList.add("opacity-0", "scale-95", "translate-y-2");
  }

  const target = panel || modal;
  let finished = false;

  const finalize = () => {
    if (finished) return;
    finished = true;
    clearPendingTransition(state);
    if (state.token !== myToken) return;
    modal.classList.add("hidden");
    modal.classList.remove("flex");
    if (typeof onAfterClose === "function") onAfterClose();
  };

  const onTransitionEnd = (event) => {
    if (event.target !== target) return;
    finalize();
  };

  target.addEventListener("transitionend", onTransitionEnd);
  state.cleanupEnd = () => target.removeEventListener("transitionend", onTransitionEnd);
  state.timerId = setTimeout(finalize, MODAL_TRANSITION_FALLBACK_MS);
}
