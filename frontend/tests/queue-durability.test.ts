import {DrawerController} from '@src/musiq/panels';
import {reconcileList} from '@src/musiq/update';
import {acceptVote, getVoteState, paintVotes, setVotePending} from '@src/musiq/vote-state';

const tick = () => Promise.resolve();
const song = (id, tier = 'normal') => ({id, occurrenceId: 'song-' + id, priorityTier: tier, title: 'Song ' + id, artist: 'Artist', durationFormatted: '3:00', votes: 1, internalUrl: 'ready', externalUrl: 'https://example.com/' + id, artworkUrl: '', reviewStatus: 'clear'});

beforeEach(() => { document.body.innerHTML = '<ul id="song-queue"></ul><ul id="auto-song-queue"></ul>'; });

test('drawer switch waits for complete close before opening history', async () => {
  const calls = [];
  let close: () => void;
  const controller = new DrawerController((panel, opening) => {
    calls.push([panel, opening]);
    return !opening ? new Promise<void>(resolve => { close = resolve; }) : Promise.resolve();
  });
  await controller.show('auto');
  const pending = controller.show('history');
  await tick();
  expect(calls).toEqual([['auto', true], ['auto', false]]);
  close(); await pending;
  expect(calls).toEqual([['auto', true], ['auto', false], ['history', true]]);
});

test('rapid drawer intent changes coalesce without opening stale history', async () => {
  const calls = []; let finish: () => void;
  const controller = new DrawerController((panel, opening) => {
    calls.push([panel, opening]);
    return !opening ? new Promise<void>(resolve => { finish = resolve; }) : Promise.resolve();
  });
  await controller.show('auto');
  const pending = controller.show('history'); await tick();
  controller.show(null); finish(); await pending;
  expect(controller.active).toBe(null);
  expect(calls).toEqual([['auto', true], ['auto', false]]);
});

test('metadata polling retains the same row and title nodes', () => {
  reconcileList('#song-queue', [song(1)], false);
  const row = document.querySelector('#song-queue>li');
  const title = row.querySelector('.queue-title');
  reconcileList('#song-queue', [{...song(1), votes: 3}], false);
  expect(document.querySelector('#song-queue>li')).toBe(row);
  expect(row.querySelector('.queue-title')).toBe(title);
  expect(row.querySelector('.queue-vote-count').textContent).toBe('3');
});

test('reordering and an immediate newer update never lose incoming rows', () => {
  reconcileList('#song-queue', [song(1),song(2)], false);
  const first = document.querySelector('[data-occurrence-id="song-1"]');
  reconcileList('#song-queue', [song(2),song(1),song(3)], true);
  reconcileList('#song-queue', [song(3),song(2)], true);
  expect(Array.from(document.querySelector('#song-queue').children).map((n: HTMLElement)=>n.dataset.occurrenceId)).toEqual(['song-3','song-2']);
  expect(first.isConnected).toBe(false);
});

test('external titles do not accept script URLs', () => {
  reconcileList('#song-queue', [{...song(1), externalUrl:'javascript:alert(1)'}], false);
  expect(document.querySelector('.queue-title').hasAttribute('href')).toBe(false);
});

test('an auto queue row cannot regain voting through a private state refresh', () => {
  reconcileList('#auto-song-queue', [song(7,'extra')], false);
  acceptVote({occurrenceId:'song-7',choice:0,voteRevision:0}); paintVotes();
  expect(document.querySelector('#auto-song-queue .vote-up').getAttribute('aria-disabled')).toBe('true');
});

test('pending votes ignore a stale private poll and accept the server acknowledgement', () => {
  acceptVote({occurrenceId:'pending-song',choice:1,voteRevision:1},true);
  setVotePending('pending-song',true);
  acceptVote({occurrenceId:'pending-song',choice:0,voteRevision:0});
  expect(getVoteState('pending-song').choice).toBe(1);
  acceptVote({occurrenceId:'pending-song',choice:-1,voteRevision:2},true);
  setVotePending('pending-song',false);
  expect(getVoteState('pending-song').choice).toBe(-1);
});

test('the same persistent occurrence retains its vote after moving into the current card', () => {
  reconcileList('#song-queue', [song(9)], false);
  acceptVote({occurrenceId:'song-9',choice:1,voteRevision:2});
  document.body.innerHTML = '<li id="current-song" data-occurrence-id="song-9"><i class="vote-up"></i><i class="vote-down"></i></li>';
  paintVotes();
  expect(document.querySelector('.vote-up').classList.contains('pressed')).toBe(true);
});
