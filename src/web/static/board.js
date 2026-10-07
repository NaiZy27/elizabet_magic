/*
 * Доска заказов: перенос карточек между колонками меняет этап,
 * перетаскивание внутри «В очереди» и «Собирается» — место в общей очереди.
 *
 * Перетаскивание на Pointer Events, одинаково мышью и пальцем:
 *  - мышью — потянуть карточку больше чем на 5px;
 *  - пальцем — зажать карточку (~0,35 с), дождаться отклика и вести.
 * Карточка «поднимается» и едет за пальцем, на её месте остаётся пунктир.
 * У краёв экрана доска сама прокручивается — так на телефоне карточка
 * переезжает в соседнюю колонку слайдера.
 * Короткий тап/клик по карточке открывает заказ.
 */
(() => {
  "use strict";

  const board = document.querySelector("[data-board]");
  if (!board) return;

  const shell = document.querySelector("[data-board-shell]");
  const switcher = document.querySelector("[data-col-switch]");
  const toast = document.getElementById("board-toast");
  const csrfToken = board.dataset.csrf;
  const queueStatuses = JSON.parse(board.dataset.queueStatuses || "[]");

  const TOUCH_HOLD_MS = 350;
  const TOUCH_SLOP_PX = 8;
  const MOUSE_THRESHOLD_PX = 5;
  const EDGE_PX = 56;
  const MAX_SCROLL_SPEED = 18;

  let drag = null;
  let saving = false;
  let suppressClick = false;

  const isMobile = () => window.matchMedia("(max-width: 900px)").matches;

  function showToast(message, isError) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle("error", Boolean(isError));
    toast.classList.add("show");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2200);
  }

  const columns = () => [...board.querySelectorAll(".kanban-column")];
  const cardsBox = (column) => column.querySelector(".column-cards");

  function refreshColumn(column) {
    const box = cardsBox(column);
    const count = box.querySelectorAll(".order-card").length;
    const placeholder = box.querySelector(".drop-empty");
    if (count && placeholder) placeholder.remove();
    if (!count && !placeholder) {
      const empty = document.createElement("div");
      empty.className = "drop-empty";
      empty.textContent = "Пусто";
      box.appendChild(empty);
    }
    column.querySelector(".column-count").textContent = count;
    if (switcher) {
      const pill = switcher.querySelector(`[data-target="${column.dataset.status}"] .count`);
      if (pill) pill.textContent = count;
    }
  }

  /** Очередь целиком в видимом порядке: сначала «Собирается», затем «В очереди». */
  function collectQueueIds() {
    const ids = [];
    ["assembling", "queued"].forEach((status) => {
      const column = board.querySelector(`.kanban-column[data-status="${status}"]`);
      if (!column) return;
      column.querySelectorAll(".order-card").forEach((card) => ids.push(Number(card.dataset.orderId)));
    });
    return ids;
  }

  const isQueueColumn = (column) => queueStatuses.includes(column.dataset.status);

  /* ---------- перетаскивание ---------- */

  function beginDrag(card, x, y) {
    const rect = card.getBoundingClientRect();
    const ghost = card.cloneNode(true);
    ghost.classList.add("drag-ghost");
    ghost.classList.remove("pressing");
    ghost.style.width = `${rect.width}px`;
    document.body.appendChild(ghost);

    drag = {
      card,
      ghost,
      fromColumn: card.closest(".kanban-column"),
      fromNext: card.nextElementSibling,
      offsetX: x - rect.left,
      offsetY: y - rect.top,
      x,
      y,
      overColumn: null,
      raf: 0,
    };
    card.classList.remove("pressing");
    card.classList.add("placeholder");
    document.body.classList.add("is-dragging");
    if (shell) shell.classList.add("dragging");
    if (navigator.vibrate) navigator.vibrate(12);
    positionGhost();
    drag.raf = window.requestAnimationFrame(autoScroll);
  }

  function positionGhost() {
    drag.ghost.style.transform = `translate(${drag.x - drag.offsetX}px, ${drag.y - drag.offsetY}px)`;
  }

  /** Куда вставить карточку в колонке — по вертикали относительно середин соседей. */
  function insertionPoint(column, y) {
    for (const other of column.querySelectorAll(".order-card:not(.placeholder)")) {
      const box = other.getBoundingClientRect();
      if (y < box.top + box.height / 2) return other;
    }
    return null;
  }

  function trackPointer() {
    const under = document.elementFromPoint(drag.x, drag.y);
    const column = under && under.closest(".kanban-column");
    if (column !== drag.overColumn) {
      if (drag.overColumn) drag.overColumn.classList.remove("drag-over");
      if (column && column !== drag.fromColumn) column.classList.add("drag-over");
      drag.overColumn = column;
    }
    if (!column) return;
    const box = cardsBox(column);
    const before = insertionPoint(column, drag.y);
    if (before) {
      if (before !== drag.card.nextElementSibling) box.insertBefore(drag.card, before);
    } else if (box.lastElementChild !== drag.card) {
      box.appendChild(drag.card);
    }
    const empty = box.querySelector(".drop-empty");
    if (empty) empty.hidden = true;
  }

  /** Прокрутка у краёв: по горизонтали — слайдер колонок, по вертикали — страница. */
  function autoScroll() {
    if (!drag) return;
    let moved = false;
    if (shell) {
      const rect = shell.getBoundingClientRect();
      const left = drag.x - rect.left;
      const right = rect.right - drag.x;
      if (left < EDGE_PX) {
        shell.scrollLeft -= speed(left);
        moved = true;
      } else if (right < EDGE_PX) {
        shell.scrollLeft += speed(right);
        moved = true;
      }
    }
    if (drag.y < EDGE_PX + 40) {
      window.scrollBy(0, -speed(drag.y - 40));
      moved = true;
    } else if (window.innerHeight - drag.y < EDGE_PX + 80) {
      window.scrollBy(0, speed(window.innerHeight - drag.y - 80));
      moved = true;
    }
    if (moved) trackPointer();
    drag.raf = window.requestAnimationFrame(autoScroll);
  }

  function speed(distance) {
    const k = 1 - Math.max(0, Math.min(distance, EDGE_PX)) / EDGE_PX;
    return Math.ceil(MAX_SCROLL_SPEED * k);
  }

  async function finishDrag(cancelled) {
    const { card, ghost, fromColumn, fromNext } = drag;
    window.cancelAnimationFrame(drag.raf);
    if (drag.overColumn) drag.overColumn.classList.remove("drag-over");
    drag = null;

    ghost.remove();
    card.classList.remove("placeholder");
    document.body.classList.remove("is-dragging");
    if (shell) shell.classList.remove("dragging");
    board.querySelectorAll(".drop-empty[hidden]").forEach((el) => (el.hidden = false));

    if (cancelled) {
      putBack(card, fromColumn, fromNext);
      refreshColumn(fromColumn);
      return;
    }

    const toColumn = card.closest(".kanban-column");
    if (!toColumn) return;
    if (isMobile()) snapTo(toColumn);

    const statusChanged = toColumn !== fromColumn;
    const orderChanged = fromNext !== card.nextElementSibling;
    if (!statusChanged && !orderChanged) {
      refreshColumn(fromColumn);
      return;
    }

    refreshColumn(fromColumn);
    refreshColumn(toColumn);

    saving = true;
    try {
      const response = await fetch("/api/board/move", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({
          order_id: Number(card.dataset.orderId),
          status: toColumn.dataset.status,
          queue_ids: isQueueColumn(toColumn) || isQueueColumn(fromColumn) ? collectQueueIds() : [],
        }),
      });

      if (response.status === 409) {
        showToast("Доска устарела, обновляем…", true);
        window.setTimeout(() => window.location.reload(), 1200);
        return;
      }
      if (!response.ok) throw new Error(await readError(response));

      applyServerState(await response.json());
      showToast(statusChanged ? `Перенесено: ${toColumn.querySelector(".column-title").textContent.trim()}` : "Очередь сохранена");
    } catch (error) {
      // Возвращаем карточку на место: доска должна показывать то, что в базе.
      putBack(card, fromColumn, fromNext);
      refreshColumn(fromColumn);
      refreshColumn(toColumn);
      showToast(shortMessage(error), true);
    } finally {
      saving = false;
    }
  }

  function putBack(card, column, next) {
    if (next && next.parentElement) cardsBox(column).insertBefore(card, next);
    else cardsBox(column).appendChild(card);
  }

  async function readError(response) {
    const text = await response.text();
    try {
      const data = JSON.parse(text);
      if (typeof data.detail === "string") return data.detail;
    } catch {
      /* не JSON */
    }
    return text || "Не удалось сохранить";
  }

  function shortMessage(error) {
    const text = String(error && error.message ? error.message : error);
    return text.length > 120 ? "Не удалось сохранить. Карточка возвращена." : text;
  }

  /** Обновить номера очереди и расчётные даты во всех карточках. */
  function applyServerState(data) {
    const positions = data.positions || {};
    const readyDates = data.ready_dates || {};
    const late = data.late || {};

    board.querySelectorAll(".order-card").forEach((card) => {
      const id = card.dataset.orderId;
      const positionBox = card.querySelector(".queue-position");
      if (positionBox) {
        positionBox.textContent = positions[id] != null ? positions[id] : "";
        positionBox.hidden = positions[id] == null;
      }
      const readyBox = card.querySelector(".order-ready");
      if (readyBox) {
        if (readyDates[id]) {
          readyBox.textContent = `к ${readyDates[id]}`;
          readyBox.hidden = false;
          readyBox.classList.toggle("late", Boolean(late[id]));
        } else {
          readyBox.hidden = true;
        }
      }
    });
  }

  board.addEventListener("pointerdown", (event) => {
    if (event.button != null && event.button !== 0) return;
    const card = event.target.closest(".order-card");
    if (!card || saving || drag) return;

    const touch = event.pointerType !== "mouse";
    const startX = event.clientX;
    const startY = event.clientY;
    let holdTimer = null;

    const onMove = (e) => {
      if (drag) {
        e.preventDefault();
        drag.x = e.clientX;
        drag.y = e.clientY;
        positionGhost();
        trackPointer();
        return;
      }
      const dx = Math.abs(e.clientX - startX);
      const dy = Math.abs(e.clientY - startY);
      if (touch) {
        // Палец сдвинулся раньше, чем сработало зажатие — это прокрутка, не перенос.
        if (dx > TOUCH_SLOP_PX || dy > TOUCH_SLOP_PX) stop();
        return;
      }
      if (dx > MOUSE_THRESHOLD_PX || dy > MOUSE_THRESHOLD_PX) beginDrag(card, e.clientX, e.clientY);
    };

    const onUp = (e) => {
      const wasDragging = Boolean(drag);
      stop();
      if (wasDragging) {
        suppressClick = true;
        finishDrag(e.type === "pointercancel");
      }
    };

    const stop = () => {
      if (holdTimer) window.clearTimeout(holdTimer);
      holdTimer = null;
      card.classList.remove("pressing");
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };

    if (touch) {
      card.classList.add("pressing");
      holdTimer = window.setTimeout(() => {
        holdTimer = null;
        beginDrag(card, startX, startY);
      }, TOUCH_HOLD_MS);
    }

    window.addEventListener("pointermove", onMove, { passive: false });
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  });

  // Пока карточка в руке, страница не должна прокручиваться пальцем.
  document.addEventListener(
    "touchmove",
    (event) => {
      if (drag) event.preventDefault();
    },
    { passive: false }
  );

  // Долгое нажатие не должно открывать системное меню ссылки.
  board.addEventListener("contextmenu", (event) => {
    if (event.target.closest(".order-card")) event.preventDefault();
  });

  board.addEventListener("dragstart", (event) => event.preventDefault());

  // Короткий клик в любом месте карточки открывает заказ, перетаскивание — нет.
  board.addEventListener(
    "click",
    (event) => {
      if (suppressClick) {
        suppressClick = false;
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      const card = event.target.closest(".order-card");
      if (!card || event.target.closest("a")) return;
      if (event.metaKey || event.ctrlKey) window.open(card.dataset.href, "_blank");
      else window.location.href = card.dataset.href;
    },
    true
  );

  /* ---------- слайдер колонок на телефоне ---------- */

  function snapTo(column) {
    if (!shell) return;
    shell.scrollTo({ left: column.offsetLeft - parseFloat(getComputedStyle(shell).paddingLeft), behavior: "smooth" });
  }

  if (switcher && shell) {
    switcher.addEventListener("click", (event) => {
      const button = event.target.closest("[data-target]");
      if (!button) return;
      const column = board.querySelector(`.kanban-column[data-status="${button.dataset.target}"]`);
      if (column) snapTo(column);
    });

    const markActive = () => {
      if (!isMobile()) return;
      const shellLeft = shell.getBoundingClientRect().left;
      let best = null;
      let bestDistance = Infinity;
      columns().forEach((column) => {
        const distance = Math.abs(column.getBoundingClientRect().left - shellLeft - 16);
        if (distance < bestDistance) {
          bestDistance = distance;
          best = column;
        }
      });
      if (!best) return;
      switcher.querySelectorAll("[data-target]").forEach((button) => {
        const on = button.dataset.target === best.dataset.status;
        if (on && !button.classList.contains("on")) {
          button.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
        }
        button.classList.toggle("on", on);
      });
    };

    let ticking = false;
    shell.addEventListener(
      "scroll",
      () => {
        if (ticking) return;
        ticking = true;
        window.requestAnimationFrame(() => {
          ticking = false;
          markActive();
        });
      },
      { passive: true }
    );
    markActive();
  }

  columns().forEach(refreshColumn);
})();
