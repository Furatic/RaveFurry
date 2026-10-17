import {updateState} from './musiq/update';
import {initPanels} from './musiq/panels';
import {onReady as initTitles} from './musiq/title-scrolling';

// This entry is bundled separately: no events.ts, live request handlers, or
// vote transports are imported. All cards are rendered by the production UI.
export function onReady() {
  const local = window as any;
  if (!local.FURATIC_TUTORIAL_PLAYER) return;
  if (local.furaticTutorialMounted) return;
  local.furaticTutorialMounted = true;
  local.$ = local.jQuery = $;
  const lesson = local.FURATIC_TUTORIAL_LESSON || 'adding';
  const token = new URLSearchParams(location.search).get('token');
  const favorites = ['foxes', 'punk', 'rn', 'hooligang', 'jump'];
  const catalog = {
    foxes: ['FOXES', 'Red Means Recording', 'foxes', 153],
    punk: ['PUNK TACTICS', 'Joey Valence & Brae', 'punk', 143],
    rn: ['RN', 'Joey Valence & Brae', 'punk', 132],
    hooligang: ['HOOLIGANG', 'Joey Valence & Brae', 'punk', 152],
    jump: ['Double Jump', 'Joey Valence & Brae', 'jump', 142],
  };
  const copy = {
    adding: [
      'Tap the real song field below. Search a song or artist, or paste a YouTube or YouTube Music link. Try “Red Means Recording You Love Foxes”, then press the search button or Enter.',
      'FOXES is now in the main queue with one starting vote. On the website, songs may briefly download or wait for moderator review before they are ready.',
    ],
    voting: [
      'Tap the up arrow beside RN. An upvote can move a song ahead of songs with fewer votes.',
      'RN moved up, but PUNK TACTICS stayed first. Its lock means it is next to play, so other songs cannot cut ahead. Tap the lock or continue.',
      'HOOLIGANG is your song. Its highlighted card and vote controls are the same ones you see on the website. Your one starting vote is already included; you cannot vote on your own song again.',
      'Now tap the down arrow beside Double Jump. Watch its vote count and its place in the queue.',
      'Double Jump has zero votes and keeps its place. For queue ordering, one vote or fewer counts equally. Each listener has one vote choice. Next, simulate two other listeners downvoting it.',
      'At −2 total votes, Double Jump is removed. If it were currently playing, it would be skipped instead. Upvotes can move songs forward, but never past the locked next song.',
    ],
    multiple: [
      'Add your first song using the song field and search button below. The example is “Red Means Recording You Love Foxes”.',
      'FOXES is in the main queue. Add “Joey Valence & Brae PUNK TACTICS” using the same song field. Your extra song will go to the pre-queue.',
      'PUNK TACTICS is waiting in the pre-queue. Add “Joey Valence & Brae RN” next.',
      'Your extra songs keep the order you add them. Add “Joey Valence & Brae HOOLIGANG” next.',
      'Add “Joey Valence & Brae Double Jump”. You can have five active songs total, including your song if it is currently playing.',
      'You now have one song in the main queue and four extras in the pre-queue. Open the pre-queue to see them. Your oldest extra gets your next turn after your active song finishes.',
    ],
    prequeue: [
      'FOXES is your currently playing song. Your extra songs are waiting. Open the real pre-queue using its arrow.',
      'Your extras wait here in submission order. They cannot receive votes yet. Simulate FOXES finishing to see what happens next.',
      'Your oldest extra, PUNK TACTICS, moved into the main queue. RN keeps waiting because you already have an active song. PUNK TACTICS is locked as next up while another listener’s song plays.',
      'After PUNK TACTICS finishes, RN moves into the main queue and can receive votes there. Your remaining extra keeps waiting for your following turn.',
    ],
  };
  let step = 0;
  let typing = 0;
  let disposed = false;
  let typed = false;
  let advancing = false;
  const input = document.getElementById('music-input') as HTMLInputElement;
  const search = document.getElementById('request-new-music');
  const desktop = () => window.parent.matchMedia('(min-width: 1581px)').matches;
  function song(key, options: any = {}) {
    const [title, artist, cover, duration] = catalog[key];
    const id = favorites.indexOf(key) + 1;
    return {id, queueKey: id, occurrenceId: 'tutorial-' + key, index: id,
      title, artist, artworkUrl: local.FURATIC_TUTORIAL_ART_ROOT + cover + '.jpg',
      duration, effectiveDuration: duration, durationFormatted: Math.floor(Number(duration) / 60) + ':' + String(Number(duration) % 60).padStart(2, '0'),
      internalUrl: 'tutorial-only', externalUrl: '', votes: 1, priorityTier: 'normal',
      requestedBy: null, requesterVote: 1, upvotes: [], downvotes: [], ...options};
  }
  function queue() {
    if (lesson === 'adding') return {current: song('jump'), rows: step ? [song('foxes', {own: true, isNextUpLocked: true})] : []};
    if (lesson === 'multiple') return {current: null,
      rows: favorites.slice(0, step).map((key, i) => song(key, {own: true, priorityTier: i ? 'extra' : 'normal', isNextUpLocked: i === 0}))};
    if (lesson === 'prequeue') {
      if (step < 2) return {current: song('foxes', {own: true}), rows: [song('jump', {isNextUpLocked: true}), ...['punk', 'rn', 'hooligang'].map(key => song(key, {own: true, priorityTier: 'extra'}))]};
      if (step === 2) return {current: song('jump'), rows: [song('punk', {own: true, isNextUpLocked: true}), ...['rn', 'hooligang'].map(key => song(key, {own: true, priorityTier: 'extra'}))]};
      return {current: null, rows: [song('rn', {own: true, isNextUpLocked: true}), song('hooligang', {own: true, priorityTier: 'extra'})]};
    }
    const locked = song('punk', {isNextUpLocked: true});
    const rn = song('rn', {votes: step ? 2 : 1});
    const jump = song('jump', {votes: step >= 4 ? 0 : 1});
    const own = song('hooligang', {own: true});
    return {current: song('foxes'), rows: step === 0 ? [locked, jump, rn, own] : step < 5 ? [locked, rn, jump, own] : [locked, rn, own]};
  }
  function nextLabel() {
    if (step === copy[lesson].length - 1) return 'Done';
    if (lesson === 'voting') return step === 4 ? 'Two other listeners downvote' : step === 1 || step === 2 ? 'Continue' : '';
    if (lesson === 'prequeue') return step === 1 ? 'Simulate FOXES finishing' : step === 2 ? 'Simulate PUNK TACTICS finishing' : '';
    return '';
  }
  function notify() {
    if (disposed) return;
    window.parent.postMessage({type: 'furatic:tutorial-state', token, lesson, step, total: copy[lesson].length,
      instruction: copy[lesson][step], next: nextLabel()}, location.origin);
  }
  function target() {
    if (lesson === 'adding' || lesson === 'multiple') return step === copy[lesson].length - 1 ? '#auto-queue-toggle' : '#music-input';
    if (lesson === 'prequeue') return step < 2 ? '#auto-queue-toggle' : '#song-queue';
    return ['[data-queue-key="3"] .vote-up', '.next-up-lock', '[data-queue-key="4"] .vote-up', '[data-queue-key="5"] .vote-down', '[data-queue-key="5"] .queue-vote-count', '#song-queue'][step];
  }
  function render() {
    const {current, rows} = queue();
    const votes = [...rows, ...(current ? [current] : [])].map(item => ({occurrenceId: item.occurrenceId, choice: item.own ? 1 : lesson === 'voting' && item.id === 3 && step > 0 ? 1 : lesson === 'voting' && item.id === 5 && step >= 4 ? -1 : 0, voteRevision: step}));
    local.furaticTutorialOwnState = {songs: rows.filter(s => s.own).map(s => ({queueKey: s.id})), currentSongQueueKey: current?.own ? current.id : null, myVotes: votes};
    updateState({musiq: {currentSong: current, songQueue: rows, paused: true, progress: 23, volume: .5, totalTimeFormatted: '', myVotes: votes}, siteMode: 'normal'});
    document.dispatchEvent(new CustomEvent('furatic:refresh-personal-votes'));
    document.querySelectorAll('.furatic-tutorial-target').forEach(node => node.classList.remove('furatic-tutorial-target'));
    requestAnimationFrame(() => {
      if (disposed) return;
      const focus = document.querySelector<HTMLElement>(target());
      focus?.classList.add('furatic-tutorial-target');
      // Scroll only inside the real queue list; never move the landing page.
      const list = focus?.closest('#main-queue-scroll, #auto-drawer .furatic-list-scroll');
      if (list && focus) {
        const row = focus.closest('li');
        if (row) (list as HTMLElement).scrollTop = Math.max(0, (row as HTMLElement).offsetTop - 12);
      }
    });
    input.readOnly = (lesson !== 'adding' && lesson !== 'multiple') || step === copy[lesson].length - 1;
    notify();
  }
  function stopTyping() { window.clearTimeout(typing); typing = 0; }
  function fill() {
    if (input.readOnly) return;
    stopTyping(); typed = true;
    const key = lesson === 'adding' ? 'foxes' : favorites[Math.min(step, 4)];
    const example = key === 'foxes' ? 'Red Means Recording You Love Foxes' : catalog[key][1] + ' ' + catalog[key][0];
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) { input.value = example; return; }
    let index = 0;
    const type = () => { if (disposed) return; input.value = example.slice(0, ++index); if (index < example.length) typing = window.setTimeout(type, 28); };
    type();
  }
  function submit() {
    if (input.readOnly || !input.value.trim()) { input.focus(); if (!typed) fill(); return; }
    stopTyping(); input.value = ''; typed = false; step += 1; render();
  }
  function advance() {
    if (advancing || !nextLabel()) return;
    if (step === copy[lesson].length - 1) {
      window.parent.postMessage({type: 'furatic:tutorial-complete', token, lesson}, location.origin); return;
    }
    if (lesson === 'prequeue') {
      advancing = true;
      local.furaticQueuePanels.close().then(() => { advancing = false; if (!disposed) { step += 1; render(); } });
    }
    else { step += 1; render(); }
  }
  local.furaticTutorialPanels = {
    syncLayout: () => { local.furaticQueuePanels?.setDesktop(desktop()); local.furaticQueuePanels?.setSpace(desktop() ? local.furaticQueuePanels.current() === 'auto' ? 570 : 136 : 0); },
    setDrawer: (drawer) => local.furaticQueuePanels?.setSpace(desktop() ? drawer === 'auto' ? 570 : 136 : 0),
    beforeDrawer: () => Promise.resolve(),
  };
  initPanels(() => {});
  initTitles();
  document.querySelector('.furatic-embed-shell').classList.add('furatic-shell-mounted', 'furatic-intro-complete');
  input.removeAttribute('autofocus');
  search.setAttribute('role', 'button'); search.setAttribute('tabindex', '0'); search.setAttribute('aria-label', 'Search for this song');
  input.addEventListener('focus', () => { if (!typed && !input.value) fill(); });
  input.addEventListener('beforeinput', () => { stopTyping(); typed = true; });
  input.addEventListener('input', () => { stopTyping(); typed = true; });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') { window.parent.postMessage({type: 'furatic:tutorial-close', token}, location.origin); return; }
    if (event.key === 'Enter' && event.target === input) { event.preventDefault(); submit(); }
    else if ((event.key === 'Enter' || event.key === ' ') && event.target instanceof HTMLElement && event.target.matches('[role="button"], .next-up-lock')) { event.preventDefault(); event.target.click(); }
  }, true);
  document.addEventListener('click', event => {
    const element = event.target instanceof Element ? event.target : null;
    if (!element) return;
    if (element.closest('#music-input') && !typed && !input.value) { fill(); return; }
    if (element.closest('#request-button-container')) { event.preventDefault(); event.stopImmediatePropagation(); submit(); return; }
    if (element.closest('#clearbutton')) { stopTyping(); input.value = ''; typed = true; return; }
    if (element.closest('#queue-history-button, .own-song-remove, a')) { event.preventDefault(); event.stopImmediatePropagation(); return; }
    if (element.closest('#auto-queue-toggle') && lesson === 'prequeue' && step === 0) { step = 1; notify(); }
    if (lesson !== 'voting') return;
    if (step === 1 && element.closest('.next-up-lock')) { advance(); return; }
    const row = element.closest('[data-queue-key]');
    if (step === 0 && row?.getAttribute('data-queue-key') === '3' && element.closest('.vote-up') || step === 3 && row?.getAttribute('data-queue-key') === '5' && element.closest('.vote-down')) {
      event.preventDefault(); event.stopImmediatePropagation(); step += 1; render();
    }
  }, true);
  document.addEventListener('submit', event => event.preventDefault());
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.origin !== location.origin || event.data?.token !== token) return;
    if (event.data.type === 'furatic:tutorial-next') advance();
    if (event.data.type === 'furatic:tutorial-fill') { input.focus(); fill(); }
  });
  window.addEventListener('resize', local.furaticTutorialPanels.syncLayout);
  window.addEventListener('pagehide', () => { disposed = true; stopTyping(); });
  render();
}

$(document).ready(onReady);
