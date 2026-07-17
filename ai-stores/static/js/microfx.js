// AI Stores — micro-interaction & motion layer.
// Progressive enhancement only: every effect degrades gracefully and is a
// no-op under prefers-reduced-motion or on coarse-pointer (touch) devices
// where it wouldn't make sense. Nothing here is required for the page to work.
(function () {
  'use strict';

  var mm = window.matchMedia ? window.matchMedia.bind(window) : null;
  var reduce = mm ? mm('(prefers-reduced-motion: reduce)').matches : false;
  var finePointer = mm ? mm('(pointer: fine)').matches : true;

  // ── Scroll progress bar + scroll-to-top button ────────────────────────
  function initScroll() {
    var bar = document.getElementById('scroll-progress');
    var toTop = document.getElementById('to-top');
    if (!bar && !toTop) return;
    var ticking = false;

    function update() {
      ticking = false;
      var doc = document.documentElement;
      var max = (doc.scrollHeight - doc.clientHeight) || 1;
      var y = window.scrollY || doc.scrollTop || 0;
      var p = Math.min(1, Math.max(0, y / max));
      if (bar) bar.style.setProperty('--sp', p.toFixed(4));
      if (toTop) toTop.classList.toggle('is-show', y > 420);
    }
    function onScroll() {
      if (!ticking) { ticking = true; requestAnimationFrame(update); }
    }

    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll, { passive: true });
    update();

    if (toTop) {
      toTop.addEventListener('click', function () {
        window.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' });
      });
    }
  }

  // ── Image fade-in on load (opt-in via .fx-img) ────────────────────────
  function initImages() {
    var imgs = document.querySelectorAll('.fx-img');
    for (var i = 0; i < imgs.length; i++) {
      (function (img) {
        if (img.complete && img.naturalWidth > 0) { img.classList.add('is-loaded'); return; }
        img.addEventListener('load', function () { img.classList.add('is-loaded'); }, { once: true });
        img.addEventListener('error', function () { img.classList.add('is-loaded'); }, { once: true });
      })(imgs[i]);
    }
  }

  // ── Card 3D tilt + cursor spotlight ───────────────────────────────────
  function initTilt() {
    if (reduce || !finePointer) return;
    var cards = document.querySelectorAll('.store-card');
    cards.forEach(function (card) {
      var raf = 0;
      var rect = null;

      function onMove(e) {
        if (!rect) rect = card.getBoundingClientRect();
        var px = (e.clientX - rect.left) / rect.width;
        var py = (e.clientY - rect.top) / rect.height;
        card.style.setProperty('--mx', (px * 100).toFixed(1) + '%');
        card.style.setProperty('--my', (py * 100).toFixed(1) + '%');
        var rx = (0.5 - py) * 7;   // rotateX (deg)
        var ry = (px - 0.5) * 9;   // rotateY (deg)
        if (raf) return;
        raf = requestAnimationFrame(function () {
          raf = 0;
          card.style.transform =
            'perspective(900px) rotateX(' + rx.toFixed(2) + 'deg) rotateY(' +
            ry.toFixed(2) + 'deg) translateY(-6px)';
        });
      }

      card.addEventListener('pointerenter', function () {
        rect = card.getBoundingClientRect();
        card.classList.add('is-tilting');
      });
      card.addEventListener('pointermove', onMove);
      card.addEventListener('pointerleave', function () {
        card.classList.remove('is-tilting');
        card.style.transform = '';
        if (raf) { cancelAnimationFrame(raf); raf = 0; }
        rect = null;
      });
    });
  }

  // ── Magnetic elements (opt-in via data-magnetic="0.35") ───────────────
  function initMagnetic() {
    if (reduce || !finePointer) return;
    document.querySelectorAll('[data-magnetic]').forEach(function (el) {
      var strength = parseFloat(el.getAttribute('data-magnetic')) || 0.3;
      el.addEventListener('pointermove', function (e) {
        var r = el.getBoundingClientRect();
        var mx = e.clientX - (r.left + r.width / 2);
        var my = e.clientY - (r.top + r.height / 2);
        el.style.transform = 'translate(' + (mx * strength).toFixed(1) + 'px,' +
          (my * strength).toFixed(1) + 'px)';
      });
      el.addEventListener('pointerleave', function () { el.style.transform = ''; });
    });
  }

  // ── Hero caption pointer parallax (opt-in via [data-hero]) ────────────
  function initHeroParallax() {
    if (reduce || !finePointer) return;
    var hero = document.querySelector('[data-hero]');
    if (!hero) return;
    var caps = hero.querySelectorAll('.store-slide-caption');
    if (!caps.length) return;

    hero.addEventListener('pointermove', function (e) {
      var r = hero.getBoundingClientRect();
      var dx = (e.clientX - (r.left + r.width / 2)) / r.width;
      var dy = (e.clientY - (r.top + r.height / 2)) / r.height;
      caps.forEach(function (c) {
        c.style.setProperty('--px', (dx * -18).toFixed(1) + 'px');
        c.style.setProperty('--py', (dy * -12).toFixed(1) + 'px');
      });
    });
    hero.addEventListener('pointerleave', function () {
      caps.forEach(function (c) {
        c.style.setProperty('--px', '0px');
        c.style.setProperty('--py', '0px');
      });
    });
  }

  function boot() {
    document.documentElement.classList.add('fx-on');
    initScroll();
    initImages();
    initTilt();
    initMagnetic();
    initHeroParallax();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
