/*
 * Доска заказов: перенос карточек между колонками меняет этап,
 * перетаскивание внутри «В очереди» и «Собирается» — место в общей очереди.
 *
 * Перетаскивание сделано на Pointer Events: одинаково работает мышью и пальцем.
 * На тач-экране драг начинается по долгому нажатию, чтобы короткий тап открывал заказ.
 */
(() => {
  "use strict";

  const board = document.querySelector("[data-board]");
  if (!board) return;

  const toast = document.getElementById("board-toast");
  const csrfToken = board.dataset.csrf;
  const queueStatuses = JSON.parse(board.dataset.queueStatuses || "[]");

  const TOUCH_HOLD_MS = 200;
  const MOUSE_THRESHOLD_PX = 5;

  let drag = null;
  let saving = false;
  let suppressClick = false;

  function showToast(message, isError) {
    if (!toast) return;
    toast.textContent = message;
    toast.style.background = isError ? "#8f2f41" : "#2c1822";
    toast.classList.add("show");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2200);
  }

  function columnsOf() {
    return [...board.querySelectorAll(".kanban-column")];
  }

  function cardsBox(column) {
    return column.querySelector(".column-cards");
  }

  function refreshColumn(column) {
    const box = cardsBox(column);
    const cards = box.querySelectorAll(".order-card");
    const placeholder = box.querySelector(".drop-empty");
    if (cards.length && placeholder) placeholder.remove();
    if (!cards.length && !placeholder) {
      const empty = document.createElement("div");
      empty.className = "drop-empty";
      empty.textContent = "Перетащите заказ сюда";
      box.appendChild(empty);
    }
    column.querySelector(".column-count").textContent = cards.length;
  }

  /** Очередь целиком в видимом порядке: сначала «Собирается», затем «В очереди». */
  function collectQueueIds() {
    const ids = [];
    ["assembling", "queued"].forEach((status) => {
      const column = board.querySelector(`.kanban-column[data-status="${status}"]`);
      if (!column) return;
      column.querySelectorAll(".order-card").forEach((card) => {
        ids.push(Number(card.dataset.orderId));
      });
    });
    return ids;
  }

  function isQueueColumn(column) {
    return queueStatuses.includes(column.dataset.status);
  }

  function beginDrag(card, event) {
    const column = card.closest(".kanban-column");
    drag = {
      card,
      pointerId: event.pointerId,
      fromColumn: column,
      // Куда вернуть карточку, если сохранить не удалось.
      fromNext: card.nextElementSibling,
      active: true,
    };
    card.classList.add("dragging");
    document.body.style.userSelect = "none";
    try {
      card.setPointerCapture(event.pointerId);
    } catch {
      /* захват необязателен */
    }
  }

  /** Куда вставить карточку в колонке — по вертикали относительно середин соседей. */
  function insertionPoint(column, y) {
    const others = [...column.querySelectorAll(".order-card:not(.dragging)")];
    for (const other of others) {
      const box = other.getBoundingClientRect();
      if (y < box.top + box.height / 2) return other;
    }
    return null;
  }

  function moveTo(column, y) {
    const box = cardsBox(column);
    const before = insertionPoint(column, y);
    if (before) {
      box.insertBefore(drag.card, before);
    } else {
      box.appendChild(drag.card);
    }
  }

  async function finishDrag() {
    const card = drag.card;
    const fromColumn = drag.fromColumn;
    const fromNext = drag.fromNext;
    const toColumn = card.closest(".kanban-column");
    const current = drag;
    drag = null;

    card.classList.remove("dragging");
    document.body.style.userSelect = "";

    if (!toColumn) return;

    const statusChanged = toColumn !== fromColumn;
    const orderChanged = fromNext !== card.nextElementSibling;
    if (!statusChanged && !orderChanged) return;

    refreshColumn(fromColumn);
    refreshColumn(toColumn);

    saving = true;
    try {
      const response = await fetch("/api/board/move", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          order_id: Number(card.dataset.orderId),
          status: toColumn.dataset.status,
          queue_ids: isQueueColumn(toColumn) || isQueueColumn(fromColumn) ? collectQueueIds() : [],
        }),
      });

      if (response.status === 409) {
        showToast("Доска устарела. Обновляем страницу…", true);
        window.setTimeout(() => window.location.reload(), 1200);
        return;
      }
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail || "Не удалось сохранить");
      }

      applyServerState(await response.json());
      showToast(statusChanged ? "Этап заказа обновлён" : "Очередь сохранена");
    } catch (error) {
      // Возвращаем карточку на место: доска должна показывать то, что в базе.
      if (fromNext && fromNext.parentElement) {
        cardsBox(fromColumn).insertBefore(card, fromNext);
      } else {
        cardsBox(fromColumn).appendChild(card);
      }
      refreshColumn(fromColumn);
      refreshColumn(toColumn);
      showToast(shortMessage(error), true);
    } finally {
      saving = false;
      void current;
    }
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
          readyBox.textContent = `Готов к ${readyDates[id]}`;
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

    const startX = event.clientX;
    const startY = event.clientY;
    let holdTimer = null;

    const onMove = (moveEvent) => {
      if (drag && drag.active) {
        moveEvent.preventDefault();
        const under = document.elementFromPoint(moveEvent.clientX, moveEvent.clientY);
        const column = under && under.closest(".kanban-column");
        if (column) moveTo(column, moveEvent.clientY);
        return;
      }
      const far =
        Math.abs(moveEvent.clientX - startX) > MOUSE_THRESHOLD_PX ||
        Math.abs(moveEvent.clientY - startY) > MOUSE_THRESHOLD_PX;
      if (event.pointerType === "touch") {
        // На тач-экране движение до долгого нажатия — это прокрутка списка.
        if (far && holdTimer) {
          window.clearTimeout(holdTimer);
          holdTimer = null;
          cleanup();
        }
        return;
      }
      if (far) beginDrag(card, event);
    };

    const onUp = () => {
      if (holdTimer) window.clearTimeout(holdTimer);
      if (drag && drag.active) {
        suppressClick = true;
        finishDrag();
      }
      cleanup();
    };

    const cleanup = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };

    if (event.pointerType === "touch") {
      holdTimer = window.setTimeout(() => {
        holdTimer = null;
        beginDrag(card, event);
      }, TOUCH_HOLD_MS);
    }

    window.addEventListener("pointermove", onMove, { passive: false });
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  });

  // Короткий клик открывает заказ, перетаскивание — нет.
  board.addEventListener(
    "click",
    (event) => {
      if (!suppressClick) return;
      suppressClick = false;
      event.preventDefault();
      event.stopPropagation();
    },
    true
  );

  columnsOf().forEach(refreshColumn);
})();
