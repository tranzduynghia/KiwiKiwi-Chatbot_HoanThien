/**
 * Thành phần Custom Dropdown Popover đồng bộ 100% với bộ chọn phông chữ Rich-Text.
 * - Giữ nguyên thẻ <select> gốc (id, name, value, data-*, disabled, sự kiện change)
 *   để tương thích tuyệt đối với mọi form, bộ lọc và API hiện có.
 * - Hỗ trợ tự động căn vị trí/chiều rộng không tràn viewport, nhóm <optgroup>,
 *   điều hướng bàn phím (ArrowUp/Down/Home/End/Enter/Space/Esc), và bảo toàn
 *   vùng chọn văn bản (Range) khi dùng trong trình soạn thảo Rich-Text.
 */

const NATIVE_VALUE_DESC = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
const NATIVE_INDEX_DESC = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "selectedIndex");

let activeInstance = null;
let globalObserverInitialized = false;
let globalListenersInitialized = false;

function isCompactSelect(selectEl) {
  if (!selectEl) return false;
  if (selectEl.classList.contains("ox-rte-select")) return true;
  if (selectEl.classList.contains("text-xs")) return true;
  if (selectEl.classList.contains("py-1") || selectEl.classList.contains("py-1.5")) return true;
  if (selectEl.dataset.field === "tag_preset") return true;
  return false;
}

function shouldPreserveSelection(selectEl) {
  return Boolean(selectEl.closest(".ox-rte-toolbar") || selectEl.dataset.preserveSelection === "true");
}

function getSelectedOption(selectEl) {
  if (!selectEl || !selectEl.options || selectEl.options.length === 0) return null;
  const idx = selectEl.selectedIndex >= 0 ? selectEl.selectedIndex : 0;
  return selectEl.options[idx] || selectEl.options[0] || null;
}

function getSelectedLabel(selectEl) {
  const opt = getSelectedOption(selectEl);
  return opt ? opt.textContent.trim() : "";
}

function copyLayoutClasses(selectEl, wrapEl) {
  const layoutPrefixes = ["w-", "min-w-", "max-w-", "flex-", "shrink", "grow", "sm:", "md:", "lg:"];
  const preserved = [];
  selectEl.classList.forEach((cls) => {
    if (cls === "w-full") return;
    if (layoutPrefixes.some((p) => cls.startsWith(p))) {
      preserved.push(cls);
    }
  });
  if (preserved.length) {
    wrapEl.classList.add(...preserved);
  }
  if (selectEl.classList.contains("inp") || selectEl.classList.contains("stepup-select")) {
    if (!preserved.some((c) => c.startsWith("w-") && c !== "w-auto")) {
      wrapEl.classList.add("ox-select-wrap--block");
    }
  }
}

function closeActiveInstance(restoreFocus = false) {
  if (!activeInstance) return;
  const inst = activeInstance;
  activeInstance = null;
  inst.close(restoreFocus);
}

function positionPopover(triggerEl, popoverEl) {
  const rect = triggerEl.getBoundingClientRect();
  const margin = 8;
  const gap = 6;
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  const spaceBelow = Math.max(0, vh - rect.bottom - gap - margin);
  const spaceAbove = Math.max(0, rect.top - gap - margin);
  const openUpward = spaceBelow < 180 && spaceAbove > spaceBelow;
  const availableHeight = openUpward ? spaceAbove : spaceBelow;
  const maxHeight = Math.max(120, Math.min(300, availableHeight));

  const minW = Math.max(rect.width, 160);
  const maxW = Math.max(160, vw - margin * 2);

  popoverEl.style.minWidth = `${Math.min(minW, maxW)}px`;
  popoverEl.style.maxWidth = `${maxW}px`;
  popoverEl.style.maxHeight = `${maxHeight}px`;

  // Đo chiều rộng thực tế sau khi hiển thị để căn mép trái không tràn màn hình
  const popRect = popoverEl.getBoundingClientRect();
  const popW = Math.min(Math.max(popRect.width, minW), maxW);
  let left = rect.left;
  if (left + popW > vw - margin) {
    left = Math.max(margin, vw - margin - popW);
  }
  if (left < margin) left = margin;

  popoverEl.style.left = `${Math.round(left)}px`;
  if (openUpward) {
    popoverEl.style.top = "auto";
    popoverEl.style.bottom = `${Math.round(vh - rect.top + gap)}px`;
    popoverEl.dataset.placement = "top";
  } else {
    popoverEl.style.bottom = "auto";
    popoverEl.style.top = `${Math.round(rect.bottom + gap)}px`;
    popoverEl.dataset.placement = "bottom";
  }
}

function initGlobalListeners() {
  if (globalListenersInitialized) return;
  globalListenersInitialized = true;

  document.addEventListener("pointerdown", (e) => {
    if (!activeInstance) return;
    const { wrap, popover } = activeInstance;
    if (wrap.contains(e.target) || popover.contains(e.target)) return;
    closeActiveInstance(false);
  });

  document.addEventListener("keydown", (e) => {
    if (!activeInstance) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      closeActiveInstance(true);
    }
  });

  window.addEventListener(
    "resize",
    () => {
      if (activeInstance) {
        positionPopover(activeInstance.trigger, activeInstance.popover);
      }
    },
    { passive: true },
  );

  window.addEventListener(
    "scroll",
    (e) => {
      if (!activeInstance) return;
      if (activeInstance.popover.contains(e.target)) return;
      positionPopover(activeInstance.trigger, activeInstance.popover);
    },
    { passive: true, capture: true },
  );
}

export function enhanceSelect(selectEl) {
  if (!selectEl || selectEl.tagName !== "SELECT") return null;
  if (selectEl.dataset.oxSelectEnhanced === "1") return null;

  initGlobalListeners();
  selectEl.dataset.oxSelectEnhanced = "1";

  const compact = isCompactSelect(selectEl);
  const preserveSelection = shouldPreserveSelection(selectEl);

  const wrap = document.createElement("div");
  wrap.className = "ox-select-wrap";
  copyLayoutClasses(selectEl, wrap);

  const parent = selectEl.parentNode;
  if (parent) {
    parent.insertBefore(wrap, selectEl);
  }
  wrap.appendChild(selectEl);

  selectEl.classList.add("ox-select-native");
  selectEl.tabIndex = -1;
  selectEl.setAttribute("aria-hidden", "true");

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = compact ? "ox-select-trigger ox-select-trigger--compact" : "ox-select-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");

  const labelEl = document.createElement("span");
  labelEl.className = "ox-select-label";

  const chevronSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  chevronSvg.setAttribute("width", "13");
  chevronSvg.setAttribute("height", "13");
  chevronSvg.setAttribute("viewBox", "0 0 24 24");
  chevronSvg.setAttribute("fill", "none");
  chevronSvg.setAttribute("stroke", "currentColor");
  chevronSvg.setAttribute("stroke-width", "2");
  chevronSvg.setAttribute("aria-hidden", "true");
  chevronSvg.classList.add("ox-select-chevron");
  const poly = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  poly.setAttribute("points", "6 9 12 15 18 9");
  chevronSvg.appendChild(poly);

  trigger.appendChild(labelEl);
  trigger.appendChild(chevronSvg);
  wrap.appendChild(trigger);

  const popover = document.createElement("div");
  popover.className = "ox-select-popover hidden";
  popover.setAttribute("role", "listbox");
  if (preserveSelection) {
    popover.dataset.preserveSelection = "true";
  }

  let isOpen = false;
  let focusedIndex = -1;
  let optionButtons = [];
  let typeBuffer = "";
  let typeTimer = null;

  function syncTriggerState() {
    const label = getSelectedLabel(selectEl);
    labelEl.textContent = label;
    const baseTitle = selectEl.getAttribute("title");
    trigger.title = baseTitle && baseTitle !== label ? `${baseTitle}: ${label}` : label;
    const ariaLabel = selectEl.getAttribute("aria-label");
    if (ariaLabel) {
      trigger.setAttribute("aria-label", ariaLabel);
      popover.setAttribute("aria-label", ariaLabel);
    }
    const isDisabled = Boolean(selectEl.disabled);
    trigger.disabled = isDisabled;
    trigger.setAttribute("aria-disabled", String(isDisabled));
    wrap.classList.toggle("is-disabled", isDisabled);
    if (isDisabled && isOpen) {
      closePopover(false);
    }
  }

  function highlightFocused(index) {
    if (!optionButtons.length) {
      focusedIndex = -1;
      return;
    }
    const clamped = Math.max(0, Math.min(optionButtons.length - 1, index));
    focusedIndex = clamped;
    optionButtons.forEach((btn, i) => {
      btn.classList.toggle("is-focused", i === clamped);
    });
    const target = optionButtons[clamped];
    if (target) {
      target.scrollIntoView({ block: "nearest" });
    }
  }

  function buildOptionButton(optEl) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ox-select-option";
    btn.setAttribute("role", "option");
    btn.dataset.value = optEl.value;
    btn.disabled = Boolean(optEl.disabled);

    const isSelected = optEl.selected;
    btn.setAttribute("aria-selected", String(isSelected));
    if (isSelected) btn.classList.add("is-selected");

    const textSpan = document.createElement("span");
    textSpan.className = "ox-select-option-text";
    textSpan.textContent = optEl.textContent.trim();
    btn.title = textSpan.textContent;

    const checkSpan = document.createElement("span");
    checkSpan.className = "ox-select-check";
    checkSpan.setAttribute("aria-hidden", "true");
    checkSpan.textContent = isSelected ? "✓" : "";

    btn.appendChild(textSpan);
    btn.appendChild(checkSpan);

    btn.addEventListener("mousedown", (e) => {
      if (preserveSelection) e.preventDefault();
    });

    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (optEl.disabled) return;
      selectOptionByElement(optEl);
    });

    return btn;
  }

  function renderPopoverOptions() {
    popover.replaceChildren();
    optionButtons = [];
    focusedIndex = -1;

    const children = Array.from(selectEl.children);
    children.forEach((child) => {
      if (child.tagName === "OPTGROUP") {
        const groupLabel = document.createElement("div");
        groupLabel.className = "ox-select-group-label";
        groupLabel.textContent = child.label || "";
        popover.appendChild(groupLabel);
        Array.from(child.children).forEach((opt) => {
          if (opt.tagName !== "OPTION") return;
          const btn = buildOptionButton(opt);
          optionButtons.push(btn);
          popover.appendChild(btn);
        });
      } else if (child.tagName === "OPTION") {
        const btn = buildOptionButton(child);
        optionButtons.push(btn);
        popover.appendChild(btn);
      }
    });

    const selectedIdx = optionButtons.findIndex((b) => b.classList.contains("is-selected"));
    if (selectedIdx >= 0) {
      focusedIndex = selectedIdx;
      optionButtons[selectedIdx].classList.add("is-focused");
    }
  }

  function selectOptionByElement(optEl) {
    const prevValue = NATIVE_VALUE_DESC.get.call(selectEl);
    const nextValue = optEl.value;
    NATIVE_VALUE_DESC.set.call(selectEl, nextValue);
    optEl.selected = true;
    syncTriggerState();
    closePopover(!preserveSelection);
    if (prevValue !== nextValue) {
      selectEl.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  function openPopover() {
    if (selectEl.disabled || isOpen) return;
    if (activeInstance && activeInstance !== instanceApi) {
      closeActiveInstance(false);
    }
    renderPopoverOptions();
    if (!document.body.contains(popover)) {
      document.body.appendChild(popover);
    }
    isOpen = true;
    activeInstance = instanceApi;
    popover.classList.remove("hidden", "is-open");
    positionPopover(trigger, popover);
    trigger.setAttribute("aria-expanded", "true");
    wrap.classList.add("is-open");
    requestAnimationFrame(() => {
      if (!isOpen) return;
      popover.classList.add("is-open");
      const selectedBtn = optionButtons[focusedIndex];
      if (selectedBtn) selectedBtn.scrollIntoView({ block: "nearest" });
    });
  }

  function closePopover(restoreFocus = false) {
    if (!isOpen) return;
    isOpen = false;
    if (activeInstance === instanceApi) {
      activeInstance = null;
    }
    popover.classList.remove("is-open");
    popover.classList.add("hidden");
    if (popover.parentNode === document.body) {
      document.body.removeChild(popover);
    }
    trigger.setAttribute("aria-expanded", "false");
    wrap.classList.remove("is-open");
    if (restoreFocus && !preserveSelection) {
      trigger.focus();
    }
  }

  function moveFocus(delta) {
    if (!optionButtons.length) return;
    let idx = focusedIndex;
    const len = optionButtons.length;
    for (let step = 0; step < len; step += 1) {
      idx = (idx + delta + len) % len;
      if (!optionButtons[idx].disabled) {
        highlightFocused(idx);
        return;
      }
    }
  }

  function handleTypeAhead(key) {
    if (key.length !== 1 || /\s/.test(key)) return;
    typeBuffer += key.toLowerCase();
    clearTimeout(typeTimer);
    typeTimer = setTimeout(() => {
      typeBuffer = "";
    }, 450);

    const matchIdx = optionButtons.findIndex(
      (btn) => !btn.disabled && btn.textContent.trim().toLowerCase().startsWith(typeBuffer),
    );
    if (matchIdx >= 0) {
      highlightFocused(matchIdx);
    }
  }

  trigger.addEventListener("mousedown", (e) => {
    if (preserveSelection) e.preventDefault();
  });

  trigger.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (isOpen) closePopover(false);
    else openPopover();
  });

  trigger.addEventListener("keydown", (e) => {
    if (selectEl.disabled) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!isOpen) openPopover();
      else moveFocus(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!isOpen) openPopover();
      else moveFocus(-1);
    } else if (e.key === "Home" && isOpen) {
      e.preventDefault();
      const first = optionButtons.findIndex((b) => !b.disabled);
      if (first >= 0) highlightFocused(first);
    } else if (e.key === "End" && isOpen) {
      e.preventDefault();
      for (let i = optionButtons.length - 1; i >= 0; i -= 1) {
        if (!optionButtons[i].disabled) {
          highlightFocused(i);
          break;
        }
      }
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (!isOpen) {
        openPopover();
      } else if (focusedIndex >= 0 && optionButtons[focusedIndex] && !optionButtons[focusedIndex].disabled) {
        const targetVal = optionButtons[focusedIndex].dataset.value;
        const matchOpt = Array.from(selectEl.options).find((o) => o.value === targetVal);
        if (matchOpt) selectOptionByElement(matchOpt);
      }
    } else if (isOpen) {
      handleTypeAhead(e.key);
    }
  });

  selectEl.addEventListener("change", () => {
    syncTriggerState();
    if (isOpen) renderPopoverOptions();
  });

  // Ghi đè getter/setter value & selectedIndex trên instance để đồng bộ khi JS gán .value = "..."
  Object.defineProperty(selectEl, "value", {
    configurable: true,
    get() {
      return NATIVE_VALUE_DESC.get.call(this);
    },
    set(val) {
      NATIVE_VALUE_DESC.set.call(this, val);
      syncTriggerState();
      if (isOpen) renderPopoverOptions();
    },
  });

  Object.defineProperty(selectEl, "selectedIndex", {
    configurable: true,
    get() {
      return NATIVE_INDEX_DESC.get.call(this);
    },
    set(idx) {
      NATIVE_INDEX_DESC.set.call(this, idx);
      syncTriggerState();
      if (isOpen) renderPopoverOptions();
    },
  });

  const selectObserver = new MutationObserver(() => {
    syncTriggerState();
    if (isOpen) {
      renderPopoverOptions();
      positionPopover(trigger, popover);
    }
  });
  selectObserver.observe(selectEl, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["disabled", "title", "aria-label", "class"],
  });

  const instanceApi = {
    wrap,
    trigger,
    popover,
    close: closePopover,
  };

  syncTriggerState();
  return instanceApi;
}

export function enhanceAllSelects(root = document) {
  if (!root || typeof root.querySelectorAll !== "function") return;
  const selects = root.querySelectorAll("select:not([data-ox-select-enhanced='1'])");
  selects.forEach((sel) => enhanceSelect(sel));
}

export function initCustomSelects(root = document) {
  enhanceAllSelects(root);
  if (globalObserverInitialized || !document.body) return;
  globalObserverInitialized = true;

  const domObserver = new MutationObserver((mutations) => {
    for (const mut of mutations) {
      for (const node of mut.addedNodes) {
        if (node.nodeType !== 1) continue;
        if (node.tagName === "SELECT") {
          enhanceSelect(node);
        } else if (node.querySelectorAll) {
          enhanceAllSelects(node);
        }
      }
    }
  });
  domObserver.observe(document.body, { childList: true, subtree: true });
}
