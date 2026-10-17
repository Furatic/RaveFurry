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

type Artwork = {node: HTMLElement, occurrence: string, bounds: DOMRect};
type PreQueueStart = {bounds: DOMRect, preview: DOMRect, content: DOMRect, artwork: Artwork[], labels: {node: HTMLElement, bounds: DOMRect}[]};
let previewObserver: ResizeObserver;
let preQueueMorphing = false;

function fitAutoPreview() {
  const preview = document.getElementById('auto-queue-preview');
  if (!preview || preQueueMorphing || document.getElementById('auto-queue-dock').classList.contains('is-expanded')) return;
  const vertical = desktopLayout();
  const space = vertical ? preview.clientHeight : preview.clientWidth;
  const tile = vertical ? preview.clientWidth : 44;
  if (!space || !tile) return;
  const style = getComputedStyle(preview);
  const gap = parseFloat(vertical ? style.rowGap : style.columnGap) || 0;
  const count = Math.min(10, Math.max(0, Math.floor((space + gap) / (tile + gap))));
  Array.from(preview.children).forEach((node: HTMLElement, index) => { node.hidden = index >= count; });
}

function captureArtwork(): Artwork[] {
  const expanded = document.getElementById('auto-queue-dock').classList.contains('is-expanded');
  const container = document.getElementById(expanded ? 'auto-drawer' : 'auto-queue-preview');
  const clip = container.getBoundingClientRect();
  return Array.from(container.querySelectorAll<HTMLElement>(expanded ? '.queue-artwork' : '.auto-queue-thumbnail'))
      .map(node => ({node, bounds: node.getBoundingClientRect(),
        occurrence: node.closest<HTMLElement>('[data-occurrence-id]')?.dataset.occurrenceId}))
      .filter(item => item.occurrence && item.bounds.width > 0 && item.bounds.height > 0 &&
        item.bounds.top >= clip.top && item.bounds.bottom <= clip.bottom);
}

function capturePreQueue(): PreQueueStart {
  const dock = document.getElementById('auto-queue-dock');
  const preview = document.getElementById('auto-queue-preview');
  return {bounds: dock.getBoundingClientRect(), preview: preview.getBoundingClientRect(),
    content: document.getElementById('auto-drawer').getBoundingClientRect(), artwork: captureArtwork(),
    labels: Array.from(dock.querySelectorAll<HTMLElement>('#auto-queue-toggle > strong, #auto-queue-toggle > i'))
        .map(node => ({node, bounds: node.getBoundingClientRect()}))};
}

async function morphPreQueue(start: PreQueueStart, opening: boolean, reduced: boolean): Promise<void> {
  const dock = document.getElementById('auto-queue-dock');
  const preview = document.getElementById('auto-queue-preview');
  const content = document.getElementById('auto-drawer');
  const savedTransition = dock.style.transition;
  dock.style.transition = 'none';
  const end = dock.getBoundingClientRect();
  const timing = {duration: 600, easing: 'cubic-bezier(.4, 0, .2, 1)', fill: 'both' as FillMode};
  const animations: Animation[] = [];
  const savedPreviewStyle = preview.getAttribute('style');
  const savedContentStyle = content.getAttribute('style');
  const artworkLayers: HTMLElement[] = [];
  const hiddenArtwork: {node: HTMLElement, visibility: string}[] = [];
  const destinations = start.labels.map(label => ({...label, destination: label.node.getBoundingClientRect()}));
  if (!reduced && typeof dock.animate === 'function') {
    // Keep the expanded rows at their original geometry while the surface
    // narrows, so artwork and text do not reflow on every animation frame.
    const contentBounds = opening ? content.getBoundingClientRect() : start.content;
    const contentOrigin = opening ? end : start.bounds;
    Object.assign(content.style, {position: 'absolute', left: contentBounds.left - contentOrigin.left + 'px',
      top: contentBounds.top - contentOrigin.top + 'px', width: contentBounds.width + 'px', height: contentBounds.height + 'px'});
    fitAutoPreview();
    const destinationArtwork = captureArtwork();
    preQueueMorphing = true;
    const previewBounds = opening ? start.preview : preview.getBoundingClientRect();
    if (previewBounds.width) {
      const origin = opening ? start.bounds : end;
      Object.assign(preview.style, {position: 'absolute', display: 'grid', left: previewBounds.left - origin.left + 'px',
        top: previewBounds.top - origin.top + 'px', width: previewBounds.width + 'px', height: previewBounds.height + 'px'});
    } else preview.style.display = 'grid';
    // Animate only after measuring the final compact preview.
    animations.push(dock.animate([{width: start.bounds.width + 'px'}, {width: end.width + 'px'}], timing));
    start.artwork.forEach(source => {
      const destination = destinationArtwork.find(item => item.occurrence === source.occurrence);
      const layer = source.node.cloneNode(true) as HTMLElement;
      layer.removeAttribute('id'); layer.removeAttribute('href'); layer.setAttribute('aria-hidden', 'true');
      layer.removeAttribute('data-occurrence-id');
      layer.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
      Object.assign(layer.style, {position: 'fixed', display: 'grid', left: source.bounds.left + 'px', top: source.bounds.top + 'px',
        width: source.bounds.width + 'px', height: source.bounds.height + 'px', minWidth: '0', maxWidth: 'none',
        margin: '0', objectFit: 'cover', borderRadius: '8px', overflow: 'hidden', zIndex: '20', pointerEvents: 'none',
        transformOrigin: 'top left', transition: 'none', animation: 'none'});
      document.body.appendChild(layer); artworkLayers.push(layer);
      [source.node, destination?.node].filter(Boolean).forEach(node => {
        hiddenArtwork.push({node, visibility: node.style.visibility}); node.style.visibility = 'hidden';
      });
      animations.push(layer.animate(destination ? [{transform: 'translate(0px, 0px) scale(1, 1)'},
        {transform: `translate(${destination.bounds.left - source.bounds.left}px, ${destination.bounds.top - source.bounds.top}px) scale(${destination.bounds.width / source.bounds.width}, ${destination.bounds.height / source.bounds.height})`}] :
        [{opacity: 1}, {opacity: 0, offset: .6}, {opacity: 0}], timing));
    });
    animations.push(preview.animate(opening ?
      [{opacity: 1}, {opacity: 0, offset: .35}, {opacity: 0}] :
      [{opacity: 0}, {opacity: 0, offset: .4}, {opacity: 1}], timing));
    animations.push(content.animate(opening ?
      [{opacity: 0}, {opacity: 0, offset: .2}, {opacity: 1}] :
      [{opacity: 1}, {opacity: 0, offset: .6}, {opacity: 0}], timing));
    destinations.forEach(({node, bounds, destination}) => {
      animations.push(node.animate([
        {translate: (bounds.left - destination.left) + 'px ' + (bounds.top - destination.top) + 'px'},
        {translate: '0px 0px'}
      ], timing));
    });
  }
  // Expanding the parent necessarily resizes this iframe throughout the morph.
  // Only a desktop/mobile mode change may settle it early.
  const resize = () => { if (!desktopLayout()) animations.forEach(animation => animation.cancel()); };
  window.addEventListener('resize', resize);
  await Promise.all(animations.map(animation => animation.finished.catch(() => undefined)));
  window.removeEventListener('resize', resize);
  animations.forEach(animation => animation.cancel());
  dock.style.transition = savedTransition;
  artworkLayers.forEach(layer => layer.remove());
  hiddenArtwork.forEach(({node, visibility}) => { node.style.visibility = visibility; });
  if (savedPreviewStyle === null) preview.removeAttribute('style'); else preview.setAttribute('style', savedPreviewStyle);
  if (savedContentStyle === null) content.removeAttribute('style'); else content.setAttribute('style', savedContentStyle);
  if (!opening) content.hidden = true;
  preQueueMorphing = false;
  fitAutoPreview();
}


async function transition(panel: Panel, opening: boolean): Promise<void> {
  const element = document.getElementById(panel + '-drawer');
  if (!element) return;
  if (opening && desktopLayout()) await parentPanels()?.beforeDrawer();
  const desktop = desktopLayout();
  const preQueueStart = panel === 'auto' && desktop ? capturePreQueue() : null;
  document.documentElement.classList.toggle('furatic-history-open', desktop && panel === 'history' && opening);
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
  if (preQueueStart) {
    await morphPreQueue(preQueueStart, opening, reduced);
    return;
  }
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
  if (typeof ResizeObserver !== 'undefined') {
    previewObserver = new ResizeObserver(fitAutoPreview);
    previewObserver.observe(document.getElementById('auto-queue-preview'));
  }
  (window as any).furaticQueuePanels = {
    close: () => controller.show(null),
    current: () => controller.active && document.getElementById(controller.active + '-drawer').classList.contains('is-open') ? controller.active : null,
    setDesktop: (desktop: boolean) => {
      document.documentElement.classList.toggle('furatic-desktop-panels', desktop);
      document.documentElement.classList.toggle('furatic-mobile-compact', !desktop);
      document.documentElement.classList.toggle('furatic-history-open', desktop && controller.active === 'history' &&
        document.getElementById('history-drawer').classList.contains('is-open'));
      const autoDrawer = document.getElementById('auto-drawer');
      const home = document.querySelector(desktop ? '#auto-queue-dock' : '.furatic-queue-middle');
      if (autoDrawer.parentElement !== home) home.insertBefore(autoDrawer, desktop ? null : document.getElementById('history-drawer'));
      document.getElementById('main-queue-scroll').toggleAttribute('inert', !desktop && Boolean(controller.active));
      parentPanels()?.setDrawer((window as any).furaticQueuePanels.current());
      fitAutoPreview();
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
  const first = songs.slice(0, 10);
  preview.classList.toggle('is-empty', first.length === 0);
  const key = JSON.stringify(first.map(s => [s.occurrenceId, s.artworkUrl, s.artist, s.title]));
  if (preview.dataset.previewKey !== key) {
    preview.dataset.previewKey = key;
    preview.replaceChildren(...first.map(song => {
      const link = document.createElement('a');
      link.className = 'auto-queue-thumbnail';
      link.dataset.occurrenceId = String(song.occurrenceId || song.id);
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
  fitAutoPreview();
}

export function refreshOpenHistory() {
  if (controller && controller.desired === 'history') loadHistory();
}
