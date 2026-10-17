import {keyOfElement} from './buttons';
import {state} from './update';
import {warningToastWithBar, errorToast} from '../base';
import {getVoteState, acceptVote, setVotePending, paintVotes} from './vote-state';
import {getState} from '../base';

/** Adds handlers to buttons that are visible when voting is enabled. */
export function onReady() {
  // Use a token bucket implementation to allow 10 Votes per minute.
  const maxTokens = 10;
  let currentTokens = maxTokens;
  const bucketLifetime = 30000; // half a minute
  let currentBucket = $.now();

  let lastActivationAt = 0;
  let lastActivationSignature = '';

  function canVote() {
    const now = $.now();
    const timePassed = now - currentBucket;

    if (timePassed > bucketLifetime) {
      currentBucket = now;
      currentTokens = maxTokens - 1;
      return true;
    }

    if (currentTokens > 0) {
      currentTokens--;
      return true;
    }

    const ratio = (bucketLifetime - timePassed) / bucketLifetime;
    warningToastWithBar('You\'re doing that too often');
    $('#vote-timeout-bar').css('transition', 'none');
    $('#vote-timeout-bar').css('width', ratio * 100 + '%');
    $('#vote-timeout-bar')[0].offsetHeight;
    $('#vote-timeout-bar').css({
      'transition': 'width ' + ratio * bucketLifetime / 1000 + 's linear',
      'width': '0%',
    });
    return false;
  }

  function triggerVoteAnimation(buttonElement) {
    if (
      !buttonElement ||
      !(buttonElement instanceof HTMLElement) ||
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    ) {
      return;
    }

    buttonElement.classList.remove('furatic-vote-bump');
    void buttonElement.offsetWidth;
    buttonElement.classList.add('furatic-vote-bump');

    window.setTimeout(function() {
      buttonElement.classList.remove('furatic-vote-bump');
    }, 560);
  }

  function findVoteButton(event) {
    const target = event && event.target instanceof Element ? event.target : null;
    if (!target) {
      return null;
    }

    return target.closest('.vote-up, .vote-down');
  }

  function activationSignature(buttonElement) {
    const button = $(buttonElement).closest('.vote-up, .vote-down');
    const keyedElement = button.closest('[data-queue-key]');
    const key = keyedElement.attr('data-queue-key') || '';
    const id = button.attr('id') || '';
    const direction = button.hasClass('vote-up') ? 'up' : 'down';

    return direction + ':' + key + ':' + id;
  }

  function markActivationHandled(event, buttonElement) {
    const originalEvent = event;

    if (
      event.type === 'pointerup' &&
      event instanceof PointerEvent &&
      event.pointerType === 'mouse' &&
      event.button !== 0
    ) {
      return false;
    }

    const now = Date.now();
    const signature = activationSignature(buttonElement);

    if (signature === lastActivationSignature && now - lastActivationAt < 650) {
      if (originalEvent.cancelable) {
        originalEvent.preventDefault();
      }
      originalEvent.stopImmediatePropagation();
      return false;
    }

    lastActivationSignature = signature;
    lastActivationAt = now;

    if (originalEvent.cancelable) {
      originalEvent.preventDefault();
    }
    originalEvent.stopImmediatePropagation();

    return true;
  }

  function resolveVoteKey(button) {
    if (button.closest('#current-song-card').length > 0) {
      if (state == null || state.currentSong == null) {
        return -1;
      }

      return state.currentSong.queueKey;
    }

    return keyOfElement(button);
  }

  async function handleVotePress(buttonElement) {
    const button = $(buttonElement);
    const row = button.closest('[data-occurrence-id]');
    const occurrence = row.attr('data-occurrence-id');
    const previous = getVoteState(occurrence);
    if (button.attr('data-furatic-own-vote-blocked') === 'true' ||
        row.attr('data-priority-tier') === 'extra' ||
        button.attr('data-vote-pending') === 'true') return false;
    if (!previous) {
      document.dispatchEvent(new CustomEvent('furatic:refresh-personal-votes'));
      return false;
    }
    const key = resolveVoteKey(button);
    if (key === -1 || !canVote()) return false;
    const direction = button.hasClass('vote-up') ? 1 : -1;
    const desired = previous.choice === direction ? 0 : direction;
    const voteCount = row.find('.queue-vote-count, #current-song-votes');
    const originalCount = Number(voteCount.text()) || 0;
    const mutation = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() :
      Date.now().toString(36) + '-' + Math.random().toString(36).slice(2) + '-' + Math.random().toString(36).slice(2);
    acceptVote({...previous, choice: desired}, true);
    setVotePending(occurrence, true);
    voteCount.text(String(originalCount + desired - previous.choice));
    triggerVoteAnimation(buttonElement);
    const form = new URLSearchParams();
    form.set('key', String(key));
    form.set('occurrence', occurrence);
    form.set('choice', String(desired));
    form.set('revision', String(previous.voteRevision));
    form.set('mutation', mutation);
    form.set('csrfmiddlewaretoken', CSRF_TOKEN);
    const abort = new AbortController();
    const timeout = window.setTimeout(() => abort.abort(), 9000);
    try {
      const response = await fetch(urls['musiq']['vote'], {
        method: 'POST', credentials: 'same-origin', signal: abort.signal,
        headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-CSRFToken': CSRF_TOKEN},
        body: form.toString(),
      });
      const result = await response.json();
      if (result.occurrenceId && typeof result.choice === 'number') {
        acceptVote(result, true);
        if (typeof result.votes === 'number') voteCount.text(String(result.votes));
      }
      if (!response.ok) {
        if (!result.occurrenceId) acceptVote(previous, true);
        throw new Error(result.message || 'Could not register vote');
      }
    } catch (error) {
      // A timed-out POST may have committed. Re-read; never retry a mutation blindly.
      errorToast(error && error.message && error.name !== 'AbortError' ? error.message : 'Checking your vote after a connection interruption.');
    } finally {
      window.clearTimeout(timeout);
      setVotePending(occurrence, false);
      document.dispatchEvent(new CustomEvent('furatic:refresh-personal-votes'));
      getState();
      paintVotes();
    }
    return true;
  }

  function handleVoteActivation(event) {
    const buttonElement = findVoteButton(event);
    if (!buttonElement) {
      return;
    }

    if (buttonElement.getAttribute('data-furatic-own-vote-blocked') === 'true') {
      return;
    }

    if (!markActivationHandled(event, buttonElement)) {
      return;
    }

    handleVotePress(buttonElement);
  }

  document.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') handleVoteActivation(event);
  }, {capture: true});

  document.addEventListener('pointerup', handleVoteActivation, {
    capture: true,
    passive: false,
  });

  document.addEventListener('touchend', handleVoteActivation, {
    capture: true,
    passive: false,
  });

  document.addEventListener('click', handleVoteActivation, {
    capture: true,
    passive: false,
  });
}

$(document).ready(() => {
  if (!["/musiq/", "/p/"].includes(window.location.pathname)) {
    return;
  }

  onReady();
});
