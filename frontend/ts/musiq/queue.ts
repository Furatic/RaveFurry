import {state} from './update';
import {keyOfElement} from './buttons';
import {getState, errorToast} from '../base';
import 'jquery-ui/ui/widgets/sortable';
import 'jquery-ui-touch-punch';

/** Allows reordering of the queue when not voting. */
export function onReady() {
  // enable drag and drop for the song queue
  if (INTERACTIVITY !== INTERACTIVITIES.fullControl) {
    return;
  }

  $('#song-queue, #auto-song-queue').sortable({
    handle: '.queue-handle',
    items: '> li:not(.furatic-queue-exit)',
    start: function(e, ui) { ui.item.parent().addClass('ui-sortable-active'); },
    stop: function(e, ui) {
      ui.item.parent().removeClass('ui-sortable-active');
      const key = keyOfElement(ui.item);
      const prev = ui.item.prev();
      let prevKey = null;
      if (prev.length) {
        prevKey = keyOfElement(prev);
      }
      const next = ui.item.next();
      let nextKey = null;
      if (next.length) {
        nextKey = keyOfElement(next);
      }

      // change our state so the animation does not trigger
      const newIndex = ui.item.index();
      const oldIndex = state.songQueue.findIndex(song => song.id === key);
      const tier = ui.item.attr('data-priority-tier');
      const offset = tier === 'extra' ? state.songQueue.filter(song => song.priorityTier !== 'extra').length : 0;
      const targetIndex = offset + newIndex;
      if (prevKey === null && offset > 0) prevKey = state.songQueue[offset - 1].id;
      if (nextKey === null && tier !== 'extra') {
        const extra = state.songQueue.find(song => song.priorityTier === 'extra');
        if (extra) nextKey = extra.id;
      }
      if (targetIndex == oldIndex) {
        return;
      }

      // remove the entry from its old position
      const queueEntry = state.songQueue.splice(oldIndex, 1);
      // and insert it in the new position
      state.songQueue.splice(targetIndex, 0, queueEntry[0]);
      // update the indices of all items
      $('#song-queue>li, #auto-song-queue>li').each(function(index, el) {
        $(el).find('.queue-index').text(index + 1);
      });

      $.post(urls['musiq']['reorder'], {
        prev: prevKey,
        element: key,
        next: nextKey,
      }).fail(function() { errorToast('The queue changed while reordering. Its current order has been restored.'); }).always(getState);
    },
  });
}

$(document).ready(() => {
  if (!["/musiq/", "/p/"].includes(window.location.pathname)) {
    return;
  }
  onReady();
});
