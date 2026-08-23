(function () {
  const SPEED_RANGES = {
    instant: [0, 0],
    fast: [600, 1800],
    realistic: [1000, 4000],
    slow: [3000, 8000],
  };
  const NEED_TARGETS = { QB: 3, RB: 6, WR: 7, TE: 3, K: 1, DEF: 1 };
  const ROSTER_POSITION_ORDER = ['QB', 'RB', 'WR', 'TE', 'K', 'DEF'];
  const STARTER_REQUIREMENTS = { QB: 1, RB: 2, WR: 2, TE: 1, K: 1, DEF: 1 };
  const FLEX_ELIGIBLE = ['RB', 'WR'];
  const REVEAL_PAUSE_MS = 260;
  const ROUND_TRANSITION_MS = 900;

  let state = JSON.parse(document.getElementById('draft-init').textContent);
  let pollInterval = null;
  let filterPos = '';
  let filterQuery = '';
  let lastRenderedRound = state.on_the_clock ? state.on_the_clock.round : null;
  let busy = false;
  let activeSubtab = 'players';

  const el = {
    roundHeader: document.getElementById('draft-round-header'),
    speedPill: document.getElementById('draft-speed-pill'),
    ticker: document.getElementById('ticker'),
    clockContent: document.getElementById('clock-content'),
    playersBody: document.getElementById('players-body'),
    teamsPanel: document.getElementById('teams-panel'),
    posTabs: document.getElementById('pos-tabs'),
    search: document.getElementById('player-search'),
    roundTransition: document.getElementById('round-transition'),
    completionOverlay: document.getElementById('completion-overlay'),
    gradesContent: document.getElementById('grades-content'),
    subtabs: document.getElementById('draft-subtabs'),
    playersPanel: document.getElementById('players-panel'),
    queuePanel: document.getElementById('queue-panel'),
    lineupPanel: document.getElementById('lineup-panel'),
    queueBody: document.getElementById('queue-body'),
    queueCount: document.getElementById('queue-count'),
    lineupBody: document.getElementById('lineup-body'),
  };

  // ---- draft queue (client-side only -- personal to this browser) --------

  const queueKey = `rivyl_draft_queue_${state.league.id}`;
  let queue = [];
  try {
    queue = JSON.parse(localStorage.getItem(queueKey) || '[]');
  } catch (e) {
    queue = [];
  }

  function saveQueue() {
    try {
      localStorage.setItem(queueKey, JSON.stringify(queue));
    } catch (e) { /* ignore (e.g. private browsing storage limits) */ }
  }

  function toggleQueue(playerId) {
    const idx = queue.indexOf(playerId);
    if (idx === -1) queue.push(playerId);
    else queue.splice(idx, 1);
    saveQueue();
    renderPlayers();
    renderQueue();
  }

  function moveInQueue(playerId, delta) {
    const idx = queue.indexOf(playerId);
    const newIdx = idx + delta;
    if (idx === -1 || newIdx < 0 || newIdx >= queue.length) return;
    [queue[idx], queue[newIdx]] = [queue[newIdx], queue[idx]];
    saveQueue();
    renderQueue();
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function speedDelay(speed) {
    const [a, b] = SPEED_RANGES[speed] || SPEED_RANGES.fast;
    return a + Math.random() * (b - a);
  }

  async function fetchJSON(url, opts) {
    const res = await fetch(url, opts);
    return res.json();
  }

  function pickCode(round, pickInRound) {
    return `${round}.${String(pickInRound).padStart(2, '0')}`;
  }

  function ownerIcon(ownerType) {
    return ownerType === 'human' ? '👤' : '🤖';
  }

  function displayName(pick) {
    if (pick.owner_type === 'human') return pick.owner_name || pick.team_name;
    return pick.ai_personality || pick.team_name;
  }

  // ---- rendering ---------------------------------------------------------

  function renderHeader() {
    const { league } = state;
    if (league.draft_status === 'complete') {
      el.roundHeader.textContent = `Draft complete · ${league.rounds} rounds`;
    } else {
      el.roundHeader.textContent = `Pick ${league.current_pick_index} of ${league.total_picks}`;
    }
    el.speedPill.textContent = {
      instant: 'Instant', fast: 'Fast', realistic: 'Realistic', slow: 'Slow',
    }[league.ai_speed] || 'Fast';
  }

  function renderTicker() {
    const made = state.picks.filter((p) => p.player_id).slice().reverse();
    const onClockPick = state.on_the_clock;

    let html = '';
    if (state.league.draft_status === 'in_progress' && onClockPick) {
      html += `
        <div class="tick-item tick-pending">
          <div class="tick-code">${pickCode(onClockPick.round, onClockPick.pick_in_round)}</div>
          <div class="tick-body">
            <div class="tick-team">⏳ ${displayName(onClockPick)}</div>
            <div class="tick-meta">ON THE CLOCK</div>
          </div>
        </div>`;
    }

    let currentRound = null;
    made.forEach((pick, i) => {
      if (pick.round !== currentRound) {
        currentRound = pick.round;
        html += `<div class="tick-round-label">ROUND ${currentRound}</div>`;
      }
      const timeoutTag = pick.is_autopick ? '<span class="tick-auto">⏰ auto</span>' : '';
      html += `
        <div class="tick-item ${i === 0 ? 'tick-newest' : ''}">
          <div class="tick-code">${pickCode(pick.round, pick.pick_in_round)}</div>
          <div class="tick-body">
            <div class="tick-team">${ownerIcon(pick.owner_type)} ${displayName(pick)} ${timeoutTag}</div>
            <div class="tick-player">${pick.player_name}</div>
            <div class="tick-meta">${pick.position} &middot; ${pick.nfl_team || '—'}</div>
            <button class="why-btn" type="button" data-pick="${pick.id}">Why?</button>
            <div class="why-text hidden" id="why-${pick.id}">${pick.reasoning || ''}</div>
          </div>
        </div>`;
    });

    el.ticker.innerHTML = html;
    el.ticker.querySelectorAll('.why-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.getElementById(`why-${btn.dataset.pick}`).classList.toggle('hidden');
      });
    });
  }

  function needBars(team) {
    const counts = {};
    team.roster.forEach((p) => { counts[p.position] = (counts[p.position] || 0) + 1; });
    return Object.keys(NEED_TARGETS).map((pos) => {
      const have = counts[pos] || 0;
      const pct = Math.min(100, Math.round((have / NEED_TARGETS[pos]) * 100));
      return `
        <div class="need-row">
          <span class="need-pos">${pos}</span>
          <div class="need-track"><div class="need-fill" style="width:${pct}%"></div></div>
        </div>`;
    }).join('');
  }

  function paintTimer(seconds) {
    const timerEl = document.getElementById('human-timer');
    if (!timerEl || typeof seconds !== 'number') return;
    const remaining = Math.max(0, Math.round(seconds));
    const m = Math.floor(remaining / 60);
    const s = remaining % 60;
    timerEl.textContent = `${m}:${String(s).padStart(2, '0')}`;
    timerEl.classList.toggle('timer-low', remaining <= 10);
  }

  function renderClock() {
    const { league, on_the_clock: onClock } = state;

    if (league.draft_status === 'complete') {
      el.clockContent.innerHTML = `<div class="clock-team">Draft complete 🏆</div>`;
      return;
    }
    if (!onClock) {
      el.clockContent.innerHTML = `<div class="clock-team">Waiting to start...</div>`;
      return;
    }

    const team = state.teams.find((t) => t.id === onClock.team_id);
    const isMe = onClock.team_id === state.my_team_id;
    const upNext = state.picks
      .filter((p) => !p.player_id && p.overall_pick > onClock.overall_pick)
      .slice(0, 5);

    const upNextHtml = upNext.map((p) => `
      <li>${pickCode(p.round, p.pick_in_round)} &mdash; ${ownerIcon(p.owner_type)} ${displayName(p)}</li>
    `).join('');

    if (onClock.owner_type === 'ai') {
      el.clockContent.innerHTML = `
        <div class="invite-label">On the clock</div>
        <div class="clock-team">${ownerIcon('ai')} ${displayName(onClock).toUpperCase()}</div>
        <div class="hint">Round ${onClock.round}, Pick ${onClock.pick_in_round} (overall #${onClock.overall_pick})</div>
        <div class="thinking" id="thinking-indicator">picking<span class="dots"><span>.</span><span>.</span><span>.</span></span></div>
        ${team ? `<div class="need-panel">${needBars(team)}</div>` : ''}
        ${upNext.length ? `<div class="up-next"><div class="invite-label">Up next</div><ul>${upNextHtml}</ul></div>` : ''}
      `;
    } else {
      el.clockContent.innerHTML = `
        <div class="invite-label">${isMe ? "You're on the clock" : 'On the clock'}</div>
        <div class="clock-team">${ownerIcon('human')} ${displayName(onClock)}</div>
        <div class="hint">Round ${onClock.round}, Pick ${onClock.pick_in_round} (overall #${onClock.overall_pick})</div>
        ${isMe ? '<div class="human-timer" id="human-timer">--:--</div>' : '<div class="hint">Waiting for their pick&hellip;</div>'}
        ${team ? `<div class="need-panel">${needBars(team)}</div>` : ''}
        ${upNext.length ? `<div class="up-next"><div class="invite-label">Up next</div><ul>${upNextHtml}</ul></div>` : ''}
      `;
      if (isMe) paintTimer(state.remaining_seconds || 0);
    }
  }

  function canPickNow() {
    return state.league.draft_status === 'in_progress' && state.on_the_clock
      && state.on_the_clock.owner_type === 'human' && state.on_the_clock.team_id === state.my_team_id;
  }

  function renderPlayers() {
    const canPick = canPickNow();
    const q = filterQuery.trim().toLowerCase();
    const rows = state.available_players
      .filter((p) => (!filterPos || p.position === filterPos))
      .filter((p) => (!q || p.full_name.toLowerCase().includes(q)))
      .slice(0, 100);

    el.playersBody.innerHTML = rows.map((p) => `
      <tr>
        <td><button type="button" class="queue-star ${queue.includes(p.id) ? 'queued' : ''}" data-player="${p.id}" title="Queue this player">★</button></td>
        <td>${p.full_name}</td>
        <td><span class="badge badge-pos badge-pos-${p.position}">${p.position}</span></td>
        <td>${p.nfl_team || '—'}</td>
        <td>${p.search_rank < 999999 ? p.search_rank : '—'}</td>
        <td>${canPick ? `<button class="btn btn-small draft-btn" data-player="${p.id}">Draft</button>` : ''}</td>
      </tr>
    `).join('') || `<tr><td colspan="6" class="row-open">No players match.</td></tr>`;

    el.playersBody.querySelectorAll('.draft-btn').forEach((btn) => {
      btn.addEventListener('click', () => draftPlayer(btn.dataset.player));
    });
    el.playersBody.querySelectorAll('.queue-star').forEach((btn) => {
      btn.addEventListener('click', () => toggleQueue(btn.dataset.player));
    });
  }

  function renderQueue() {
    const availableIds = new Set(state.available_players.map((p) => p.id));
    const pruned = queue.filter((id) => availableIds.has(id));
    if (pruned.length !== queue.length) {
      queue = pruned;
      saveQueue();
    }
    el.queueCount.textContent = queue.length ? String(queue.length) : '';

    const byId = {};
    state.available_players.forEach((p) => { byId[p.id] = p; });
    const canPick = canPickNow();

    el.queueBody.innerHTML = queue.map((id, i) => {
      const p = byId[id];
      if (!p) return '';
      return `
        <tr>
          <td>
            <div class="queue-reorder">
              <button type="button" data-move="up" data-player="${p.id}" ${i === 0 ? 'disabled' : ''}>&uarr;</button>
              <button type="button" data-move="down" data-player="${p.id}" ${i === queue.length - 1 ? 'disabled' : ''}>&darr;</button>
            </div>
          </td>
          <td>${p.full_name}</td>
          <td><span class="badge badge-pos badge-pos-${p.position}">${p.position}</span></td>
          <td>${p.nfl_team || '—'}</td>
          <td>${p.search_rank < 999999 ? p.search_rank : '—'}</td>
          <td>
            ${canPick ? `<button class="btn btn-small draft-btn" data-player="${p.id}">Draft</button>` : ''}
            <button type="button" class="queue-star queued" data-player="${p.id}" title="Remove from queue">★</button>
          </td>
        </tr>`;
    }).join('') || `<tr><td colspan="6" class="row-open">No players queued yet &mdash; star players on the Players tab.</td></tr>`;

    el.queueBody.querySelectorAll('.draft-btn').forEach((btn) => {
      btn.addEventListener('click', () => draftPlayer(btn.dataset.player));
    });
    el.queueBody.querySelectorAll('.queue-star').forEach((btn) => {
      btn.addEventListener('click', () => toggleQueue(btn.dataset.player));
    });
    el.queueBody.querySelectorAll('[data-move]').forEach((btn) => {
      btn.addEventListener('click', () => moveInQueue(btn.dataset.player, btn.dataset.move === 'up' ? -1 : 1));
    });
  }

  function renderLineup() {
    const team = state.teams.find((t) => t.id === state.my_team_id);
    if (!team) {
      el.lineupBody.innerHTML = `<tr><td colspan="4" class="row-open">No team found.</td></tr>`;
      return;
    }
    const remaining = team.roster.slice().sort((a, b) => {
      const ar = a.player_rank == null ? 999999 : a.player_rank;
      const br = b.player_rank == null ? 999999 : b.player_rank;
      return ar - br;
    });
    const slots = [];

    Object.keys(STARTER_REQUIREMENTS).forEach((pos) => {
      const count = STARTER_REQUIREMENTS[pos];
      for (let i = 0; i < count; i++) {
        const idx = remaining.findIndex((p) => p.position === pos);
        const pick = idx === -1 ? null : remaining.splice(idx, 1)[0];
        slots.push({ slot: count === 1 ? pos : `${pos}${i + 1}`, pick });
      }
    });
    const flexIdx = remaining.findIndex((p) => FLEX_ELIGIBLE.includes(p.position));
    slots.push({ slot: 'FLEX', pick: flexIdx === -1 ? null : remaining.splice(flexIdx, 1)[0] });

    const benchHtml = remaining.map((p) => `
      <tr>
        <td><span class="badge badge-pos">BN</span></td>
        <td>${p.player_name}</td>
        <td><span class="badge badge-pos badge-pos-${p.position}">${p.position}</span></td>
        <td>${p.nfl_team || '—'}</td>
      </tr>`).join('');

    const starterHtml = slots.map(({ slot, pick }) => `
      <tr class="${pick ? '' : 'row-open'}">
        <td><span class="badge badge-pos">${slot}</span></td>
        ${pick
          ? `<td>${pick.player_name}</td><td><span class="badge badge-pos badge-pos-${pick.position}">${pick.position}</span></td><td>${pick.nfl_team || '—'}</td>`
          : `<td colspan="3" class="open-slot">Empty</td>`}
      </tr>`).join('');

    el.lineupBody.innerHTML = starterHtml + benchHtml
      || `<tr><td colspan="4" class="row-open">No picks yet.</td></tr>`;
  }

  function renderTeams() {
    el.teamsPanel.innerHTML = state.teams.map((t) => {
      const isOnClock = state.on_the_clock && state.on_the_clock.team_id === t.id && state.league.draft_status === 'in_progress';
      const sortedRoster = t.roster.slice().sort((a, b) => {
        const posDiff = ROSTER_POSITION_ORDER.indexOf(a.position) - ROSTER_POSITION_ORDER.indexOf(b.position);
        return posDiff !== 0 ? posDiff : a.overall_pick - b.overall_pick;
      });
      const rosterHtml = sortedRoster.map((p) => `<li>${p.position} &nbsp; ${p.player_name}</li>`).join('') || '<li class="hint">No picks yet</li>';
      return `
        <div class="card team-mini ${isOnClock ? 'team-mini-active' : ''}">
          <div class="clock-team team-mini-name">
            ${t.team_name}
            ${t.owner_type === 'human' ? `<span class="badge badge-human">👤 ${t.owner_name || ''}</span>` : `<span class="badge badge-ai">🤖 AI</span>`}
          </div>
          <ul class="roster-list">${rosterHtml}</ul>
        </div>`;
    }).join('');
  }

  function renderGrades() {
    const grades = state.grades;
    if (!grades) return;
    const teamRows = grades.teams.map((t) => `
      <div class="grade-row">
        <div class="grade-badge">${t.grade}</div>
        <div>
          <div class="clock-team" style="font-size:1rem">${t.team_name} ${t.owner_type === 'human' ? `<span class="badge badge-human">👤 ${t.owner_name || ''}</span>` : `<span class="badge badge-ai">🤖 AI</span>`}</div>
          <div class="hint">${t.strategy}${t.best_pick ? ` &middot; Best pick: ${t.best_pick.player_name}` : ''}${t.worst_pick ? ` &middot; Worst pick: ${t.worst_pick.player_name}` : ''}</div>
        </div>
      </div>
    `).join('');

    el.gradesContent.innerHTML = `
      <div class="grade-callouts">
        ${grades.biggest_value ? `<div class="badge badge-open">Best value: ${grades.biggest_value.player_name} (${grades.biggest_value.team_name})</div>` : ''}
        ${grades.biggest_reach ? `<div class="badge badge-ai">Biggest reach: ${grades.biggest_reach.player_name} (${grades.biggest_reach.team_name})</div>` : ''}
      </div>
      <div class="grade-list">${teamRows}</div>
    `;
  }

  function maybeShowRoundTransition(newRound) {
    if (lastRenderedRound !== null && newRound && newRound > lastRenderedRound) {
      el.roundTransition.textContent = `ROUND ${lastRenderedRound} COMPLETE`;
      el.roundTransition.classList.remove('hidden');
      return sleep(450).then(() => {
        el.roundTransition.textContent = `ROUND ${newRound}`;
        return sleep(450).then(() => {
          el.roundTransition.classList.add('hidden');
        });
      });
    }
    return Promise.resolve();
  }

  function render(newState) {
    state = newState;
    renderHeader();
    renderTicker();
    renderClock();
    renderPlayers();
    renderQueue();
    renderLineup();
    renderTeams();

    if (state.league.draft_status === 'complete') {
      renderGrades();
      el.completionOverlay.classList.remove('hidden');
    }
  }

  // ---- draft loop ---------------------------------------------------------

  function stopPolling() {
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
  }

  async function tick() {
    if (busy) return;
    stopPolling();
    if (state.league.draft_status !== 'in_progress' || !state.on_the_clock) {
      render(state);
      return;
    }

    const onClock = state.on_the_clock;

    if (onClock.owner_type === 'human') {
      render(state);
      pollHumanTurn(onClock.overall_pick);
      return;
    }

    render(state);
    const delay = speedDelay(state.league.ai_speed);
    busy = true;
    await sleep(delay);
    const newState = await fetchJSON(state.urls.advance, { method: 'POST' });
    busy = false;

    const prevRound = lastRenderedRound;
    lastRenderedRound = newState.on_the_clock ? newState.on_the_clock.round : prevRound;
    state = newState;
    render(state);

    if (newState.on_the_clock && prevRound && newState.on_the_clock.round > prevRound) {
      await maybeShowRoundTransition(newState.on_the_clock.round);
    } else {
      await sleep(REVEAL_PAUSE_MS);
    }
    tick();
  }

  function pollHumanTurn(watchOverallPick) {
    stopPolling();
    pollInterval = setInterval(async () => {
      const s = await fetchJSON(state.urls.state);
      if (s.league.draft_status !== 'in_progress' || !s.on_the_clock || s.on_the_clock.overall_pick !== watchOverallPick) {
        stopPolling();
        lastRenderedRound = s.on_the_clock ? s.on_the_clock.round : lastRenderedRound;
        state = s;
        tick();
        return;
      }
      state.remaining_seconds = s.remaining_seconds;
      if (s.on_the_clock.team_id === state.my_team_id) paintTimer(s.remaining_seconds);
    }, 1000);
  }

  async function draftPlayer(playerId) {
    stopPolling();
    const body = new URLSearchParams({ player_id: playerId });
    const newState = await fetchJSON(state.urls.pick, { method: 'POST', body });
    if (newState.error) {
      state = newState;
      render(state);
      return;
    }
    lastRenderedRound = newState.on_the_clock ? newState.on_the_clock.round : lastRenderedRound;
    state = newState;
    render(state);
    tick();
  }

  // ---- subtabs ----------------------------------------------------------

  const SUBTAB_PANELS = { players: el.playersPanel, queue: el.queuePanel, lineup: el.lineupPanel };

  el.subtabs.addEventListener('click', (e) => {
    const btn = e.target.closest('.draft-subtab');
    if (!btn) return;
    activeSubtab = btn.dataset.subtab;
    el.subtabs.querySelectorAll('.draft-subtab').forEach((b) => b.classList.remove('draft-subtab-active'));
    btn.classList.add('draft-subtab-active');
    Object.entries(SUBTAB_PANELS).forEach(([name, panel]) => {
      panel.classList.toggle('hidden', name !== activeSubtab);
    });
  });

  // ---- filters --------------------------------------------------------

  el.posTabs.addEventListener('click', (e) => {
    const tab = e.target.closest('.pos-tab');
    if (!tab) return;
    el.posTabs.querySelectorAll('.pos-tab').forEach((t) => t.classList.remove('pos-tab-active'));
    tab.classList.add('pos-tab-active');
    filterPos = tab.dataset.pos || '';
    renderPlayers();
  });

  el.search.addEventListener('input', () => {
    filterQuery = el.search.value;
    renderPlayers();
  });

  // ---- boot -------------------------------------------------------------

  render(state);
  if (state.league.draft_status === 'in_progress') {
    tick();
  } else if (state.league.draft_status === 'complete') {
    renderGrades();
    el.completionOverlay.classList.remove('hidden');
  }
})();
