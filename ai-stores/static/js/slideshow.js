// AI Stores — hero slideshow engine
// Features:
//   - Per-slide media: image (background) OR video (autoplay/muted)
//   - Per-slide transitions (fade / kenburns / zoom / slide) via data-transition
//   - Per-slide duration (data-duration). Videos respect their own length when shorter.
//   - Focal point (--fx / --fy CSS vars)
//   - Progress bar
//   - Swipe / pointer gestures
//   - Preload next image
//   - Pause on hidden tab / hover / prefers-reduced-motion
//   - aria-live announcement
//   - Keyboard navigation (arrow keys, space to toggle)
//
// Reuse: window.StoreSlideshow.init(rootEl, { autoplay: true, keyboard: false, ... })
// returns a controller { destroy, next, prev, setActive, setPlaying }
(function () {
  'use strict';

  function init(root, opts) {
    opts = opts || {};
    const slides = Array.from(root.querySelectorAll('.store-slide'));
    if (slides.length === 0) return null;

    const dots = Array.from(root.querySelectorAll('.store-slide-dot'));
    const prevBtn = root.querySelector('[data-slide-prev]');
    const nextBtn = root.querySelector('[data-slide-next]');
    const toggleBtn = root.querySelector('[data-slide-toggle]');
    const toggleIcon = toggleBtn ? toggleBtn.querySelector('i') : null;
    const progress = root.querySelector('.store-slide-progress');
    const live = root.querySelector('.store-slide-live');

    const prefersReduced = window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    const wantAutoplay = opts.autoplay !== false;
    const wantKeyboard = opts.keyboard !== false;
    const wantGestures = opts.gestures !== false;

    let current = 0;
    let playing = wantAutoplay && !prefersReduced && slides.length > 1;
    let timer = null;
    let progressStart = 0;
    let progressDuration = 5000;
    let progressRaf = 0;
    let hoverPaused = false;
    let hiddenPaused = false;
    let destroyed = false;
    const cleanupFns = [];

    function slideDuration(i) {
      const d = parseInt(slides[i]?.dataset.duration, 10);
      if (Number.isFinite(d) && d >= 1000 && d <= 60000) return d;
      return 5000;
    }

    function slideVideo(i) {
      return slides[i] ? slides[i].querySelector('video') : null;
    }

    function pauseAllVideosExcept(i) {
      slides.forEach((s, idx) => {
        const v = s.querySelector('video');
        if (!v) return;
        if (idx !== i) {
          try { v.pause(); v.currentTime = 0; } catch (_) { /* ignore */ }
        }
      });
    }

    function setActive(i) {
      current = (i + slides.length) % slides.length;
      slides.forEach((s, idx) => s.classList.toggle('is-active', idx === current));
      dots.forEach((d, idx) => d.classList.toggle('is-active', idx === current));

      if (live) live.textContent = `Slide ${current + 1} of ${slides.length}`;

      const active = slides[current];
      const img = active.querySelector('.store-slide-img');
      const vid = active.querySelector('video');

      if (img) {
        img.style.animation = 'none';
        // force reflow so animation restarts
        // eslint-disable-next-line no-unused-expressions
        img.offsetHeight;
        img.style.animation = '';
        const dur = slideDuration(current);
        img.style.setProperty('--kb-dur', `${Math.max(dur * 1.3, 6000)}ms`);
      }

      pauseAllVideosExcept(current);
      if (vid) {
        try {
          vid.muted = true;
          vid.currentTime = 0;
          const p = vid.play();
          if (p && typeof p.catch === 'function') p.catch(() => { /* autoplay blocked */ });
        } catch (_) { /* ignore */ }
      }

      preloadNext();
      restartProgress();
    }

    function next() { setActive(current + 1); }
    function prev() { setActive(current - 1); }

    function startTimer() {
      stopTimer();
      if (!playing || hoverPaused || hiddenPaused) return;
      // If the active slide is a video, let the video drive advancement on `ended`
      // — but still set a max-duration safety timeout.
      const dur = slideDuration(current);
      timer = setTimeout(() => { next(); }, dur);
    }
    function stopTimer() {
      if (timer) { clearTimeout(timer); timer = null; }
    }

    function restartProgress() {
      stopProgress();
      if (!progress) { startTimer(); return; }
      progressDuration = slideDuration(current);
      progressStart = performance.now();
      const tick = (now) => {
        if (destroyed) return;
        const elapsed = now - progressStart;
        const pct = Math.min(100, (elapsed / progressDuration) * 100);
        progress.style.width = pct + '%';
        if (pct < 100 && playing && !hoverPaused && !hiddenPaused) {
          progressRaf = requestAnimationFrame(tick);
        }
      };
      if (playing && !hoverPaused && !hiddenPaused) {
        progressRaf = requestAnimationFrame(tick);
      } else {
        progress.style.width = '0%';
      }
      startTimer();
    }
    function stopProgress() {
      if (progressRaf) { cancelAnimationFrame(progressRaf); progressRaf = 0; }
      stopTimer();
    }

    function preloadNext() {
      if (slides.length < 2) return;
      const nextIdx = (current + 1) % slides.length;
      const url = slides[nextIdx].dataset.preload;
      if (!url) return;
      if (preloadNext._done && preloadNext._done.has(url)) return;
      if (!preloadNext._done) preloadNext._done = new Set();
      preloadNext._done.add(url);
      const img = new Image();
      img.src = url;
    }

    function setPlaying(v) {
      playing = !!v;
      if (toggleIcon) {
        toggleIcon.classList.toggle('fa-play', !playing);
        toggleIcon.classList.toggle('fa-pause', playing);
      }
      if (toggleBtn) {
        toggleBtn.setAttribute('aria-label', playing ? 'Pause slideshow' : 'Play slideshow');
      }
      const vid = slideVideo(current);
      if (vid) {
        try { playing ? vid.play().catch(() => {}) : vid.pause(); } catch (_) { /* ignore */ }
      }
      if (playing) restartProgress(); else stopProgress();
    }

    // --- event wiring ---
    function addL(target, ev, fn, opt) {
      if (!target) return;
      target.addEventListener(ev, fn, opt);
      cleanupFns.push(() => target.removeEventListener(ev, fn, opt));
    }

    addL(nextBtn, 'click', () => { next(); });
    addL(prevBtn, 'click', () => { prev(); });
    addL(toggleBtn, 'click', () => { setPlaying(!playing); });

    dots.forEach((dot, idx) => addL(dot, 'click', () => { setActive(idx); }));

    // advance immediately when a video finishes (loop end-driven only when not loop'd)
    slides.forEach((s) => {
      const v = s.querySelector('video');
      if (!v) return;
      addL(v, 'ended', () => {
        if (s.classList.contains('is-active') && playing) next();
      });
    });

    addL(root, 'mouseenter', () => { hoverPaused = true; stopProgress(); });
    addL(root, 'mouseleave', () => { hoverPaused = false; if (playing) restartProgress(); });

    addL(document, 'visibilitychange', () => {
      hiddenPaused = document.hidden;
      if (document.hidden) stopProgress();
      else if (playing) restartProgress();
    });

    if (wantKeyboard) {
      const onKey = (e) => {
        const tag = (document.activeElement && document.activeElement.tagName) || '';
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
        if (e.key === 'ArrowLeft')  { prev(); }
        else if (e.key === 'ArrowRight') { next(); }
        else if (e.key === ' ' && e.target === document.body) {
          e.preventDefault();
          setPlaying(!playing);
        }
      };
      addL(document, 'keydown', onKey);
    }

    if (wantGestures) {
      let dragStartX = 0;
      let dragging = false;
      addL(root, 'pointerdown', (e) => {
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        dragging = true;
        dragStartX = e.clientX;
      });
      addL(root, 'pointerup', (e) => {
        if (!dragging) return;
        dragging = false;
        const dx = e.clientX - dragStartX;
        if (Math.abs(dx) > 50) {
          if (dx < 0) next(); else prev();
        }
      });
      addL(root, 'pointercancel', () => { dragging = false; });
    }

    setActive(0);
    setPlaying(playing);

    return {
      next, prev, setActive, setPlaying,
      togglePlaying() { setPlaying(!playing); },
      isPlaying() { return playing; },
      destroy() {
        destroyed = true;
        stopProgress();
        cleanupFns.forEach((fn) => { try { fn(); } catch (_) { /* ignore */ } });
        slides.forEach((s) => {
          const v = s.querySelector('video');
          if (v) { try { v.pause(); } catch (_) { /* ignore */ } }
        });
      },
    };
  }

  function boot() {
    document.querySelectorAll('[data-slideshow]').forEach((el) => {
      if (el.dataset.storeInited === '1') return;
      el.dataset.storeInited = '1';
      init(el);
    });
  }

  window.StoreSlideshow = { init, boot };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
