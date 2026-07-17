// Shared drag-to-reorder helper for admin screens (layout, slideshow, …).
// Wraps SortableJS and persists the new order via PATCH /{api}/{id} {order}.
//
// Usage:
//   AdminSortable.init({
//     grid: '#layout-list',
//     handle: '.admin-drag-handle',
//     item: '[data-id]',
//     api: '/api/sections',
//     toast: myToastFn,          // optional: (msg, 'ok'|'err') => void
//     onReorder: (cards) => {}   // optional: custom persistence
//   });
(function () {
  'use strict';

  async function patchOrder(cards, api, toast) {
    try {
      await Promise.all(cards.map((card, idx) =>
        fetch(api + '/' + card.dataset.id, {
          method: 'PATCH',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ order: idx + 1 }),
        }).then((res) => {
          if (!res.ok) throw new Error('HTTP ' + res.status);
        })
      ));
      if (toast) toast('Order saved', 'ok');
    } catch (err) {
      if (toast) toast('Reorder failed: ' + err.message, 'err');
    }
  }

  function init(opts) {
    opts = opts || {};
    const grid = typeof opts.grid === 'string' ? document.querySelector(opts.grid) : opts.grid;
    if (!grid || typeof Sortable === 'undefined') return null;
    const itemSel = opts.item || '[data-id]';

    return Sortable.create(grid, {
      handle: opts.handle || '.admin-drag-handle',
      animation: 180,
      ghostClass: 'is-ghost',
      dragClass: 'is-dragging',
      onEnd: async () => {
        const cards = Array.from(grid.querySelectorAll(itemSel));
        cards.forEach((card, idx) => {
          const posEl = card.querySelector('[data-position]');
          if (posEl) posEl.textContent = idx + 1;
        });
        if (typeof opts.onReorder === 'function') {
          await opts.onReorder(cards);
        } else if (opts.api) {
          await patchOrder(cards, opts.api, opts.toast);
        }
      },
    });
  }

  // Retry until SortableJS (loaded with `defer`) is available.
  function initWhenReady(opts) {
    if (typeof Sortable !== 'undefined') return init(opts);
    let tries = 0;
    const t = setInterval(() => {
      if (typeof Sortable !== 'undefined' || tries++ > 100) {
        clearInterval(t);
        init(opts);
      }
    }, 50);
    return null;
  }

  window.AdminSortable = { init, initWhenReady, patchOrder };
})();
