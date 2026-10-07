/*
 * Мелкая живость интерфейса: крупные числа в плитках «досчитываются» при открытии.
 * Число берём из текста элемента с атрибутом data-count — без него ничего не меняется.
 */
(() => {
  "use strict";
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  const nodes = document.querySelectorAll("[data-count]");
  nodes.forEach((node) => {
    const original = node.textContent;
    const match = original.match(/[\d\s ]+/);
    if (!match) return;
    const target = Number(match[0].replace(/[\s ]/g, ""));
    if (!Number.isFinite(target) || target < 2) return;
    const format = (value) =>
      original.replace(match[0], value.toLocaleString("ru-RU").replace(/,/g, " ") + (match[0].endsWith(" ") ? " " : ""));
    const started = performance.now();
    const duration = 700;
    const tick = (now) => {
      const t = Math.min(1, (now - started) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      node.textContent = format(Math.round(target * eased));
      if (t < 1) requestAnimationFrame(tick);
      else node.textContent = original;
    };
    requestAnimationFrame(tick);
  });
})();

/*
 * Кольцевые диаграммы: наведение на дугу или строку легенды выделяет часть,
 * остальные гаснут, в центре — доля и название выбранной части.
 */
(() => {
  "use strict";
  document.querySelectorAll("[data-donut]").forEach((wrap) => {
    const value = wrap.querySelector(".center b");
    const label = wrap.querySelector(".center span");
    if (!value || !label) return;
    const original = [value.textContent, label.textContent];

    const select = (index) => {
      wrap.classList.toggle("hl", index != null);
      wrap.querySelectorAll("[data-seg]").forEach((el) => el.classList.toggle("on", el.dataset.seg === index));
      if (index == null) {
        [value.textContent, label.textContent] = original;
        return;
      }
      const item = wrap.querySelector(`li[data-seg="${index}"]`);
      if (!item) return;
      value.textContent = `${item.dataset.percent}%`;
      label.textContent = item.dataset.label;
    };

    wrap.addEventListener("mouseover", (event) => {
      const target = event.target.closest("[data-seg]");
      select(target ? target.dataset.seg : null);
    });
    // На телефоне касание тоже даёт mouseover, поэтому отдельный обработчик не нужен.
    wrap.addEventListener("mouseleave", () => select(null));
  });
})();

/* Кнопки удаления переспрашивают: data-confirm="Удалить бокс «…»?" */
document.addEventListener(
  "click",
  (event) => {
    const button = event.target.closest("[data-confirm]");
    if (button && !window.confirm(button.dataset.confirm)) {
      event.preventDefault();
      event.stopPropagation();
    }
  },
  true
);
