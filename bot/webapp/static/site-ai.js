/* AI-помощник на сайте: плавающая кнопка и окно чата.
   Сам спрашивает у сервера, нужен ли он на этой странице (включается админом в боте),
   и ничего не рисует, если выключен. Ответы показываются через белый список тегов. */
(function () {
  'use strict';
  if (window.__siteAiLoaded) return;
  window.__siteAiLoaded = true;

  var page = location.pathname.indexOf('/welcome') === 0 ? 'welcome' : 'shop';
  var STORE = 'site_ai_chat_v1';
  var cfg = null, box = null, list = null, input = null, sendBtn = null, chips = null, busy = false;

  function store(get, val) {
    try {
      if (get) return JSON.parse(sessionStorage.getItem(STORE) || '[]');
      sessionStorage.setItem(STORE, JSON.stringify(val.slice(-30)));
    } catch (e) { return []; }
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* Безопасный показ ответа: только b/i/u/s/code/pre/a и переводы строк. */
  var ALLOWED = { B: 1, STRONG: 1, I: 1, EM: 1, U: 1, S: 1, CODE: 1, PRE: 1, A: 1, BR: 1 };
  function render(parent, html) {
    var doc = new DOMParser().parseFromString('<body>' + String(html) + '</body>', 'text/html');
    (function walk(src, dst) {
      src.childNodes.forEach(function (n) {
        if (n.nodeType === 3) {
          var parts = n.textContent.split('\n');
          parts.forEach(function (t, i) {
            if (i) dst.appendChild(document.createElement('br'));
            if (t) dst.appendChild(document.createTextNode(t));
          });
        } else if (n.nodeType === 1) {
          if (!ALLOWED[n.tagName]) { walk(n, dst); return; }
          if (n.tagName === 'BR') { dst.appendChild(document.createElement('br')); return; }
          var out = document.createElement(n.tagName.toLowerCase());
          if (n.tagName === 'A') {
            var href = n.getAttribute('href') || '';
            if (!/^(https?:\/\/|tg:\/\/)/i.test(href)) { walk(n, dst); return; }
            out.setAttribute('href', href);
            out.setAttribute('target', '_blank');
            out.setAttribute('rel', 'noopener noreferrer');
          }
          walk(n, out);
          dst.appendChild(out);
        }
      });
    })(doc.body, parent);
  }

  function addMsg(role, text, save) {
    var row = el('div', 'sai-row sai-' + role);
    var bubble = el('div', 'sai-bubble');
    if (role === 'bot') render(bubble, text); else bubble.textContent = text;
    row.appendChild(bubble);
    list.appendChild(row);
    list.scrollTop = list.scrollHeight;
    if (save !== false) {
      var h = store(true); h.push({ r: role, t: text }); store(false, h);
    }
    return row;
  }

  function setBusy(v) {
    busy = v;
    sendBtn.disabled = v;
    input.disabled = v;
  }

  function ask(text) {
    text = (text || '').trim();
    if (!text || busy) return;
    if (chips) { chips.style.display = 'none'; }
    addMsg('user', text);
    input.value = '';
    setBusy(true);
    var typing = el('div', 'sai-row sai-bot');
    var tb = el('div', 'sai-bubble sai-typing');
    tb.innerHTML = '<span></span><span></span><span></span>';
    typing.appendChild(tb);
    list.appendChild(typing);
    list.scrollTop = list.scrollHeight;

    fetch('/api/public/site-ai/ask', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, d: d }; });
    }).then(function (res) {
      typing.remove();
      if (res.ok && res.d.reply) {
        addMsg('bot', res.d.reply);
      } else {
        var m = res.d.message;
        if (!m && res.d.error === 'message_too_long') m = 'Вопрос слишком длинный, сократите его.';
        addMsg('bot', m || 'Не удалось получить ответ. Попробуйте ещё раз.', false);
      }
    }).catch(function () {
      typing.remove();
      addMsg('bot', 'Нет связи с сервером. Попробуйте ещё раз.', false);
    }).then(function () {
      setBusy(false);
      if (box.classList.contains('sai-open')) input.focus();
    });
  }

  function open() {
    box.classList.add('sai-open');
    fab.classList.add('sai-hidden');
    setTimeout(function () { input.focus(); }, 50);
  }
  function close() {
    box.classList.remove('sai-open');
    fab.classList.remove('sai-hidden');
  }

  var fab = null;
  function build() {
    var css = el('style');
    css.textContent =
      '.sai-fab{position:fixed;right:16px;bottom:calc(16px + env(safe-area-inset-bottom));z-index:99990;display:flex;align-items:center;gap:8px;' +
      'padding:12px 16px;border:0;border-radius:999px;cursor:pointer;font-weight:600;font-size:14px;line-height:1;font-family:inherit;color:#16141a;' +
      'background:linear-gradient(135deg,var(--gold-bright,#F2C879),var(--gold,#D9AE6C));box-shadow:0 8px 24px rgba(0,0,0,.35)}' +
      '.sai-fab.sai-hidden{display:none}' +
      '.sai-box{position:fixed;right:16px;bottom:calc(16px + env(safe-area-inset-bottom));z-index:99991;width:min(380px,calc(100vw - 32px));' +
      'height:min(560px,calc(100vh - 32px));display:none;flex-direction:column;overflow:hidden;border-radius:18px;' +
      'background:linear-gradient(var(--panel,#14151D),var(--panel,#14151D)),var(--void,#0A0A10);color:var(--ink,#F4F1E9);border:1px solid var(--hairline,rgba(255,255,255,.1));box-shadow:0 18px 50px rgba(0,0,0,.5);font-family:inherit}' +
      '.sai-box.sai-open{display:flex}' +
      '.sai-head{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--hairline,rgba(255,255,255,.08))}' +
      '.sai-title{font-weight:700;font-size:15px}.sai-sub{font-size:12px;color:var(--mist,#8B90A0);margin-top:2px}' +
      '.sai-x{background:none;border:0;color:inherit;font-size:22px;line-height:1;cursor:pointer;padding:4px 8px;opacity:.7}' +
      '.sai-list{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;-webkit-overflow-scrolling:touch}' +
      '.sai-row{display:flex}.sai-user{justify-content:flex-end}' +
      '.sai-bubble{max-width:86%;padding:10px 13px;border-radius:14px;font-size:14px;line-height:1.45;word-wrap:break-word;overflow-wrap:anywhere}' +
      '.sai-bot .sai-bubble{background:var(--panel-2,#1B1C27);border-bottom-left-radius:4px}' +
      '.sai-user .sai-bubble{background:linear-gradient(135deg,var(--gold-bright,#F2C879),var(--gold,#D9AE6C));color:#16141a;border-bottom-right-radius:4px}' +
      '.sai-bubble a{color:var(--teal,#2FD1C4);text-decoration:underline}.sai-user .sai-bubble a{color:#16141a}' +
      '.sai-bubble code,.sai-bubble pre{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;background:rgba(255,255,255,.08);padding:1px 5px;border-radius:5px;white-space:pre-wrap}' +
      '.sai-chips{display:flex;flex-wrap:wrap;gap:8px;padding:0 14px 10px}' +
      '.sai-chip{border:1px solid var(--hairline,rgba(255,255,255,.15));background:transparent;color:inherit;border-radius:999px;padding:8px 12px;font-weight:500;font-size:13px;font-family:inherit;cursor:pointer}' +
      '.sai-chip:hover{background:var(--panel-2,#1B1C27)}' +
      '.sai-form{display:flex;gap:8px;padding:10px 12px calc(10px + env(safe-area-inset-bottom));border-top:1px solid var(--hairline,rgba(255,255,255,.08))}' +
      '.sai-in{flex:1;min-width:0;border:1px solid var(--hairline,rgba(255,255,255,.15));background:var(--void,#0A0A10);color:inherit;border-radius:12px;padding:11px 12px;font-size:14px;font-family:inherit;outline:none}' +
      '.sai-send{border:0;border-radius:12px;padding:0 16px;font-weight:700;font-size:14px;font-family:inherit;cursor:pointer;color:#16141a;background:linear-gradient(135deg,var(--gold-bright,#F2C879),var(--gold,#D9AE6C))}' +
      '.sai-send:disabled,.sai-in:disabled{opacity:.5;cursor:default}' +
      '.sai-note{font-size:11px;color:var(--mist-dim,#565A68);text-align:center;padding:0 12px 8px}' +
      '.sai-typing{display:flex;gap:4px;align-items:center;min-height:20px}' +
      '.sai-typing span{width:6px;height:6px;border-radius:50%;background:var(--mist,#8B90A0);animation:saiDot 1s infinite}' +
      '.sai-typing span:nth-child(2){animation-delay:.15s}.sai-typing span:nth-child(3){animation-delay:.3s}' +
      '@keyframes saiDot{0%,60%,100%{opacity:.3;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}' +
      '@media(max-width:520px){.sai-box{right:0;left:0;bottom:0;width:100%;height:min(82vh,640px);border-radius:18px 18px 0 0}}';
    document.head.appendChild(css);

    fab = el('button', 'sai-fab', '💬 AI-помощник');
    fab.type = 'button';
    fab.setAttribute('aria-label', 'Открыть чат с AI-помощником');
    fab.addEventListener('click', open);

    box = el('div', 'sai-box');
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-label', 'AI-помощник');
    var head = el('div', 'sai-head');
    var ht = el('div');
    ht.appendChild(el('div', 'sai-title', '🤖 AI-помощник'));
    ht.appendChild(el('div', 'sai-sub', cfg.mode === 'client' ? 'Вижу вашу подписку и помогу с ней' : 'Помогу выбрать тариф и подключиться'));
    var x = el('button', 'sai-x', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Закрыть');
    x.addEventListener('click', close);
    head.appendChild(ht); head.appendChild(x);

    list = el('div', 'sai-list');
    list.setAttribute('aria-live', 'polite');

    var form = el('form', 'sai-form');
    input = el('input', 'sai-in');
    input.type = 'text'; input.maxLength = cfg.max_len || 500; input.autocomplete = 'off';
    input.placeholder = 'Напишите вопрос…';
    sendBtn = el('button', 'sai-send', '➤'); sendBtn.type = 'submit'; sendBtn.setAttribute('aria-label', 'Отправить');
    form.appendChild(input); form.appendChild(sendBtn);
    form.addEventListener('submit', function (e) { e.preventDefault(); ask(input.value); });

    box.appendChild(head); box.appendChild(list);
    var hist = store(true);
    if (!hist.length && cfg.quick && cfg.quick.length) {
      chips = el('div', 'sai-chips');
      cfg.quick.forEach(function (q) {
        var c = el('button', 'sai-chip', q); c.type = 'button';
        c.addEventListener('click', function () { ask(q); });
        chips.appendChild(c);
      });
      box.appendChild(chips);
    }
    box.appendChild(form);
    box.appendChild(el('div', 'sai-note', 'Отвечает AI, возможны неточности. Платёж подтверждайте на странице покупки.'));
    document.body.appendChild(fab);
    document.body.appendChild(box);

    if (hist.length) hist.forEach(function (m) { addMsg(m.r, m.t, false); });
    else addMsg('bot', cfg.greeting || 'Здравствуйте! Чем могу помочь?', false);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
  }

  function start() {
    fetch('/api/public/site-ai/config?page=' + page, { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : { enabled: false }; })
      .then(function (c) { if (c && c.enabled) { cfg = c; build(); } })
      .catch(function () { /* без помощника страница работает как обычно */ });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
