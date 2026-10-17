(() => {
  'use strict';
  const dialog = document.getElementById('furatic-tutorial');
  const toggle = document.getElementById('tutorial-toggle');
  if (!dialog || !toggle || typeof dialog.showModal !== 'function') return;
  const panel = dialog.querySelector('.ft-panel');
  const hub = document.getElementById('ft-hub');
  const lessonView = document.getElementById('ft-lesson');
  const title = document.getElementById('ft-title');
  const instruction = document.getElementById('ft-instruction');
  const region = document.getElementById('ft-player-region');
  const loading = document.getElementById('ft-loading');
  const next = document.getElementById('ft-next');
  const close = document.getElementById('ft-close');
  const cue = new window.FuraticTutorialCue(dialog);
  const labels = {adding: 'Adding music', voting: 'Voting on songs', multiple: 'Adding several songs', prequeue: 'How the pre-queue works'};
  // New revision intentionally ignores both the old rules and broken v2 guide.
  const storageKey = 'furatic-tutorial-v3';
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const animations = new Set();
  let seen = false;
  try { seen = localStorage.getItem(storageKey) === 'seen'; } catch (_) {}
  let frame = null, lesson = null, token = '', priorFocus = null, priorOverflow = null;
  let introTimer = 0, fallbackTimer = 0, loadTimer = 0, closing = false, revision = 0;
  let introDeadlineReached = false;
  function animate(element, frames, duration = 280) {
    if (reduced.matches || !element.animate) return Promise.resolve();
    const animation = element.animate(frames, {duration, easing: 'cubic-bezier(.22,1,.36,1)'});
    animations.add(animation);
    return animation.finished.catch(() => {}).then(() => animations.delete(animation));
  }
  function settle() { for (const a of animations) a.cancel(); animations.clear(); }
  function changeLayout(update) {
    const before = panel.getBoundingClientRect();
    update();
    const after = panel.getBoundingClientRect();
    animate(panel, [{width: before.width + 'px', height: before.height + 'px'}, {width: after.width + 'px', height: after.height + 'px'}], 360);
  }
  function disposeFrame() { cue.pointTo(null); clearTimeout(loadTimer); if (frame) frame.remove(); frame = null; token = ''; }
  function resetHub() {
    disposeFrame(); lesson = null; dialog.classList.remove('ft-is-lesson');
    lessonView.hidden = true; hub.hidden = false; title.textContent = 'Welcome to FURATIC!';
  }
  function syncViewport() {
    if (!dialog.open) return;
    const viewport = window.visualViewport;
    dialog.style.height = viewport ? viewport.height + 'px' : '100%';
    dialog.style.top = viewport ? viewport.offsetTop + 'px' : '0px';
    dialog.classList.toggle('ft-short', (viewport ? viewport.height : innerHeight) < 550);
  }
  function open() {
    if (dialog.open) return;
    clearTimeout(introTimer); clearTimeout(fallbackTimer); observer.disconnect();
    closing = false; revision++; priorFocus = document.activeElement;
    priorOverflow = {value: document.documentElement.style.getPropertyValue('overflow'), priority: document.documentElement.style.getPropertyPriority('overflow')};
    document.documentElement.style.setProperty('overflow', 'hidden');
    resetHub(); dialog.showModal(); syncViewport(); toggle.setAttribute('aria-expanded', 'true'); title.focus({preventScroll: true});
    animate(dialog, [{backgroundColor: 'rgba(3,5,12,0)'}, {backgroundColor: 'rgba(3,5,12,.94)'}], 320);
    animate(panel, [{opacity: 0, transform: 'translateY(14px) scale(.985)'}, {opacity: 1, transform: 'none'}], 360);
  }
  function dismiss() {
    if (!dialog.open || closing) return;
    closing = true; revision++; clearTimeout(loadTimer); cue.pointTo(null);
    seen = true; try { localStorage.setItem(storageKey, 'seen'); } catch (_) {}
    const style = getComputedStyle(panel);
    const from = {opacity: style.opacity || '1', transform: style.transform || 'none'};
    settle();
    animate(dialog, [{backgroundColor: getComputedStyle(dialog).backgroundColor}, {backgroundColor: 'rgba(3,5,12,0)'}], 220);
    animate(panel, [from, {opacity: 0, transform: 'translateY(10px) scale(.985)'}], 220).then(() => {
      if (!closing) return;
      disposeFrame(); settle(); dialog.close(); closing = false;
      const style = document.documentElement.style;
      if (priorOverflow.value) style.setProperty('overflow', priorOverflow.value, priorOverflow.priority); else style.removeProperty('overflow');
      toggle.setAttribute('aria-expanded', 'false');
      (priorFocus instanceof HTMLElement && priorFocus.isConnected && priorFocus !== document.body ? priorFocus : toggle).focus({preventScroll: true});
    });
  }
  async function goHub() {
    const current = ++revision;
    cue.pointTo(null);
    await animate(lessonView, [{opacity: 1}, {opacity: 0}], 130);
    if (current !== revision || closing || !dialog.open) return;
    changeLayout(resetHub);
    animate(hub, [{opacity: 0, transform: 'translateY(6px)'}, {opacity: 1, transform: 'none'}]);
    title.focus({preventScroll: true});
  }
  async function start(key) {
    if (!Object.prototype.hasOwnProperty.call(labels, key) || closing) return;
    const current = ++revision;
    await animate(hub, [{opacity: 1}, {opacity: 0}], 130);
    if (current !== revision || !dialog.open || closing) return;
    disposeFrame(); lesson = key; title.textContent = labels[key];
    changeLayout(() => { hub.hidden = true; lessonView.hidden = false; dialog.classList.add('ft-is-lesson'); });
    instruction.textContent = ''; document.getElementById('ft-step').textContent = ''; next.hidden = true; loading.hidden = false; loading.textContent = 'Loading the player…';
    frame = document.createElement('iframe'); frame.className = 'ft-player'; frame.title = labels[key] + ' — interactive tutorial';
    frame.setAttribute('sandbox', 'allow-scripts allow-same-origin');
    token = window.crypto?.randomUUID?.() || Date.now().toString(36) + Math.random().toString(36).slice(2);
    frame.src = '/tutorial/player/?layout=framed&lesson=' + key + '&token=' + encodeURIComponent(token);
    region.append(frame);
    loadTimer = setTimeout(() => {
      if (lesson !== key || !frame || frame.classList.contains('ft-ready')) return;
      loading.textContent = 'The player could not load. Return to all tutorials and try again.';
    }, 12000);
    animate(lessonView, [{opacity: 0}, {opacity: 1}], 320);
    title.focus({preventScroll: true});
  }
  window.addEventListener('message', event => {
    if (!frame || event.source !== frame.contentWindow || event.origin !== location.origin || event.data?.token !== token || closing) return;
    const data = event.data;
    if (data.type === 'furatic:tutorial-close') { dismiss(); return; }
    if (data.type === 'furatic:tutorial-complete' && data.lesson === lesson) {
      seen = true; try { localStorage.setItem(storageKey, 'seen'); } catch (_) {}
      goHub(); return;
    }
    if (data.type !== 'furatic:tutorial-state' || data.lesson !== lesson || typeof data.instruction !== 'string') return;
    clearTimeout(loadTimer); loading.hidden = true; frame.classList.add('ft-ready');
    instruction.textContent = data.instruction;
    animate(instruction, [{opacity: 0}, {opacity: 1}], 220);
    document.getElementById('ft-step').textContent = (data.step + 1) + ' / ' + data.total;
    next.hidden = !data.next; next.disabled = false; next.textContent = data.next || 'Continue';
    cue.pointTo(data.cueInPlayer || !data.next ? null : next);
  });
  next.addEventListener('click', () => {
    if (!frame || next.disabled) return;
    cue.pointTo(null);
    next.disabled = true; frame.contentWindow.postMessage({type: 'furatic:tutorial-next', token}, location.origin);
  });
  hub.addEventListener('click', event => { const button = event.target.closest('[data-topic]'); if (button) start(button.dataset.topic); });
  toggle.addEventListener('click', open);
  close.addEventListener('click', () => dismiss());
  document.getElementById('ft-back').addEventListener('click', goHub);
  dialog.addEventListener('cancel', event => { event.preventDefault(); dismiss(); });
  function scheduleWelcome(force = false) {
    if (seen || dialog.open || document.hidden) return;
    if (!force && !document.body.classList.contains('is-logo-docked') && !document.body.classList.contains('is-ready')) return;
    clearTimeout(introTimer);
    // An iframe's autofocus is not evidence that the user dismissed the guide.
    introTimer = setTimeout(() => { if (!seen && !dialog.open && !document.hidden) open(); }, force ? 0 : 800);
  }
  const observer = new MutationObserver(() => scheduleWelcome());
  observer.observe(document.body, {attributes: true, attributeFilter: ['class']});
  window.addEventListener('furatic:intro-complete', () => scheduleWelcome(), {once: true});
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { for (const a of animations) { try { a.finish(); } catch (_) { a.cancel(); } } }
    else scheduleWelcome(introDeadlineReached);
  });
  window.addEventListener('resize', syncViewport);
  window.visualViewport?.addEventListener('resize', syncViewport);
  window.visualViewport?.addEventListener('scroll', syncViewport);
  fallbackTimer = setTimeout(() => { introDeadlineReached = true; scheduleWelcome(true); }, 7000);
  scheduleWelcome();
})();
