// Scroll-reveal + sticky header scroll state.
// Driven by IntersectionObserver; no-op under prefers-reduced-motion.
(function () {
  'use strict';

  const prefersReduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function initReveal() {
    const targets = document.querySelectorAll('.reveal');
    if (!targets.length) return;

    if (prefersReduced || !('IntersectionObserver' in window)) {
      targets.forEach((el) => el.classList.add('is-visible'));
      return;
    }

    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15, rootMargin: '0px 0px -60px 0px' });

    targets.forEach((el) => io.observe(el));
  }

  function initStickyHeader() {
    const header = document.querySelector('.store-header');
    if (!header) return;

    const update = () => {
      if (window.scrollY > 8) header.classList.add('is-scrolled');
      else header.classList.remove('is-scrolled');
    };
    update();
    window.addEventListener('scroll', update, { passive: true });
  }

  function markReady() {
    document.documentElement.classList.add('store-ready');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      markReady();
      initReveal();
      initStickyHeader();
    });
  } else {
    markReady();
    initReveal();
    initStickyHeader();
  }
})();
