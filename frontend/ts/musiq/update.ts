import {registerSpecificState} from '../base';
import {getStoredVote, paintVotes} from './vote-state';
import {initPanels, updateAutoPreview, refreshOpenHistory} from './panels';
import {showPlayButton, showPauseButton} from './buttons';
import {syncAudioStream} from './audio';
import {displayArtist} from './display-name';

export let state = null;
const rowAnimations = new Map<HTMLElement, Animation>();
let lastStateReceivedAt = 0;
let promotingOccurrence: string | null = null;
let currentPromotion: Animation | null = null;
let restorePromotionLayer: (() => void) | null = null;

function capturePromotion(newSong, oldState) {
  if (!newSong || !oldState || oldState.currentSong?.occurrenceId === newSong.occurrenceId ||
      window.matchMedia('(prefers-reduced-motion: reduce)').matches) return null;
  const row = Array.from(document.querySelectorAll<HTMLElement>('#song-queue > li'))
      .find(node => node.dataset.occurrenceId === String(newSong.occurrenceId));
  const card = document.getElementById('current-song');
  const scroll = document.getElementById('main-queue-scroll');
  if (!row || !card || typeof card.animate !== 'function' || scroll?.hasAttribute('inert')) return null;
  const from = row.getBoundingClientRect();
  const viewport = (scroll || row.parentElement).getBoundingClientRect();
  if (!from.height || from.bottom <= viewport.top || from.top >= viewport.bottom) return null;
  if (currentPromotion) {
    currentPromotion.cancel(); currentPromotion = null;
    restorePromotionLayer?.(); restorePromotionLayer = null;
  }
  const ghost = oldState.currentSong ? card.cloneNode(true) as HTMLElement : null;
  const oldRect = card.getBoundingClientRect();
  if (ghost) {
    const originals = [card, ...Array.from(card.querySelectorAll<HTMLElement>('*'))];
    const copies = [ghost, ...Array.from(ghost.querySelectorAll<HTMLElement>('*'))];
    originals.forEach((node, index) => {
      const style = window.getComputedStyle(node);
      // The departing card has no IDs; preserve its measured appearance during the crossfade.
      for (const property of Array.from(style)) copies[index].style.setProperty(property, style.getPropertyValue(property));
    });
    ghost.removeAttribute('id');
    ghost.removeAttribute('data-occurrence-id'); ghost.removeAttribute('data-queue-key');
    ghost.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
    ghost.setAttribute('inert', ''); ghost.setAttribute('aria-hidden', 'true');
    Object.assign(ghost.style, {position: 'fixed', left: oldRect.left + 'px', top: oldRect.top + 'px',
      width: oldRect.width + 'px', height: oldRect.height + 'px', margin: '0', zIndex: '19', pointerEvents: 'none'});
  }
  return {from, ghost, card, occurrence: String(newSong.occurrenceId)};
}

function finishPromotion(promotion) {
  if (!promotion) return;
  const {from, ghost, card} = promotion;
  const to = card.getBoundingClientRect();
  if (!to.width || !to.height) return;
  if (ghost) {
    document.body.appendChild(ghost);
    const fade = ghost.animate([{opacity: 1}, {opacity: 0}], {duration: 260, easing: 'ease-out', fill: 'both'});
    fade.finished.catch(() => undefined).then(() => { fade.cancel(); ghost.remove(); });
  }
  const motion = card.animate([
    {transformOrigin: 'top left', transform: `translate(${from.left - to.left}px, ${from.top - to.top}px) scale(${from.width / to.width}, ${from.height / to.height})`},
    {transformOrigin: 'top left', transform: 'translate(0, 0) scale(1, 1)'},
  ], {duration: 560, easing: 'cubic-bezier(.22,1,.36,1)'});
  const previousZ = card.style.zIndex;
  const layer = card.parentElement;
  const previousLayerZ = layer.style.zIndex;
  const previousPosition = layer.style.position;
  card.style.zIndex = '20';
  layer.style.position = 'relative'; layer.style.zIndex = '20';
  restorePromotionLayer = () => {
    card.style.zIndex = previousZ;
    layer.style.zIndex = previousLayerZ; layer.style.position = previousPosition;
  };
  currentPromotion = motion;
  motion.finished.catch(() => undefined).then(() => {
    if (currentPromotion === motion) {
      currentPromotion = null; restorePromotionLayer?.(); restorePromotionLayer = null; motion.cancel();
    }
  });
}

function formatSeconds(totalSeconds) {
  totalSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));

  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (hours > 0) {
    return hours + ':' + String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');
  }

  return minutes + ':' + String(seconds).padStart(2, '0');
}

function currentSongDuration(song) {
  if (!song) {
    return 0;
  }

  return Number(song.effectiveDuration || song.duration || 0);
}

function currentSongPlayedSeconds() {
  if (state == null || state.currentSong == null) {
    return 0;
  }

  const duration = currentSongDuration(state.currentSong);
  const serverProgress = Number(state.progress || 0);
  let played = duration * serverProgress / 100;

  if (!state.paused && lastStateReceivedAt > 0) {
    played += (Date.now() - lastStateReceivedAt) / 1000;
  }

  return Math.max(0, Math.min(duration, played));
}

function updateCurrentSongTimeLabels() {
  const elapsed = $('#current-song-time-elapsed');
  const total = $('#current-song-time-total');

  if (!elapsed.length || !total.length) {
    return;
  }

  if (state == null || state.currentSong == null) {
    elapsed.text('0:00');
    total.text('--:--');
    return;
  }

  const duration = currentSongDuration(state.currentSong);
  elapsed.text(formatSeconds(currentSongPlayedSeconds()));
  total.text(formatSeconds(duration));
}

const downloadSvg = `
<svg version="1.1" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
 <g>
  <path d="m41.5 8v40h-16.5l25 25 25-25h-16.5v-40z"/>
  <rect x="17.5" y="86.5" width="65" height="8.5"/>
 </g>
</svg>`;

/** Clear any stored state. */
export function clearState() {
  state = null;
}

/** Update the musiq state.
 * @param {Object} newState an object containing all state information */
export function updateState(newState) {
  lastStateReceivedAt = Date.now();

  if (!('musiq' in newState)) {
    // this state is not meant for a musiq update
    return;
  }
  // create deep copy
  let oldState = null;
  if (state != null) {
    oldState = jQuery.extend(true, {}, state);
  }

  if (newState.playbackError) {
    $('#current-song-title').css('color', 'var(--red)');
  } else {
    $('#current-song-title').css('color', 'var(--normal-text)');
  }

  const currentSong = newState.musiq.currentSong;
  const promotion = capturePromotion(currentSong, oldState);
  promotingOccurrence = promotion?.occurrence || null;
  if (currentSong == null) {
    state = newState.musiq;
    $('#current-song-title').empty();
    $('#current-song-title').append($('<em/>').text('Currently Empty'));
    $('#current-song-title').trigger('change');
    $('#current-song').removeClass('present own-song-current');
    $('#current-song').addClass('empty');
    $('#current-song').removeAttr('data-queue-key data-occurrence-id');
    $('#current-song-title').removeAttr('href');
    $('#current-song-artwork')
        .removeAttr('src')
        .attr('hidden', 'hidden')
        .hide();

    $('#song-votes .vote-down').removeClass('pressed');
    $('#song-votes .vote-up').removeClass('pressed');
    $('#current-song-votes').text(0);

    $('#progress-bar').css('transition', 'none');
    $('#progress-bar').css('width', '0%');
    // Trigger a reflow, flushing the CSS changes
    $('#progress-bar')[0].offsetHeight;

    updateCurrentSongTimeLabels();
    showPlayButton();
  } else {
    state = newState.musiq;

    if (oldState == null ||
      oldState.currentSong == null ||
      oldState.currentSong.id != state.currentSong.id) {
      // only update the current song title if it changed,
      // so the marquee effect does not reset
      insertDisplayName($('#current-song-title'), currentSong);
      $('#current-song-title').trigger('change');
    }

    $('#current-song').removeClass('empty').addClass('present');
    $('#current-song').attr('data-queue-key', String(currentSong.queueKey)).attr('data-occurrence-id', currentSong.occurrenceId);
    $('#current-song-title').attr('href', safeExternalUrl(currentSong.externalUrl));
    
    if (currentSong.artworkUrl) {
      $('#current-song-artwork')
          .attr('src', currentSong.artworkUrl)
          .removeAttr('hidden')
          .show();
    } else {
      $('#current-song-artwork')
          .removeAttr('src')
          .attr('hidden', 'hidden')
          .hide();
    }

    const previousVote = getStoredVote(currentSong.occurrenceId);
    if (previousVote == '+') {
      $('#song-votes .vote-up').addClass('pressed');
      $('#song-votes .vote-down').removeClass('pressed');
    } else if (previousVote == '-') {
      $('#song-votes .vote-down').addClass('pressed');
      $('#song-votes .vote-up').removeClass('pressed');
    } else {
      $('#song-votes .vote-down').removeClass('pressed');
      $('#song-votes .vote-up').removeClass('pressed');
    }

    $('#current-song-votes').text(currentSong.votes);
    updateCurrentSongTimeLabels();

    if (COLOR_INDICATION) {
      const upvoteIndicators = $('#current-song-upvote-indicators');
      const downvoteIndicators = $('#current-song-downvote-indicators');
      upvoteIndicators.empty();
      downvoteIndicators.empty();
      if (currentSong.requestedBy !== null) {
        const requesterVoteIndicator = $('<i/>')
            .addClass('fas')
            .css("color", currentSong.requestedBy)
            .css("font-size", "medium");
        if (currentSong.requesterVote >= 0) {
          if (currentSong.requesterVote > 0) {
            requesterVoteIndicator.addClass('fa-chevron-circle-up')
          } else {
            requesterVoteIndicator.addClass('fa-minus-circle')
          }
          requesterVoteIndicator.appendTo(upvoteIndicators)
        } else {
          requesterVoteIndicator.addClass('fa-chevron-circle-down')
              .appendTo(downvoteIndicators)
        }
      }
      for (const color of currentSong.upvotes) {
        $('<i/>')
            .addClass('fas')
            .addClass('fa-chevron-circle-up')
            .css("color", color)
            .appendTo(upvoteIndicators);
      }
      for (const color of currentSong.downvotes) {
        $('<i/>')
            .addClass('fas')
            .addClass('fa-chevron-circle-down')
            .css("color", color)
            .appendTo(downvoteIndicators);
      }
    }

    $('#progress-bar').css('transition', 'none');
    $('#progress-bar').css('width', state.progress + '%');
    // Trigger a reflow, flushing the CSS changes
    $('#progress-bar')[0].offsetHeight;

    if (state.paused) {
      showPlayButton();
    } else {
      showPauseButton();

      const duration = currentSongDuration(state.currentSong);

      const played = currentSongPlayedSeconds();
      const left = Math.max(0, duration - played);

      $('#progress-bar').css({
        'transition': 'width ' + left + 's linear',
        'width': '100%',
      });
    }
  }

  if (state.shuffle) {
    $('#set-shuffle').removeClass('icon-disabled');
    $('#set-shuffle').addClass('icon-enabled');
  } else {
    $('#set-shuffle').removeClass('icon-enabled');
    $('#set-shuffle').addClass('icon-disabled');
  }
  if (state.repeat) {
    $('#set-repeat').removeClass('icon-disabled');
    $('#set-repeat').addClass('icon-enabled');
  } else {
    $('#set-repeat').removeClass('icon-enabled');
    $('#set-repeat').addClass('icon-disabled');
  }
  if (state.autoplay) {
    $('#set-autoplay').removeClass('icon-disabled');
    $('#set-autoplay').addClass('icon-enabled');
  } else {
    $('#set-autoplay').removeClass('icon-enabled');
    $('#set-autoplay').addClass('icon-disabled');
  }

  $('#volume-slider').val(state.volume);
  if (state.volume == 0) {
    $('#volume-indicator').addClass('fa-volume-off');
    $('#volume-indicator').removeClass('fa-volume-down');
    $('#volume-indicator').removeClass('fa-volume-up');
  } else if (state.volume <= 0.5) {
    $('#volume-indicator').removeClass('fa-volume-off');
    $('#volume-indicator').addClass('fa-volume-down');
    $('#volume-indicator').removeClass('fa-volume-up');
  } else {
    $('#volume-indicator').removeClass('fa-volume-off');
    $('#volume-indicator').removeClass('fa-volume-down');
    $('#volume-indicator').addClass('fa-volume-up');
  }

  $('#total-time').text(state.totalTimeFormatted);

  /*
  <li class="list-group-item">
    <div class="queue-entry">
      <div class="download-icon queue-handle">
        <div class="download-overlay"></div>
        <svg>
          <!-- downloadSvg -->
        </svg>
      </div>
      <div class="queue-index queue-handle"><fa-sort>{{ forloop.counter }}</div>
      <div class="queue-title">{{ song.artist }} - {{ song.title }}</div>
      <div class="queue-info">
        <span class="queue-info-time">{{ song.duration-formatted }}</span>
        <span class="vote-indicators">
          <!-- 0-n <i class="fas fa-chevron-circle-up vote-indicator-0"><i/> -->
        </span>
        <span class="queue-info-controls">
          {% if voting-system %}
          <i class="fas fa-chevron-circle-up vote-up"></i>
          <i class="fas fa-chevron-circle-down vote-down"></i>
          {% else %}
          <i class="fas level-up-alt prioritize"></i>
          <i class="fas fa-trash-alt remove"></i>
          {% endif %}
        </span>
      </div>
    </div>
  </li>
  */

  // don't start a new animation when an old one is still in progress
  // the running animation will end in the (then) current state
  applyQueueChange(oldState, state);
  promotingOccurrence = null;
  finishPromotion(promotion);
  syncClosingBanner(state);

  paintVotes();
  if (!oldState || JSON.stringify(oldState.songQueue.map(s => s.occurrenceId)) !== JSON.stringify(state.songQueue.map(s => s.occurrenceId)) ||
      oldState.currentSong?.occurrenceId !== state.currentSong?.occurrenceId) {
    document.dispatchEvent(new CustomEvent('furatic:refresh-personal-votes'));
  }
  if (oldState && oldState.currentSong?.occurrenceId !== state.currentSong?.occurrenceId) refreshOpenHistory();
  syncAudioStream();
}

/** Inserts the displayname of a song into an element.
 * @param {HTMLElement} element the div the displayname should be inserted into
 * @param {Object} song the song the info is taken from
 */
function safeExternalUrl(url): string | undefined {
  return /^https?:\/\//i.test(String(url || '')) ? url : undefined;
}

function insertDisplayName(element, song) {
  const artist = displayArtist(song.artist, song.title);
  if (!artist) {
    element.text(song.title);
  } else {
    element.empty();
    element.append($('<strong/>').text(artist));
    element.append($('<span/>').addClass('queue-song-title-text').text(' – ' + song.title));
  }
}

/** Show or hide the closing-mode queue banner. */
function syncClosingBanner(newState) {
  const banner = $('#furatic-closing-banner');
  if (!banner.length) {
    return;
  }

  const visible = newState && newState.siteMode === 'closing';
  banner.toggleClass('is-visible', visible);
  banner.attr('aria-hidden', visible ? 'false' : 'true');
}

/** Create an empty queue entry.
 * @return {Object} the created queue item
 */
function createQueueItem() {
  const li = $('<li/>')
      .addClass('list-group-item')
      .attr('data-next-up-locked', 'false');

  const entryDiv = $('<div/>')
      .addClass('queue-entry')
      .addClass('queue-voting-layout')
      .appendTo(li);

  const downloadIcon = $('<div/>')
      .addClass('download-icon')
      .addClass('queue-handle')
      .appendTo(entryDiv);

  $('<div/>')
      .addClass('download-overlay')
      .appendTo(downloadIcon);

  $(downloadSvg).appendTo(downloadIcon);

  $('<div/>')
      .addClass('queue-index')
      .addClass('queue-handle')
      .appendTo(entryDiv)
      .hide();

  $('<span/>')
      .addClass('queue-info-time')
      .appendTo(entryDiv);

  $('<img/>')
      .addClass('queue-artwork')
      .attr('alt', '')
      .hide()
      .appendTo(entryDiv);

  $('<a/>')
      .addClass('queue-title')
      .attr('target', '_blank')
      .attr('rel', 'noopener noreferrer')
      .appendTo(entryDiv);

  const info = $('<div/>')
      .addClass('queue-info')
      .appendTo(entryDiv);

  $('<span/>')
      .addClass('vote-indicators')
      .appendTo(info);

  const controls = $('<span/>')
      .addClass('queue-info-controls')
      .appendTo(info);

  const voteCluster = $('<span/>')
      .addClass('queue-vote-cluster')
      .appendTo(controls);

  $('<i/>')
      .addClass('fas')
      .addClass('fa-chevron-circle-up')
      .addClass('vote-up')
      .attr('role', 'button')
      .attr('tabindex', '0')
      .attr('aria-label', 'Upvote this song')
      .appendTo(voteCluster);

  $('<span/>')
      .addClass('queue-vote-count')
      .text('0')
      .appendTo(voteCluster);

  $('<i/>')
      .addClass('fas')
      .addClass('fa-chevron-circle-down')
      .addClass('vote-down')
      .attr('role', 'button')
      .attr('tabindex', '0')
      .attr('aria-label', 'Downvote this song')
      .appendTo(voteCluster);

  if (CONTROLS_ENABLED) {
    const actionControls = $('<span/>')
        .addClass('queue-admin-controls')
        .appendTo(controls);

    $('<i/>')
        .addClass('fas')
        .addClass('fa-level-up-alt')
        .addClass('prioritize')
        .appendTo(actionControls);

    $('<i/>')
        .addClass('fas')
        .addClass('fa-trash-alt')
        .addClass('remove')
        .appendTo(actionControls);
  }

  return li;
}

/** Update a given queue entry with the information from a given song.
 * @param {Object} entry the list item to be updated
 * @param {Object} song the song containing all information
 */
function updateInformation(entry, song) {
  entry.attr('data-queue-key', String(song.id));
  entry.attr('data-occurrence-id', song.occurrenceId || String(song.id));
  entry.attr('data-priority-tier', song.priorityTier || 'normal');
  entry.attr('data-next-up-locked', song.isNextUpLocked ? 'true' : 'false');
  entry.toggleClass('ui-state-disabled', Boolean(song.isNextUpLocked));

  const row = entry.find('.queue-entry');
  row.toggleClass('queue-entry-ready', Boolean(song.internalUrl));

  const index = entry.find('.queue-index');
  index.text(song.index);

  const voteCount = entry.find('.queue-vote-count');
  if (voteCount.length) {
    voteCount.text(String(song.votes));
  }

  const downloadIcon = entry.find('.download-icon');
  if (song.internalUrl) {
    downloadIcon.hide();
    if (row.hasClass('queue-voting-layout')) {
      index.hide();
    } else {
      index.show();
    }
  } else {
    downloadIcon.show();
    index.hide();
  }

  const title = entry.find('.queue-title');
  const display = JSON.stringify([song.artist, song.title]);
  if (entry.attr('data-display') !== display) { insertDisplayName(title, song); entry.attr('data-display', display); }
  if (safeExternalUrl(song.externalUrl)) title.attr('href', song.externalUrl); else title.removeAttr('href');
  title.attr('title', 'Open song in a new tab');
  row.toggleClass('has-artwork', Boolean(song.artworkUrl));
  if (song.artworkUrl) {
    const artwork = entry.find('.queue-artwork');
    if (artwork.attr('src') !== song.artworkUrl) artwork.attr('src', song.artworkUrl);
    artwork.show();
  } else {
    entry.find('.queue-artwork').removeAttr('src').hide();
  }
  entry.toggleClass('queue-review-pending', song.reviewStatus === 'pending');
  if (song.reviewStatus === 'pending') {
    title.attr('title', "This song won't play until it has been checked by a moderator due to possibly breaking the event rules.");
    if (!entry.find('.queue-review-warning').length) {
      $('<span/>')
          .addClass('queue-review-warning')
          .attr('role', 'img')
          .attr('aria-label', 'Moderator review required')
          .text('!')
          .appendTo(entry.find('.queue-info-controls'));
    }
  } else {
    entry.find('.queue-review-warning').remove();
  }

  const time = entry.find('.queue-info-time');
  time.text(song.durationFormatted);

  if (COLOR_INDICATION) {
    const voteIndicators = entry.find('.vote-indicators');
    voteIndicators.empty();
    if (song.requestedBy !== null) {
      const requesterVoteIndicator = $('<i/>')
          .addClass('fas')
          .css("color", song.requestedBy)
          .css("font-size", "small")
          .appendTo(voteIndicators);
      if (song.requesterVote < 0) {
        requesterVoteIndicator.addClass('fa-chevron-circle-down')
      } else if (song.requesterVote == 0) {
        requesterVoteIndicator.addClass('fa-minus-circle')
      } else {
        requesterVoteIndicator.addClass('fa-chevron-circle-up')
      }
    }
    for (const color of song.upvotes) {
      $('<i/>')
          .addClass('fas')
          .addClass('fa-chevron-circle-up')
          .css("color", color)
          .appendTo(voteIndicators);
    }
    for (const color of song.downvotes) {
      $('<i/>')
          .addClass('fas')
          .addClass('fa-chevron-circle-down')
          .css("color", color)
          .appendTo(voteIndicators);
    }
  }

 const up = entry.find('.vote-up');
 const down = entry.find('.vote-down');
 
 up.removeClass('pressed');
 down.removeClass('pressed');
 
 const previousVote = getStoredVote(song.occurrenceId);
 if (previousVote == '+') {
   up.addClass('pressed');
 } else if (previousVote == '-') {
   down.addClass('pressed');
 }
}

/** Fade departing visible cards while the remaining keyed rows move into place. */
function animateExit(list: HTMLElement, row: HTMLElement, enabled: boolean) {
  const rect = row.getBoundingClientRect();
  const viewport = list.parentElement.getBoundingClientRect();
  if (enabled && !window.matchMedia('(prefers-reduced-motion: reduce)').matches &&
      typeof row.animate === 'function' && rect.height > 0 &&
      rect.bottom > viewport.top && rect.top < viewport.bottom &&
      list.querySelectorAll('.furatic-queue-exit').length < 6) {
    const ghost = row.cloneNode(true) as HTMLElement;
    ghost.removeAttribute('data-occurrence-id'); ghost.removeAttribute('data-queue-key');
    ghost.classList.remove('furatic-queue-item-enter');
    ghost.classList.add('furatic-queue-exit', 'ui-state-disabled');
    ghost.setAttribute('aria-hidden', 'true'); ghost.setAttribute('inert', '');
    Object.assign(ghost.style, {position:'absolute', top:row.offsetTop + 'px', left:row.offsetLeft + 'px',
      width:rect.width + 'px', margin:'0', pointerEvents:'none', zIndex:'2'});
    list.appendChild(ghost);
    const animation = ghost.animate([{opacity:1, transform:'translateY(0) scale(1)', filter:'blur(0)'},
      {opacity:0, transform:'translateY(-6px) scale(.985)', filter:'blur(8px)'}],
      {duration:300, easing:'cubic-bezier(.22,1,.36,1)', fill:'both'});
    animation.finished.catch(() => undefined).then(() => { animation.cancel(); ghost.remove(); });
  }
  row.remove();
}

/** Keyed FLIP: measure real row positions; retain nodes, focus and existing entry effects. */
export function reconcileList(selector: string, songs: any[], animate = true, history = false) {
  const list = document.querySelector<HTMLElement>(selector);
  if (!list || list.classList.contains('ui-sortable-active')) return;
  const existing = new Map<string, HTMLElement>();
  const before = new Map<string, DOMRect>();
  Array.from(list.children).forEach((row: HTMLElement) => {
    const key = row.dataset.occurrenceId;
    if (!key) return;
    existing.set(key, row);
    before.set(key, row.getBoundingClientRect());
    const running = rowAnimations.get(row);
    if (running) { running.cancel(); rowAnimations.delete(row); }
  });
  const keep = new Set<string>();
  const targets: HTMLElement[] = [];
  for (const song of songs) {
    const key = String(song.occurrenceId || song.id);
    keep.add(key);
    let row = existing.get(key);
    const added = !row;
    if (!row) row = createQueueItem()[0];
    updateInformation($(row), song);
    if (history) {
      row.removeAttribute('data-queue-key');
      row.removeAttribute('data-priority-tier');
      row.querySelectorAll('.queue-info-controls, .download-icon, .vote-indicators').forEach(node => node.remove());
      const duration = row.querySelector('.queue-info-time');
      duration.setAttribute('title', 'Played ' + new Date(song.endedAt).toLocaleTimeString());
    }
    // appendChild on an already ordered node would also disturb focus every poll.
    const index = targets.length;
    if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
    if (added && animate && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      row.classList.add('furatic-queue-item-enter');
      window.setTimeout(() => row.classList.remove('furatic-queue-item-enter'), 820);
    }
    targets.push(row);
  }
  existing.forEach((row, key) => { if (!keep.has(key)) animateExit(list, row, animate && key !== promotingOccurrence); });
  if (!animate || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  targets.forEach(row => {
    const previous = before.get(row.dataset.occurrenceId);
    if (!previous || typeof row.animate !== 'function') return;
    const after = row.getBoundingClientRect();
    const delta = previous.top - after.top;
    if (Math.abs(delta) < 1 || !after.height) return;
    const animation = row.animate([{transform: `translateY(${delta}px)`}, {transform: 'translateY(0)'}], {
      duration: 500, easing: 'cubic-bezier(0.22, 1, 0.36, 1)',
    });
    rowAnimations.set(row, animation);
    animation.finished.catch(() => undefined).then(() => {
      if (rowAnimations.get(row) === animation) { rowAnimations.delete(row); animation.cancel(); }
    });
  });
}

function applyQueueChange(oldState, newState) {
  const split = Boolean(document.getElementById('auto-song-queue'));
  const songs = newState.songQueue || [];
  reconcileList('#song-queue', split ? songs.filter(s => s.priorityTier !== 'extra') : songs, oldState !== null);
  if (split) {
    const extras = songs.filter(s => s.priorityTier === 'extra');
    reconcileList('#auto-song-queue', extras, oldState !== null);
    updateAutoPreview(extras);
  }
  $('#song-queue > li[data-next-up-locked="true"]').addClass('ui-state-disabled');
}

$(document).ready(() => {
  if (!["/musiq/", "/p/"].includes(window.location.pathname)) {
    return;
  }

  registerSpecificState(updateState);
  initPanels(songs => reconcileList('#history-song-queue', songs.map(song => ({...song, occurrenceId: 'history-' + song.id, durationFormatted: formatSeconds(song.duration)})), false, true));

  window.setInterval(function() {
    updateCurrentSongTimeLabels();
  }, 1000);
});
