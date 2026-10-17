import * as votingButtons from '@src/musiq/buttons-voting';
import {acceptVote} from '@src/musiq/vote-state';
import * as base from '@src/base';

test('real activations send desired votes, suppress duplicate taps and retain the voting rate limit', async () => {
  const warning = jest.spyOn(base, 'warningToastWithBar').mockImplementation(() => {});
  jest.spyOn(base, 'getState').mockImplementation(() => {});
  jest.spyOn(base, 'errorToast').mockImplementation(() => {});
  (global as any).urls = {musiq: {vote:'/vote'}};
  document.body.innerHTML = '<ul id="song-queue"></ul><div id="vote-timeout-bar"></div>';
  const post = jest.fn(async (_url, options) => {
    const form = new URLSearchParams(options.body);
    return {ok:true, json: async () => ({occurrenceId:form.get('occurrence'),choice:Number(form.get('choice')),voteRevision:Number(form.get('revision'))+1,votes:2})};
  });
  (global as any).fetch = post;
  const row = (id: number) => {
    const occurrence = 'vote-' + id;
    const li = document.createElement('li');
    li.dataset.queueKey=String(id); li.dataset.occurrenceId=occurrence; li.dataset.priorityTier='normal';
    li.innerHTML='<div class="queue-entry"><i class="vote-up"></i><span class="queue-vote-count">1</span><i class="vote-down"></i></div>';
    document.getElementById('song-queue').appendChild(li);
    acceptVote({occurrenceId:occurrence,choice:0,voteRevision:0},true);
    return li;
  };
  const activate = (button: Element) => button.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
  const settle = async () => { for(let i=0;i<8;i++) await Promise.resolve(); };
  votingButtons.onReady();
  const first=row(1);
  activate(first.querySelector('.vote-up')); activate(first.querySelector('.vote-up'));
  await settle(); expect(post).toHaveBeenCalledTimes(1);
  activate(first.querySelector('.vote-down')); await settle();
  const flip = new URLSearchParams(post.mock.calls[1][1].body);
  expect(flip.get('choice')).toBe('-1'); expect(flip.get('revision')).toBe('1');
  expect(flip.get('occurrence')).toBe('vote-1'); expect(flip.get('mutation')).toMatch(/^[A-Za-z0-9_-]{16,64}$/);
  for(let id=2;id<=9;id++) { activate(row(id).querySelector('.vote-up')); await settle(); }
  expect(post).toHaveBeenCalledTimes(10);
  activate(row(10).querySelector('.vote-up'));
  expect(post).toHaveBeenCalledTimes(10);
  expect(warning).toHaveBeenCalledWith("You're doing that too often");
  jest.restoreAllMocks();
});
