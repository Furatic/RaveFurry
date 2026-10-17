/* Local practice only. No queue API, player, socket, or live vote code is used here. */
(() => {
  "use strict";

  const dialog = document.getElementById("furatic-tutorial");
  const toggle = document.getElementById("tutorial-toggle");
  if (!dialog || !toggle || typeof dialog.showModal !== "function") return;

  const panel = dialog.querySelector(".ft-panel");
  const viewport = document.getElementById("ft-viewport");
  const view = document.getElementById("ft-view");
  const back = document.getElementById("ft-back");
  const wordmark = document.getElementById("ft-wordmark");
  const closeButton = document.getElementById("ft-close");
  const artRoot = dialog.dataset.artRoot;
  // This intentionally replaces the old rules-v1 marker for returning visitors.
  const storageKey = "furatic-tutorial-v2";
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const ease = "cubic-bezier(.22,1,.36,1)";
  const topics = {
    adding: { title: "Add your first song", detail: "Search or paste a YouTube link.", icon: "music", steps: 3 },
    voting: { title: "Shape the queue", detail: "Votes, fairness, and the next-song lock.", icon: "vote", steps: 6 },
    multiple: { title: "Add a few favorites", detail: "See where your extra songs go.", icon: "stack", steps: 6 },
    prequeue: { title: "Meet the pre-queue", detail: "How everyone gets their next turn.", icon: "queue", steps: 4 },
  };
  // Bundled official album/video artwork: no runtime YouTube requests,
  // downloads, or dependency on the live archive being populated.
  const songs = {
    foxes: { title: "FOXES", artist: "Red Means Recording", video: "idJrErksNYk" },
    punk: { title: "PUNK TACTICS", artist: "Joey Valence & Brae", video: "OklSZmIx9-o" },
    rn: { title: "RN", artist: "Joey Valence & Brae", video: "IW9nTwZo1kY", art: "punk" },
    hooligang: { title: "HOOLIGANG", artist: "Joey Valence & Brae", video: "UPqDWj-RxV8", art: "punk" },
    jump: { title: "Double Jump", artist: "Joey Valence & Brae", video: "_nPIk3y4jrA" },
  };
  const favorites = ["foxes", "punk", "rn", "hooligang", "jump"];
  const sampleQuery = "Red Means Recording You Love Foxes";
  const artwork = key => artRoot + (songs[key].art || key) + ".jpg";
  const animations = new Set();
  const ghosts = new Set();
  let saved = { seen: false, completed: [] };
  try {
    const stored = JSON.parse(localStorage.getItem(storageKey) || "null");
    if (stored && typeof stored === "object") {
      saved.seen = stored.seen === true;
      saved.completed = Array.isArray(stored.completed) ? stored.completed.filter(key => Object.prototype.hasOwnProperty.call(topics, key)) : [];
    }
  } catch (_error) { /* Private browsing or unavailable storage still permits the guide. */ }

  let lesson = null;
  let step = 0;
  let busy = false;
  let closing = false;
  let scene = 0;
  let typingTimer = 0;
  let welcomeTimer = 0;
  let closeTimer = 0;
  let inputWasUsed = false;
  let artPreloaded = false;
  let previousFocus = null;
  let previousOverflow = null;

  const icon = name => `<svg class="ft-icon" aria-hidden="true"><use href="#ft-${name}"/></svg>`;
  const escape = value => String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const save = () => {
    try { localStorage.setItem(storageKey, JSON.stringify(saved)); } catch (_error) { /* Keep this visit working without storage. */ }
  };
  const stopTyping = () => { window.clearTimeout(typingTimer); typingTimer = 0; };
  const stopAnimations = () => {
    for (const animation of animations) animation.cancel();
    animations.clear();
    for (const ghost of ghosts) ghost.remove();
    ghosts.clear();
  };
  function animate(element, frames, duration = 340) {
    if (reducedMotion.matches || !element.animate || !dialog.open) return Promise.resolve();
    const animation = element.animate(frames, { duration, easing: ease });
    animations.add(animation);
    return animation.finished.catch(() => {}).then(() => { animations.delete(animation); });
  }
  function focusInside(selector) {
    const element = dialog.querySelector(selector);
    if (!element || element.disabled) return;
    element.focus({ preventScroll: true });
    if (viewport.contains(element)) {
      const narrator = view.querySelector(".ft-narrator");
      const bounds = viewport.getBoundingClientRect();
      const stickyHeight = narrator && getComputedStyle(narrator).position === "sticky" ? narrator.getBoundingClientRect().height + 12 : 0;
      const rect = element.getBoundingClientRect();
      if (rect.top < bounds.top + stickyHeight || rect.bottom > bounds.bottom) {
        viewport.scrollTo({ top: viewport.scrollTop + rect.top - bounds.top - stickyHeight - 6, behavior: reducedMotion.matches ? "auto" : "smooth" });
      }
    }
  }
  function morphHeight(before) {
    const after = panel.getBoundingClientRect().height;
    if (Math.abs(after - before) > 1) animate(panel, [{ height: `${before}px` }, { height: `${after}px` }]);
  }

  async function changeView(build, focusSelector) {
    const token = ++scene;
    busy = true;
    stopTyping();
    const height = panel.getBoundingClientRect().height;
    const look = getComputedStyle(view);
    const from = { opacity: look.opacity || "1", transform: look.transform || "none" };
    stopAnimations();
    panel.style.height = `${height}px`;
    view.style.pointerEvents = "none";
    await animate(view, [from, { opacity: 0, transform: "translateY(-6px)" }], 120);
    if (token !== scene || !dialog.open || closing) return;
    build();
    viewport.scrollTop = 0;
    panel.style.height = "";
    morphHeight(height);
    await animate(view, [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "translateY(0)" }], 280);
    if (token !== scene || !dialog.open || closing) return;
    view.style.pointerEvents = "";
    busy = false;
    focusInside(focusSelector);
  }

  function hubMarkup(completedTopic) {
    return `<div class="ft-intro"><span class="ft-eyebrow">Your music. Everyone’s night.</span>
      <h2 id="ft-title" tabindex="-1">Welcome to FURATIC!</h2>
      <p>Want to see how things work? Pick a quick, hands-on guide. You can leave whenever you’re ready to add music.</p></div>
      ${completedTopic ? `<div class="ft-completion" role="status">${icon("check")} ${escape(topics[completedTopic].title)} — you’ve got it!</div>` : ""}
      <div class="ft-topic-grid">${Object.entries(topics).map(([key, topic]) => `<button type="button" class="ft-topic" data-topic="${key}" data-complete="${saved.completed.includes(key)}">
        <span class="ft-topic-symbol">${icon(topic.icon)}</span><span><strong>${escape(topic.title)}</strong><small>${escape(topic.detail)}</small></span>
        ${icon(saved.completed.includes(key) ? "check" : "arrow")}<span class="ft-sr-only">${saved.completed.includes(key) ? "Completed. Replay tutorial." : "Start tutorial."}</span></button>`).join("")}</div>
      <p class="ft-hub-note"><strong>No pressure to do them all.</strong> The <strong>?</strong> button above the TV brings you back anytime.<br>
      Keep submissions kind and follow the event rules. Songs that need a moderator’s review may take a little longer to appear.</p>`;
  }
  function buildHub(completedTopic) {
    lesson = null;
    back.hidden = true;
    wordmark.hidden = false;
    view.innerHTML = hubMarkup(completedTopic);
  }
  function goHub(completedTopic) {
    changeView(() => buildHub(completedTopic), completedTopic ? `[data-topic="${completedTopic}"]` : "#ft-title");
  }

  const rowData = (key, options = {}) => ({ key, votes: 1, ...options });
  function queueState() {
    if (lesson === "adding") return { main: step >= 2 ? [rowData("foxes", { own: true })] : [], pre: [] };
    if (lesson === "multiple") return {
      main: step ? [rowData("foxes", { own: true })] : [],
      pre: favorites.slice(1, step).map(key => rowData(key, { own: true, waiting: true })),
    };
    if (lesson === "prequeue") {
      const turn = step === 0 ? 0 : step < 3 ? 1 : 2;
      return {
        main: [rowData(favorites[turn], { own: true })],
        pre: favorites.slice(turn + 1).map(key => rowData(key, { own: true, waiting: true })),
      };
    }
    const locked = rowData("punk", { locked: true });
    const rn = rowData("rn", { votes: step ? 2 : 1 });
    const jump = rowData("jump", { votes: step >= 4 ? 0 : 1 });
    const own = rowData("hooligang", { own: true });
    return { main: step === 0 ? [locked, jump, rn, own] : step < 5 ? [locked, rn, jump, own] : [locked, rn, own], pre: [] };
  }

  function rowContents(data, result = false) {
    const song = songs[data.key];
    let control;
    if (result) {
      control = `<button type="button" class="ft-button ft-add-demo" data-action="add" aria-label="Add ${escape(song.title)} to the practice queue">Add ${icon("music")}</button>`;
    } else if (data.waiting) {
      control = lesson === "prequeue" && data.key === "rn" && (step === 1 || step === 2)
        ? `<button type="button" class="ft-own-button" data-action="try-promote" ${step === 2 ? "disabled" : ""}>${step === 1 ? "Move now?" : "Waiting"}</button>` : "";
    } else if (data.locked) {
      control = `<button type="button" class="ft-lock-button" data-action="lock" aria-label="Why is PUNK TACTICS locked next?" ${step === 1 ? "" : "disabled"}>${icon("lock")}<span>Up next</span></button>`;
    } else if (data.own) {
      control = lesson === "voting" ? `<button type="button" class="ft-own-button" data-action="own" aria-label="Your song already starts with one vote" ${step === 2 ? "" : "disabled"}>1 vote<br>Your song</button>` : `<span class="ft-vote-count">1 vote</span>`;
    } else {
      control = `<div class="ft-song-votes"><button type="button" class="ft-vote-button" data-action="up" data-song="${data.key}" aria-label="Upvote ${escape(song.title)} in the practice queue" ${lesson === "voting" && step === 0 && data.key === "rn" ? "" : "disabled"}>${icon("vote")}</button>
        <span class="ft-vote-count">${data.votes}</span><button type="button" class="ft-vote-button" data-action="down" data-song="${data.key}" aria-label="Downvote ${escape(song.title)} in the practice queue" ${lesson === "voting" && step === 3 && data.key === "jump" ? "" : "disabled"}>${icon("vote")}</button></div>`;
    }
    return `<img class="ft-art" src="${escape(artwork(data.key))}" alt="" width="44" height="44" decoding="async">
      <div class="ft-song-info"><p class="ft-song-title">${escape(song.title)}</p><p class="ft-song-artist">${escape(song.artist)}</p>
      ${data.waiting ? `<span class="ft-song-tag">${icon("queue")} Waiting for your turn</span>` : data.own && !result ? '<span class="ft-song-tag">Your song</span>' : ""}</div>${control}`;
  }
  function newRow(data) {
    const row = document.createElement("article");
    row.className = "ft-song";
    row.dataset.ftKey = data.key;
    row.dataset.own = String(!!data.own);
    row.innerHTML = rowContents(data);
    return row;
  }
  function reconcileQueues() {
    const state = queueState();
    const rows = new Map([...view.querySelectorAll(".ft-song[data-ft-key], .ft-song[data-ft-pick]")].filter(row => !row.dataset.ghost).map(row => [row.dataset.ftKey || row.dataset.ftPick, row]));
    for (const name of ["main", "pre"]) {
      const list = document.getElementById(`ft-${name}-list`);
      if (!list) continue;
      list.querySelector(".ft-empty")?.remove();
      for (const data of state[name]) {
        let row = rows.get(data.key);
        if (row) {
          rows.delete(data.key);
          // Keep the art and title nodes stable while the row moves between lists.
          const updated = document.createElement("template");
          updated.innerHTML = rowContents(data);
          const oldControl = row.querySelector(".ft-song-info").nextElementSibling;
          const newControl = updated.content.querySelector(".ft-song-info").nextElementSibling;
          if (oldControl) oldControl.replaceWith(...(newControl ? [newControl] : []));
          else if (newControl) row.append(newControl);
          row.querySelector(".ft-song-tag")?.remove();
          const tag = updated.content.querySelector(".ft-song-tag");
          if (tag) row.querySelector(".ft-song-info").append(tag);
          row.dataset.ftKey = data.key;
          delete row.dataset.ftPick;
          row.dataset.own = String(!!data.own);
        } else row = newRow(data);
        list.append(row);
      }
      if (!state[name].length) {
        const empty = document.createElement("div");
        empty.className = "ft-empty";
        empty.textContent = name === "pre" ? "Your extra songs will wait here." : "Your first song will appear here.";
        list.append(empty);
      }
      const count = document.getElementById(`ft-${name}-count`);
      if (count) count.textContent = `${state[name].length} ${state[name].length === 1 ? "song" : "songs"}`;
    }
    for (const row of rows.values()) row.remove();
  }

  function lessonMarkup(key) {
    const twoBoards = key === "multiple" || key === "prequeue";
    const search = key === "adding" ? `<form class="ft-form" id="ft-search-form" autocomplete="off">
      <label class="ft-input-label" for="ft-search">YouTube link or song name<input class="ft-input" id="ft-search" type="text" placeholder="Tap here to try a search" maxlength="200" enterkeyhint="search" spellcheck="false"></label>
      <button type="submit" class="ft-button" id="ft-search-button">Find demo song ${icon("arrow")}</button></form>
      <div class="ft-input-options" aria-label="Try another input format"><button type="button" class="ft-chip" data-format="name" aria-pressed="true">Song name</button><button type="button" class="ft-chip" data-format="youtube" aria-pressed="false">YouTube link</button><button type="button" class="ft-chip" data-format="music" aria-pressed="false">YouTube Music link</button></div>` : "";
    return `<div class="ft-lesson-head"><div><span class="ft-eyebrow">A little practice</span><h2 id="ft-title" tabindex="-1">${escape(topics[key].title)}</h2></div><div class="ft-progress" id="ft-progress" role="img"></div></div>
      <aside class="ft-narrator"><img src="${escape(artRoot + "narrator.png")}" alt="" width="56" height="56"><div><span class="ft-narrator-name">Your FURATIC guide</span><p class="ft-narration" id="ft-narration" aria-live="polite" aria-atomic="true"></p></div></aside>
      <section class="ft-stage" aria-label="Interactive practice queue"><div class="ft-practice-label"><span>FURATIC / Demo</span><span>No music will play</span></div>
      ${search}<div class="ft-result" id="ft-result" hidden></div>
      <div class="ft-current">${icon("music")}<span>Playing now · <strong>Another listener’s song</strong></span></div>
      <div class="ft-boards ${twoBoards ? "ft-two-boards" : ""}">
      ${twoBoards ? '<section class="ft-board"><div class="ft-board-head"><span>Pre-queue</span><span class="ft-board-count" id="ft-pre-count"></span></div><div class="ft-song-list" id="ft-pre-list"></div></section>' : ""}
      <section class="ft-board"><div class="ft-board-head"><span>Main queue</span><span class="ft-board-count" id="ft-main-count"></span></div><div class="ft-song-list" id="ft-main-list"></div></section></div>
      ${twoBoards ? `<p class="ft-flow-hint">Pre-queue ${icon("arrow")} Main queue ${icon("arrow")} Your turn</p>` : ""}</section>
      <div class="ft-step-footer"><span class="ft-step-hint" id="ft-step-hint"></span><div id="ft-step-action"></div></div>`;
  }

  const narration = {
    adding: [
      'Tap the box and try <strong>Red Means Recording You Love Foxes</strong>. You can search a song or artist, or paste a <strong>YouTube or YouTube Music link</strong>. This practice search always uses our FOXES example.',
      'There it is: <strong>FOXES</strong> by Red Means Recording. Check the artwork and title, then tap <strong>Add</strong>. On the real site, a song may briefly download or wait for moderator review.',
      'Your first song joins the <strong>main queue</strong> with <strong>one vote</strong> already included. That’s your vote, so everyone starts equally. Ready to try it for real? Close the guide whenever you like.',
    ],
    voting: [
      'Like a song? Tap its <strong>up arrow</strong>. Try upvoting <strong>RN</strong>: one vote becomes two, moving it ahead of songs with fewer votes.',
      'RN moved up, but <strong>PUNK TACTICS is still next</strong>. Tap the little <strong>lock</strong> to see why it can’t be passed.',
      'The <strong>next song is locked</strong> so everyone gets a fair, predictable turn. More votes can’t push ahead of it. Now tap <strong>Your song</strong> beside HOOLIGANG.',
      'Your own song already has your <strong>one starting vote</strong>, so you can’t vote on it again. Next, try <strong>downvoting Double Jump</strong>. Watch where it stays.',
      'Double Jump is at <strong>zero</strong>, but keeps its place. For ordering, <strong>one vote or fewer counts equally</strong>; ties keep their queue order. Each listener gets one vote choice. Let’s see what happens when <strong>two more listeners</strong> downvote it.',
      'At <strong>−2 total votes</strong>, Double Jump leaves the queue. The same threshold <strong>skips a song if it’s currently playing</strong>. Votes above one can move songs forward, but never past the locked next song.',
    ],
    multiple: [
      'Found a few favorites? Add them <strong>one at a time</strong>. Start with FOXES; your first active song goes to the <strong>main queue</strong>. Nothing here will be submitted to the event.',
      'FOXES is in the main queue. Add <strong>PUNK TACTICS</strong> next: because you already have an active song, this extra will wait in the <strong>pre-queue</strong>.',
      'There it goes! Your extra songs wait in the pre-queue <strong>in the order you add them</strong>. Try adding <strong>RN</strong> too.',
      'You don’t have to wait on this screen between submissions. Keep adding favorites, up to <strong>five active songs total</strong>. Add <strong>HOOLIGANG</strong> next.',
      'One more spot: add <strong>Double Jump</strong>. The five-song limit includes your main-queue song, your extras, and your song if it’s currently playing.',
      'All five are lined up: <strong>one in the main queue, four in the pre-queue</strong>. When your active song finishes, your oldest extra gets your next turn. You can add another once a spot opens up.',
    ],
    prequeue: [
      'The pre-queue holds <strong>your extra songs</strong> while you already have a song queued or playing. Other listeners get their turns too. Let’s simulate <strong>your main-queue song finishing</strong> — no audio will play.',
      'FOXES finished, so your oldest extra, <strong>PUNK TACTICS</strong>, moved into the main queue. The others keep waiting. Try moving <strong>RN</strong> now.',
      'RN stays put: you already have <strong>PUNK TACTICS active</strong>. Extras can’t jump into the main queue early, and <strong>pre-queue songs can’t be voted up</strong>. Now simulate PUNK TACTICS finishing.',
      'Now it’s <strong>RN’s turn in the main queue</strong>. It can receive votes there, while your remaining extras wait. This repeats one song at a time, and the main queue’s <strong>next-song lock</strong> still applies.',
    ],
  };

  function actionButton(label, action = "advance", secondary = false) {
    return `<button type="button" class="ft-button${secondary ? " ft-button-secondary" : ""}" data-action="${action}">${label}${icon(action === "finish" ? "check" : "arrow")}</button>`;
  }
  function updateLesson() {
    const progress = document.getElementById("ft-progress");
    progress.setAttribute("aria-label", `Step ${step + 1} of ${topics[lesson].steps}`);
    progress.innerHTML = Array.from({ length: topics[lesson].steps }, (_, index) => `<span aria-hidden="true" data-done="${index <= step}" data-current="${index === step}"></span>`).join("");
    reconcileQueues();
    const result = document.getElementById("ft-result");
    result.hidden = true;
    result.replaceChildren();
    let hint = "";
    let action = "";
    if (lesson === "adding") {
      const input = document.getElementById("ft-search");
      input.readOnly = step > 0;
      document.getElementById("ft-search-button").disabled = step > 0;
      for (const chip of view.querySelectorAll("[data-format]")) chip.disabled = step > 0;
      if (step === 1) {
        result.hidden = false;
        result.innerHTML = `<article class="ft-song" data-ft-pick="foxes">${rowContents(rowData("foxes"), true)}</article>`;
      }
      hint = ["Tap the search box, then find the demo song.", "Tap Add on the search result.", "You’ve added your first practice song."][step];
      if (step === 2) action = actionButton("Back to topics", "finish");
    } else if (lesson === "voting") {
      hint = ["Tap ↑ beside RN.", "Tap the lock beside PUNK TACTICS.", "Tap Your song beside HOOLIGANG.", "Tap ↓ beside Double Jump.", "These are three different listeners’ votes.", "You’ve seen how votes and the lock work."][step];
      if (step === 4) action = actionButton("Two more listeners downvote", "community", true);
      if (step === 5) action = actionButton("Back to topics", "finish");
    } else if (lesson === "multiple") {
      if (step < 5) {
        result.hidden = false;
        result.innerHTML = `<div class="ft-practice-label"><span>Your next pick</span><span>${step + 1} of 5</span></div><article class="ft-song" data-ft-pick="${favorites[step]}">${rowContents(rowData(favorites[step]), true)}</article>`;
      }
      hint = step < 5 ? `${step} of 5 active songs · Tap Add on your next pick.` : "5 of 5 active songs · Your extras wait their turn.";
      if (step === 5) action = actionButton("Back to topics", "finish");
    } else {
      hint = ["Watch the oldest extra move into the main queue.", "Tap Move now? beside RN in the pre-queue.", "Your extras wait while you have an active song.", "Your oldest extra gets your next turn."][step];
      if (step === 0) action = actionButton("Finish your demo song");
      if (step === 2) action = actionButton("Finish PUNK TACTICS");
      if (step === 3) action = actionButton("Back to topics", "finish");
    }
    document.getElementById("ft-narration").innerHTML = narration[lesson][step];
    document.getElementById("ft-step-hint").textContent = hint;
    document.getElementById("ft-step-action").innerHTML = action;
    for (const element of view.querySelectorAll(".ft-spotlight")) element.classList.remove("ft-spotlight");
    const target = targetSelector();
    if (target) view.querySelector(target)?.classList.add("ft-spotlight");
    if (step === topics[lesson].steps - 1) view.querySelector(".ft-stage").classList.add("ft-celebrate");
  }
  function targetSelector() {
    if (lesson === "adding") return ["#ft-search", '[data-action="add"]', '[data-action="finish"]'][step];
    if (lesson === "voting") return ['[data-action="up"][data-song="rn"]', '[data-action="lock"]', '[data-action="own"]', '[data-action="down"][data-song="jump"]', '[data-action="community"]', '[data-action="finish"]'][step];
    if (lesson === "multiple") return step < 5 ? '[data-action="add"]' : '[data-action="finish"]';
    return ['[data-action="advance"]', '[data-action="try-promote"]', '[data-action="advance"]', '[data-action="finish"]'][step];
  }
  function startLesson(key) {
    if (!Object.prototype.hasOwnProperty.call(topics, key)) return;
    changeView(() => {
      lesson = key;
      step = 0;
      inputWasUsed = false;
      back.hidden = false;
      wordmark.hidden = true;
      view.innerHTML = lessonMarkup(key);
      updateLesson();
    }, "#ft-title");
  }

  function advance() {
    if (busy || closing || !lesson || step >= topics[lesson].steps - 1) return;
    stopTyping();
    const stage = view.querySelector(".ft-stage");
    const oldStage = stage.getBoundingClientRect();
    const before = new Map([...stage.querySelectorAll(".ft-song[data-ft-key], .ft-song[data-ft-pick]")].filter(row => !row.dataset.ghost).map(row => [row.dataset.ftKey || row.dataset.ftPick, { row, rect: row.getBoundingClientRect() }]));
    const height = panel.getBoundingClientRect().height;
    stopAnimations();
    step += 1;
    updateLesson();
    const remaining = new Map([...stage.querySelectorAll(".ft-song[data-ft-key]")].map(row => [row.dataset.ftKey, row]));
    const newStage = stage.getBoundingClientRect();
    for (const [key, { row, rect }] of before) {
      if (remaining.has(key)) continue;
      const ghost = row.cloneNode(true);
      ghost.dataset.ghost = "true";
      ghost.removeAttribute("data-ft-key");
      ghost.setAttribute("aria-hidden", "true");
      ghost.inert = true;
      for (const button of ghost.querySelectorAll("button")) button.tabIndex = -1;
      Object.assign(ghost.style, { position: "absolute", left: `${rect.left - oldStage.left}px`, top: `${rect.top - oldStage.top}px`, width: `${rect.width}px`, height: `${rect.height}px`, pointerEvents: "none", zIndex: "2" });
      stage.append(ghost);
      ghosts.add(ghost);
      animate(ghost, [{ opacity: 1, transform: "scale(1)" }, { opacity: 0, transform: "scale(.95)" }], 230).then(() => { ghost.remove(); ghosts.delete(ghost); });
    }
    for (const [key, row] of remaining) {
      const old = before.get(key);
      if (old) {
        const after = row.getBoundingClientRect();
        animate(row, [
          { transform: `translate(${old.rect.left - oldStage.left - after.left + newStage.left}px, ${old.rect.top - oldStage.top - after.top + newStage.top}px)`, width: `${old.rect.width}px`, height: `${old.rect.height}px` },
          { transform: "translate(0,0)", width: `${after.width}px`, height: `${after.height}px` },
        ], 460);
      } else animate(row, [{ opacity: 0, transform: "translateY(10px)" }, { opacity: 1, transform: "translateY(0)" }], 400);
    }
    const nextPick = stage.querySelector("[data-ft-pick]");
    if (nextPick) animate(nextPick, [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "translateY(0)" }], 360);
    animate(document.getElementById("ft-narration"), [{ opacity: 0, transform: "translateY(4px)" }, { opacity: 1, transform: "translateY(0)" }], 280);
    animate(view.querySelector(".ft-step-footer"), [{ opacity: 0 }, { opacity: 1 }], 280);
    morphHeight(height);
    focusInside(targetSelector());
  }
  function completeLesson() {
    if (!lesson || step !== topics[lesson].steps - 1) return;
    const completedTopic = lesson;
    if (!saved.completed.includes(completedTopic)) saved.completed.push(completedTopic);
    save();
    goHub(completedTopic);
  }

  function typeExample(input) {
    if (inputWasUsed || input.value || step !== 0 || lesson !== "adding") return;
    inputWasUsed = true;
    if (reducedMotion.matches) { input.value = sampleQuery; return; }
    const token = scene;
    let index = 0;
    const type = () => {
      if (token !== scene || !dialog.open || closing || lesson !== "adding" || step !== 0) return;
      input.value = sampleQuery.slice(0, ++index);
      if (index < sampleQuery.length) typingTimer = window.setTimeout(type, 34);
      else typingTimer = 0;
    };
    type();
  }

  function openGuide() {
    if (dialog.open) return;
    window.clearTimeout(welcomeTimer);
    closing = false;
    busy = false;
    ++scene;
    previousFocus = document.activeElement;
    buildHub();
    view.style.pointerEvents = "";
    const rootStyle = document.documentElement.style;
    previousOverflow = { value: rootStyle.getPropertyValue("overflow"), priority: rootStyle.getPropertyPriority("overflow") };
    rootStyle.setProperty("overflow", "hidden");
    dialog.showModal();
    syncViewport();
    toggle.setAttribute("aria-expanded", "true");
    saved.seen = true;
    save();
    animate(panel, [{ opacity: 0, transform: "translateY(20px) scale(.975)" }, { opacity: 1, transform: "translateY(0) scale(1)" }], 440);
    animate(dialog, [
      { backgroundColor: "rgba(3,6,16,0)", backdropFilter: "blur(0px)", webkitBackdropFilter: "blur(0px)" },
      { backgroundColor: "rgba(3,6,16,.94)", backdropFilter: "blur(18px)", webkitBackdropFilter: "blur(18px)" },
    ], 360);
    closeButton.focus({ preventScroll: true });
    if (!artPreloaded) {
      artPreloaded = true;
      for (const src of new Set(favorites.map(artwork))) { const image = new Image(); image.src = src; }
    }
  }
  function closeGuide(addMusic = false) {
    if (!dialog.open || closing) return;
    closing = true;
    busy = true;
    ++scene;
    stopTyping();
    // Start at the visible position even if an opening/morph animation is interrupted.
    const panelLook = getComputedStyle(panel);
    const height = panel.getBoundingClientRect().height;
    const panelFrom = { opacity: panelLook.opacity || "1", transform: panelLook.transform || "none", height: `${height}px` };
    const overlayLook = getComputedStyle(dialog);
    const blur = overlayLook.backdropFilter || overlayLook.webkitBackdropFilter || "blur(18px)";
    const overlayFrom = { backgroundColor: overlayLook.backgroundColor, backdropFilter: blur, webkitBackdropFilter: blur };
    stopAnimations();
    const finish = () => {
      if (!closing) return;
      window.clearTimeout(closeTimer);
      stopAnimations();
      dialog.close();
      panel.style.height = "";
      const rootStyle = document.documentElement.style;
      if (previousOverflow.value) rootStyle.setProperty("overflow", previousOverflow.value, previousOverflow.priority);
      else rootStyle.removeProperty("overflow");
      previousOverflow = null;
      closing = false;
      busy = false;
      view.style.pointerEvents = "";
      toggle.setAttribute("aria-expanded", "false");
      const restore = previousFocus instanceof HTMLElement && previousFocus.isConnected && previousFocus !== document.body ? previousFocus : toggle;
      restore.focus({ preventScroll: true });
      if (addMusic) {
        // Focus only: never submit a form or call into the live queue code.
        try { document.getElementById("queue-player-frame")?.contentDocument?.getElementById("music-input")?.focus(); } catch (_error) { /* The user can still tap the live input. */ }
      }
    };
    closeTimer = window.setTimeout(finish, 420);
    animate(dialog, [
      overlayFrom,
      { backgroundColor: "rgba(3,6,16,0)", backdropFilter: "blur(0px)", webkitBackdropFilter: "blur(0px)" },
    ], 240);
    animate(panel, [panelFrom, { opacity: 0, transform: "translateY(12px) scale(.98)", height: `${height}px` }], 240).then(finish);
  }

  toggle.addEventListener("click", openGuide);
  closeButton.addEventListener("click", () => closeGuide());
  document.getElementById("ft-leave").addEventListener("click", () => closeGuide(true));
  back.addEventListener("click", () => { if (!closing) goHub(); });
  dialog.addEventListener("cancel", event => { event.preventDefault(); closeGuide(); });
  // Native modal dialogs keep keyboard focus and pointer events out of all live iframes.
  dialog.addEventListener("keydown", event => {
    if (event.key !== "Tab") return;
    const controls = [...dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), [tabindex="0"]')].filter(element => !element.closest("[hidden]") && element.getClientRects().length);
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || !controls.includes(document.activeElement))) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  view.addEventListener("click", event => {
    if (busy || closing) return;
    const topic = event.target.closest("[data-topic]");
    if (topic) { startLesson(topic.dataset.topic); return; }
    const format = event.target.closest("[data-format]");
    if (format && lesson === "adding" && step === 0) {
      stopTyping();
      inputWasUsed = true;
      const input = document.getElementById("ft-search");
      input.value = format.dataset.format === "name" ? sampleQuery : `https://${format.dataset.format === "music" ? "music." : "www."}youtube.com/watch?v=${songs.foxes.video}`;
      for (const chip of view.querySelectorAll("[data-format]")) chip.setAttribute("aria-pressed", String(chip === format));
      return;
    }
    const button = event.target.closest("[data-action]");
    if (!button || button.disabled) return;
    const action = button.dataset.action;
    if (action === "finish") { completeLesson(); return; }
    const allowed = (lesson === "adding" && step === 1 && action === "add") ||
      (lesson === "multiple" && step < 5 && action === "add") ||
      (lesson === "voting" && ((step === 0 && action === "up" && button.dataset.song === "rn") || (step === 1 && action === "lock") || (step === 2 && action === "own") || (step === 3 && action === "down" && button.dataset.song === "jump") || (step === 4 && action === "community"))) ||
      (lesson === "prequeue" && ((step === 0 || step === 2) && action === "advance" || step === 1 && action === "try-promote"));
    if (allowed) advance();
  });
  view.addEventListener("submit", event => {
    event.preventDefault();
    if (busy || closing || lesson !== "adding" || step !== 0) return;
    const input = document.getElementById("ft-search");
    if (!input.value.trim()) input.value = sampleQuery;
    advance();
  });
  view.addEventListener("focusin", event => { if (event.target.id === "ft-search") typeExample(event.target); });
  view.addEventListener("beforeinput", event => { if (event.target.id === "ft-search") { stopTyping(); inputWasUsed = true; } });
  view.addEventListener("input", event => { if (event.target.id === "ft-search") { stopTyping(); inputWasUsed = true; } });
  // Finish finite animations on interruptions rather than accumulating callbacks.
  function syncViewport() {
    if (!dialog.open) return;
    const visible = window.visualViewport;
    dialog.style.height = visible ? `${visible.height}px` : "100%";
    dialog.style.top = visible ? `${visible.offsetTop}px` : "0px";
    dialog.classList.toggle("ft-compact", (visible ? visible.height : window.innerHeight) < 600);
  }
  window.addEventListener("resize", () => { if (dialog.open) { stopAnimations(); syncViewport(); } });
  window.visualViewport?.addEventListener("resize", syncViewport);
  window.visualViewport?.addEventListener("scroll", syncViewport);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopTyping();
      for (const animation of animations) { try { animation.finish(); } catch (_error) { animation.cancel(); } }
    } else scheduleWelcome();
  });
  function scheduleWelcome() {
    if (saved.seen || dialog.open || document.hidden || !document.body.classList.contains("is-logo-docked")) return;
    window.clearTimeout(welcomeTimer);
    // Allow the existing dock/content reveal to settle after the logo lands.
    welcomeTimer = window.setTimeout(() => {
      const active = document.activeElement;
      const alreadyAdding = active && active.matches("iframe, input, textarea, select, [contenteditable='true']");
      if (!saved.seen && !document.hidden && !dialog.open && !alreadyAdding) openGuide();
    }, 800);
  }
  window.addEventListener("furatic:intro-complete", scheduleWelcome, { once: true });
  scheduleWelcome();
})();
