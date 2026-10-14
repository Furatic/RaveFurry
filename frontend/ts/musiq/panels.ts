import {displayName} from './display-name';

/** One transition owner keeps mutually exclusive drawers from crossing each other. */
export type Panel = 'auto' | 'history' | null;
export class DrawerController {
  active: Panel = null;
  desired: Panel = null;
  private running = false;
  private completion: Promise<void> = Promise.resolve();
  constructor(private transition: (panel: Panel, opening: boolean) => Promise<void>) {}
  show(panel: Panel): Promise<void> {
    this.desired = panel;
    if (this.running) return this.completion;
    this.running = true;
    this.completion = this.run();
    return this.completion;
  }
  private async run() {
    try {
      while (this.active !== this.desired) {
        if (this.active) {
          const old = this.active;
          await this.transition(old, false);
          this.active = null;
        }
        const next = this.desired;
        if (next) {
          this.active = next;
          await this.transition(next, true);
        }
      }
    } finally { this.running = false; }
  }
  toggle(panel: Panel) { return this.show(this.desired === panel ? null : panel); }
}

let controller: DrawerController;
let historyCursor: number | null = null;
let historyBusy = false;
let historyLoadedAt = 0;
let historySongs: any[] = [];
let renderHistory: (songs: any[]) => void;

async function loadHistory(more = false) {
  if (historyBusy) return;
  historyBusy = true;
  const status = document.getElementById('history-status');
  const button = document.getElementById('history-more') as HTMLButtonElement;
  if (button) button.disabled = true;
  if (status && !historySongs.length) status.textContent = 'Loading recently played songs…';
  const abort = new AbortController();
  const timeout = window.setTimeout(() => abort.abort(), 9000);
  try {
    const url = new URL(urls['musiq']['played-history'], window.location.href);
    if (more && historyCursor) url.searchParams.set('before', String(historyCursor));
    const response = await fetch(url.toString(), {credentials: 'same-origin', cache: 'no-store', signal: abort.signal});
    if (!response.ok) throw new Error('History is temporarily unavailable.');
    const result = await response.json();
    historyCursor = result.nextCursor || null;
    historySongs = more ? historySongs.concat(result.songs || []) : result.songs || [];
    const seen = new Set();
    const cutoff = Date.now() - 12 * 3600000;
    historySongs = historySongs.filter(song => Date.parse(song.endedAt) >= cutoff &&
      !seen.has(song.id) && Boolean(seen.add(song.id)));
    renderHistory(historySongs);
    historyLoadedAt = Date.now();
    if (status) status.textContent = historySongs.length ? '' : 'No songs have played in the last 12 hours.';
    if (button) button.hidden = !historyCursor;
  } catch (_error) {
    if (status) status.textContent = 'History could not load. Close and reopen it to try again.';
  } finally {
    window.clearTimeout(timeout);
    historyBusy = false;
    if (button) button.disabled = false;
  }
}

function desktopLayout(): boolean {
  return document.documentElement.classList.contains('furatic-desktop-panels');
}

function parentPanels() {
  try { return window.parent !== window ? (window.parent as any).furaticPlayerPanels : null; } catch (_error) { return null; }
}

async function transition(panel: Panel, opening: boolean): Promise<void> {
  const element = document.getElementById(panel + '-drawer');
  if (!element) return;
  if (opening && desktopLayout()) await parentPanels()?.beforeDrawer();
  const desktop = desktopLayout();
  if (desktop) parentPanels()?.setDrawer(opening ? panel : null);
  const historyButton = document.getElementById('queue-history-button');
  const autoButton = document.getElementById('auto-queue-toggle');
  const control = panel === 'auto' ? autoButton : historyButton;
  if (control) control.setAttribute('aria-expanded', String(opening));
  if (panel === 'auto') {
    document.getElementById('auto-queue-dock').classList.toggle('is-expanded', opening);
    document.getElementById('auto-queue-preview').toggleAttribute('inert', opening);
    document.getElementById('auto-queue-preview').setAttribute('aria-hidden', String(opening));
  }
  element.hidden = false;
  element.setAttribute('aria-hidden', String(!opening));
  element.toggleAttribute('inert', !opening);
  const queue = document.getElementById('main-queue-scroll');
  queue.toggleAttribute('inert', opening && !desktop);
  if (opening) element.classList.add('is-open'); else element.classList.remove('is-open');
  if (opening && panel === 'history' && Date.now() - historyLoadedAt > 10000) loadHistory();
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced || typeof element.animate !== 'function') {
    if (!opening) element.hidden = true;
    return;
  }
  const from = desktop ? {opacity: 0, transform: 'translateX(72px)'} :
    {opacity: 0, transform: panel === 'auto' ? 'translateY(36px)' : 'translateY(12px)'};
  const to = desktop ? {opacity: 1, transform: 'translateX(0)'} : {opacity: 1, transform: 'translateY(0)'};
  const animation = element.animate(opening ? [from, to] : [to, from], {
    duration: desktop ? 600 : 420, easing: 'cubic-bezier(.4, 0, .2, 1)', fill: 'both',
  });
  await animation.finished.catch(() => undefined).then(() => {
    animation.cancel();
    if (!opening) element.hidden = true;
  });
}

export function initPanels(renderer: (songs: any[]) => void) {
  if (!document.getElementById('auto-drawer')) return;
  renderHistory = renderer;
  controller = new DrawerController(transition);
  (window as any).furaticQueuePanels = {
    close: () => controller.show(null),
    current: () => controller.active && document.getElementById(controller.active + '-drawer').classList.contains('is-open') ? controller.active : null,
    setDesktop: (desktop: boolean) => {
      document.documentElement.classList.toggle('furatic-desktop-panels', desktop);
      document.getElementById('main-queue-scroll').toggleAttribute('inert', !desktop && Boolean(controller.active));
      parentPanels()?.setDrawer((window as any).furaticQueuePanels.current());
    },
    setSpace: (space: number) => document.documentElement.style.setProperty('--desktop-side-space', space + 'px'),
    setVisibleHeight: (height: number) => document.documentElement.style.setProperty('--furatic-visible-height', height + 'px'),
  };
  parentPanels()?.syncLayout();
  document.getElementById('queue-history-button').addEventListener('click', () => controller.toggle('history'));
  document.getElementById('auto-queue-toggle').addEventListener('click', () => controller.toggle('auto'));
  document.getElementById('history-close').addEventListener('click', () => {
    controller.show(null);
    document.getElementById('queue-history-button').focus();
  });
  document.getElementById('history-more').addEventListener('click', () => loadHistory(true));
  window.setInterval(() => {
    const filtered = historySongs.filter(song => Date.parse(song.endedAt) >= Date.now() - 12 * 3600000);
    if (filtered.length !== historySongs.length) {
      historySongs = filtered; renderHistory(historySongs);
      if (!filtered.length) document.getElementById('history-status').textContent = 'No songs have played in the last 12 hours.';
    }
  }, 60000);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && controller.desired) {
      controller.show(null);
      document.getElementById('queue-history-button').focus();
    }
  });
}

export function updateAutoPreview(songs: any[]) {
  const preview = document.getElementById('auto-queue-preview');
  if (!preview) return;
  const first = songs.slice(0, 6);
  preview.classList.toggle('is-empty', first.length === 0);
  const key = JSON.stringify(first.map(s => [s.occurrenceId, s.artworkUrl, s.artist, s.title]));
  if (preview.dataset.previewKey !== key) {
    preview.dataset.previewKey = key;
    preview.replaceChildren(...first.map(song => {
      const link = document.createElement('a');
      link.className = 'auto-queue-thumbnail';
      link.title = displayName(song);
      link.setAttribute('aria-label', link.title);
      if (/^https?:\/\//i.test(song.externalUrl)) link.href = song.externalUrl;
      link.target = '_blank'; link.rel = 'noopener noreferrer';
      if (song.artworkUrl) {
        const img = document.createElement('img'); img.src = song.artworkUrl; img.alt = ''; img.loading = 'lazy';
        link.appendChild(img);
      } else {
        const icon = document.createElement('i'); icon.className = 'fas fa-music'; icon.setAttribute('aria-hidden', 'true');
        link.appendChild(icon);
      }
      return link;
    }));
    if (!first.length) preview.textContent = 'No extra songs waiting.';
  }
  document.getElementById('auto-queue-count').textContent = String(songs.length);
  document.getElementById('auto-queue-empty').hidden = songs.length > 0;
}

export function refreshOpenHistory() {
  if (controller && controller.desired === 'history') loadHistory();
}
