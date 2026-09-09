(function () {
  const script = document.currentScript;
  const leagueId = script.dataset.leagueId;
  const teamId = script.dataset.teamId;
  const swapUrl = `/leagues/${leagueId}/team/${teamId}/lineup-swap`;

  let dragPickId = null;

  function clearDragState() {
    document.querySelectorAll('.lineup-row, .slot-drop-target').forEach((row) => {
      row.classList.remove('dragging', 'drag-over');
    });
  }

  async function sendMove(body) {
    const res = await fetch(swapUrl, { method: 'POST', body });
    const data = await res.json().catch(() => ({}));
    if (data.ok) {
      window.location.reload();
    } else {
      alert(data.message || "Can't put that player there — the position doesn't fit that slot.");
    }
  }

  function doSwap(pickA, pickB) {
    sendMove(new URLSearchParams({ pick_a: pickA, pick_b: pickB }));
  }

  function doMove(pickA, targetSlot) {
    sendMove(new URLSearchParams({ pick_a: pickA, target_slot: targetSlot }));
  }

  // Native HTML5 drag-and-drop has no built-in way to say "only start a
  // drag from this child element" -- draggable="true" makes the whole row
  // a drag source from a mousedown anywhere in it. So the row starts
  // non-draggable, and only a mousedown on its own drag-handle (the ⠿
  // dots) arms it just before the browser's own drag-detection kicks in.
  // That leaves a plain click/drag anywhere else on the row -- the player
  // name link included -- to behave like normal content instead of
  // hijacking the click into a drag.
  document.querySelectorAll('tr.lineup-row').forEach((row) => {
    const handle = row.querySelector('.drag-handle');
    if (handle) {
      handle.addEventListener('mousedown', () => { row.draggable = true; });
    }

    row.addEventListener('dragstart', (e) => {
      dragPickId = row.dataset.pickId;
      row.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', dragPickId);
    });

    row.addEventListener('dragend', () => {
      row.draggable = false;
      clearDragState();
    });
  });

  // Safety net: a mousedown on the handle with no drag following (a plain
  // click, or a drag that leaves the window and never fires dragend) would
  // otherwise leave the row armed -- disarm on any mouseup.
  document.addEventListener('mouseup', () => {
    document.querySelectorAll('tr.lineup-row[draggable="true"]').forEach((row) => {
      row.draggable = false;
    });
  });

  document.querySelectorAll('tr.lineup-row, tr.slot-drop-target').forEach((row) => {
    row.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (row.dataset.pickId !== dragPickId) row.classList.add('drag-over');
    });

    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));

    row.addEventListener('drop', (e) => {
      e.preventDefault();
      const targetPickId = row.dataset.pickId;
      const targetSlot = row.dataset.slotCode;
      clearDragState();
      if (dragPickId && targetPickId && targetPickId !== dragPickId) {
        doSwap(dragPickId, targetPickId);
      } else if (dragPickId && targetSlot) {
        doMove(dragPickId, targetSlot);
      }
      dragPickId = null;
    });
  });
})();
