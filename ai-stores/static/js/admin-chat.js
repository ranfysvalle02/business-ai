/*
 * AI Stores — conversational store editor (admin only).
 *
 * A floating chat widget that turns natural language into validated,
 * confirm-first edits. It never writes directly: it asks /admin/ai/chat for
 * proposed operations, renders a diff card, and only POSTs to /admin/ai/apply
 * once the admin clicks Apply.
 *
 * Self-contained: builds its own DOM + uses scoped .aic-* styles from app.css.
 */
(function () {
  'use strict';
  if (window.__aicMounted) return;
  window.__aicMounted = true;

  var CHAT_URL = '/admin/ai/chat';
  var APPLY_URL = '/admin/ai/apply';

  // Conversation history sent to the model (text turns only).
  var history = [];
  var busy = false;

  // ── Small helpers ───────────────────────────────────────────────────────
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function iconFor(line) {
    var t = String(line || '').toLowerCase();
    if (t.indexOf('add ') === 0 || t.indexOf('add section') === 0) return 'fa-plus';
    if (t.indexOf('remove') === 0) return 'fa-trash';
    if (t.indexOf('reorder') === 0) return 'fa-arrows-up-down';
    if (t.indexOf('hide') === 0) return 'fa-eye-slash';
    if (t.indexOf('show') === 0) return 'fa-eye';
    return 'fa-pen';
  }

  // ── DOM scaffold ────────────────────────────────────────────────────────
  var launcher = el('button', 'aic-launcher', '<i class="fa-solid fa-robot" aria-hidden="true"></i><span>Edit with AI</span>');
  launcher.type = 'button';
  launcher.setAttribute('aria-label', 'Open the AI store editor');

  var panel = el('div', 'aic-panel');
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'AI store editor');
  panel.setAttribute('aria-modal', 'false');

  var header = el('div', 'aic-header',
    '<div>' +
      '<div class="aic-title"><i class="fa-solid fa-robot" aria-hidden="true"></i> Store assistant</div>' +
      '<div class="aic-sub">Describe a change — you approve before anything saves.</div>' +
    '</div>' +
    '<button class="aic-close" type="button" aria-label="Close">&times;</button>');

  var messages = el('div', 'aic-messages');
  messages.setAttribute('role', 'log');
  messages.setAttribute('aria-live', 'polite');
  messages.setAttribute('aria-atomic', 'false');

  var suggest = el('div', 'aic-suggest');
  [
    'Change the tagline to "Handmade with care"',
    'Add a gallery section after the catalog',
    'Mark Gift Card as sold',
    'Hide the specials section'
  ].forEach(function (s) {
    var chip = el('button', 'aic-chip', esc(s));
    chip.type = 'button';
    chip.addEventListener('click', function () { input.value = s; input.focus(); autoGrow(); });
    suggest.appendChild(chip);
  });

  var inputbar = el('div', 'aic-inputbar');
  var input = el('textarea', 'aic-input');
  input.setAttribute('rows', '1');
  input.setAttribute('placeholder', 'e.g. add a call-to-action section at the end');
  input.setAttribute('aria-label', 'Message the store assistant');
  var send = el('button', 'aic-send', '<i class="fa-solid fa-paper-plane" aria-hidden="true"></i>');
  send.type = 'button';
  send.setAttribute('aria-label', 'Send');
  inputbar.appendChild(input);
  inputbar.appendChild(send);

  panel.appendChild(header);
  panel.appendChild(messages);
  panel.appendChild(suggest);
  panel.appendChild(inputbar);

  document.body.appendChild(launcher);
  document.body.appendChild(panel);

  // Greeting.
  addBot("Hi! I can update your layout, copy, catalog and specials. What would you like to change?");

  // ── Open / close ──────────────────────────────────────────────────────
  function open() {
    panel.classList.add('is-open');
    launcher.classList.add('is-hidden');
    setTimeout(function () { input.focus(); }, 180);
  }
  function close() {
    panel.classList.remove('is-open');
    launcher.classList.remove('is-hidden');
    launcher.focus();
  }
  launcher.addEventListener('click', open);
  header.querySelector('.aic-close').addEventListener('click', close);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && panel.classList.contains('is-open')) close();
  });

  // ── Rendering ───────────────────────────────────────────────────────────
  function scrollDown() { messages.scrollTop = messages.scrollHeight; }

  function addUser(text) {
    var m = el('div', 'aic-msg user', esc(text));
    messages.appendChild(m);
    scrollDown();
  }
  function addBot(text, isErr) {
    var m = el('div', 'aic-msg bot' + (isErr ? ' err' : ''), esc(text));
    messages.appendChild(m);
    scrollDown();
    return m;
  }
  function addTyping() {
    var m = el('div', 'aic-msg bot', '<span class="aic-typing"><span></span><span></span><span></span></span>');
    m.dataset.typing = '1';
    messages.appendChild(m);
    scrollDown();
    return m;
  }

  function renderDiff(diff, warnings, ops) {
    var card = el('div', 'aic-diff');
    card.appendChild(el('div', 'aic-diff-head', 'Proposed changes'));
    var list = el('ul', 'aic-diff-list');
    (diff || []).forEach(function (line) {
      var li = el('li', 'aic-diff-item',
        '<i class="fa-solid ' + iconFor(line) + '" aria-hidden="true"></i><span>' + esc(line) + '</span>');
      list.appendChild(li);
    });
    card.appendChild(list);

    if (warnings && warnings.length) {
      card.appendChild(el('div', 'aic-diff-warn',
        '<i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i> ' + esc(warnings.join(' '))));
    }

    var actions = el('div', 'aic-diff-actions');
    var applyBtn = el('button', 'aic-btn aic-btn-apply', 'Apply');
    applyBtn.type = 'button';
    var discardBtn = el('button', 'aic-btn aic-btn-discard', 'Discard');
    discardBtn.type = 'button';
    actions.appendChild(applyBtn);
    actions.appendChild(discardBtn);
    card.appendChild(actions);

    applyBtn.addEventListener('click', function () { applyOps(ops, card, applyBtn, discardBtn); });
    discardBtn.addEventListener('click', function () {
      card.remove();
      addBot('No changes made. Anything else?');
    });

    messages.appendChild(card);
    scrollDown();
  }

  // ── Network ─────────────────────────────────────────────────────────────
  async function postJson(url, body) {
    // url is a store-relative admin path (e.g. /admin/ai/chat); prefix the
    // active store's base path so the request scopes to the right store.
    var res = await fetch((window.BASE_PATH || '') + url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    var data = null;
    try { data = await res.json(); } catch (e) { /* ignore */ }
    if (!res.ok) {
      var msg = (data && (data.detail || data.error)) || ('Request failed (' + res.status + ')');
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return data;
  }

  async function sendMessage() {
    if (busy) return;
    var text = input.value.trim();
    if (!text) return;
    input.value = '';
    autoGrow();
    addUser(text);
    history.push({ role: 'user', content: text });
    if (suggest.parentNode) suggest.style.display = 'none';

    busy = true;
    send.disabled = true;
    var typing = addTyping();

    try {
      var data = await postJson(CHAT_URL, { messages: history });
      typing.remove();
      var reply = (data && data.reply) || '';
      if (reply) {
        addBot(reply);
        history.push({ role: 'assistant', content: reply });
      }
      if (data && data.ops && data.ops.length) {
        renderDiff(data.diff, data.warnings, data.ops);
      }
    } catch (err) {
      typing.remove();
      addBot(String((err && err.message) || err), true);
    } finally {
      busy = false;
      send.disabled = false;
      input.focus();
    }
  }

  async function applyOps(ops, card, applyBtn, discardBtn) {
    if (busy) return;
    busy = true;
    applyBtn.disabled = true;
    discardBtn.disabled = true;
    applyBtn.textContent = 'Applying…';
    try {
      var data = await postJson(APPLY_URL, { ops: ops });
      var ok = data && data.ok;
      card.querySelector('.aic-diff-actions').remove();
      var badge = el('div', 'aic-diff-warn', ok
        ? '<i class="fa-solid fa-check" aria-hidden="true"></i> Applied.'
        : '<i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i> Some changes could not be applied.');
      badge.style.color = ok ? 'rgb(52 211 153)' : 'rgb(251 191 36)';
      card.appendChild(badge);
      toast(ok ? 'Changes applied' : 'Applied with warnings', ok);
      // Refresh the affected view so the admin sees the result.
      setTimeout(function () { window.location.reload(); }, 900);
    } catch (err) {
      applyBtn.disabled = false;
      discardBtn.disabled = false;
      applyBtn.textContent = 'Apply';
      addBot(String((err && err.message) || err), true);
    } finally {
      busy = false;
    }
  }

  // ── Toast ───────────────────────────────────────────────────────────────
  function toast(msg, ok) {
    var t = el('div', 'store-toast', esc(msg));
    t.classList.add(ok ? 'is-ok' : 'is-err');
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add('is-show'); });
    setTimeout(function () {
      t.classList.remove('is-show');
      setTimeout(function () { t.remove(); }, 300);
    }, 2500);
  }

  // ── Input behaviour ─────────────────────────────────────────────────────
  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  }
  input.addEventListener('input', autoGrow);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  send.addEventListener('click', sendMessage);
})();
