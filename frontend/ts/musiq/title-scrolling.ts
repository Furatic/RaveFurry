type ScrollingTitle = {content: HTMLElement, animation: Animation | null, text: string, width: number, distance: number};
const titles = new Map<HTMLElement, ScrollingTitle>();
let started = false;
let scheduled = 0;
let resizeObserver: ResizeObserver;
let mutationObserver: MutationObserver;

/** Reuse one animation per title; ordinary state polls must not restart it. */
export function syncTitleScrolling() {
  for (const [title, previous] of titles) {
    if (!title.isConnected || previous.content.parentElement !== title) {
      previous.animation?.cancel();
      resizeObserver?.unobserve(title);
      titles.delete(title);
    }
  }
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.querySelectorAll<HTMLElement>('#current-song-title, #song-queue .queue-title, #auto-song-queue .queue-title, #history-song-queue .queue-title').forEach(title => {
    let previous = titles.get(title);
    let content = previous?.content || title.querySelector<HTMLElement>(':scope > .furatic-title-content');
    if (!content) {
      content = document.createElement('span');
      content.className = 'furatic-title-content';
      content.style.setProperty('display', 'inline-block', 'important');
      Object.assign(content.style, {width: 'max-content', maxWidth: 'none', whiteSpace: 'nowrap'});
      while (title.firstChild) content.appendChild(title.firstChild);
      title.appendChild(content);
      Object.assign(title.style, {display: 'block', overflow: 'hidden', whiteSpace: 'nowrap'});
    }
    if (!previous) resizeObserver?.observe(title);
    const style = getComputedStyle(title);
    const width = Math.max(0, title.clientWidth - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0));
    const distance = Math.max(0, content.scrollWidth - width);
    const text = content.textContent || '';
    const animate = !reduced && width > 0 && distance > 1 && typeof content.animate === 'function';
    title.style.textOverflow = reduced ? 'ellipsis' : 'clip';
    if (previous && previous.text === text && previous.width === width && previous.distance === distance && Boolean(previous.animation) === animate) return;
    previous?.animation?.cancel();
    let animation: Animation = null;
    if (animate) {
      const rest = 2000;
      const travel = Math.max(1000, distance / 30 * 1000);
      const duration = 2 * (rest + travel);
      animation = content.animate([
        {transform: 'translateX(0px)', offset: 0},
        {transform: 'translateX(0px)', offset: rest / duration, easing: 'ease-in-out'},
        {transform: `translateX(${-distance}px)`, offset: (rest + travel) / duration},
        {transform: `translateX(${-distance}px)`, offset: (2 * rest + travel) / duration, easing: 'ease-in-out'},
        {transform: 'translateX(0px)', offset: 1},
      ], {duration, iterations: Infinity});
    }
    titles.set(title, {content, animation, text, width, distance});
  });
}

function scheduleScrolling() {
  if (scheduled) return;
  scheduled = window.requestAnimationFrame(() => { scheduled = 0; syncTitleScrolling(); });
}

function observeTitles() {
  document.querySelectorAll('#current-song, #song-queue, #auto-song-queue, #history-song-queue').forEach(root => {
    mutationObserver.observe(root, {childList: true, subtree: true, characterData: true});
  });
  scheduleScrolling();
}

/** Scroll overflowing current/queued titles, including resized and newly opened drawers. */
export function onReady() {
  if (started) return;
  started = true;
  if (typeof ResizeObserver !== 'undefined') resizeObserver = new ResizeObserver(scheduleScrolling);
  mutationObserver = new MutationObserver(scheduleScrolling);
  observeTitles();
  $('#current-song-title').on('change', scheduleScrolling);
  window.addEventListener('resize', scheduleScrolling);
  window.matchMedia('(prefers-reduced-motion: reduce)').addEventListener?.('change', scheduleScrolling);
  document.fonts?.ready.then(scheduleScrolling);
  window.addEventListener('pagehide', () => {
    window.cancelAnimationFrame(scheduled); scheduled = 0;
    mutationObserver.disconnect(); resizeObserver?.disconnect();
    titles.forEach(title => title.animation?.cancel()); titles.clear();
  });
  window.addEventListener('pageshow', observeTitles);
}

$(document).ready(() => {
  if (!["/musiq/", "/p/"].includes(window.location.pathname)) return;
  onReady();
});
