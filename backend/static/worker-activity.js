(() => {
  const status = document.getElementById('worker-live-status');
  if (!status) return;
  const label = value => value === 'needs_input' ? 'Needs attention' : value.replaceAll('_', ' ').replace(/\b\w/g, char => char.toUpperCase());
  const time = value => value ? new Date(value).toLocaleTimeString() : 'not connected yet';
  const element = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function taskView(task) {
    const article = element('article');
    article.style.cssText = 'padding:12px 0;border-bottom:1px solid var(--border)';
    const link = element('a', `${task.company} · ${task.title}`);
    link.href = task.task_url || `/jobs/${task.posting_id}`;
    article.append(link, element('p', `${task.status_label || label(task.state)} — ${task.detail}`),
      element('small', `Updated ${time(task.updated_at)}`));
    if (task.state === 'needs_input' && task.task_url) {
      const action = element('a', task.action_label || 'Review issue');
      action.href = task.task_url;
      action.className = 'button secondary';
      article.append(element('br'), action);
    }
    return article;
  }
  let busy = false;
  async function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const response = await fetch('/autopilot/activity', {
        headers: {Accept:'application/json'}, cache:'no-store', signal:AbortSignal.timeout(10000)
      });
      if (!response.ok) throw new Error(response.status === 401 ? 'Sign in to Radar to view live activity.' : 'Cannot reach Radar. Displayed status may be outdated.');
      const data = await response.json();
      document.getElementById('worker-mode').textContent = `Worker: ${label(data.mode)}`;
      document.getElementById('worker-connection').textContent = `${data.connected ? 'Mac connected' : 'Mac offline'} · Last seen ${time(data.last_seen_at)}`;
      status.textContent = data.mode === 'stopped' ? 'Stopped. Use Start / Resume and the Mac launcher to begin.' :
        data.mode === 'paused' ? 'Paused. Your queue is saved.' :
        !data.connected ? 'Mac disconnected. Waiting for the worker to reconnect; displayed activity is the last known state.' :
        data.active ? data.active.detail : (data.counts.queued ? 'Connected. Taking the next application from your queue.' : 'Connected. Watching for new matching internships.');
      const current = document.getElementById('worker-current');
      current.replaceChildren();
      if (data.active) current.append(taskView(data.active));
      document.getElementById('worker-counts').textContent = `${data.total} applications · ` +
        Object.entries(data.counts).map(([state,count]) => `${label(state)}: ${count}`).join(' · ');
      const recent = document.getElementById('worker-recent');
      recent.replaceChildren(...data.recent.map(taskView));
      if (!data.recent.length) recent.append(element('p','No attempts yet. Queued internships will appear here as the worker handles them.'));
      document.getElementById('worker-updated').textContent = `Live · Updated ${time(data.checked_at)} · Refreshes every 3 seconds. Full history is in the application queue below.`;
    } catch (error) {
      status.textContent = error.message || 'Connection interrupted. Retrying automatically.';
      document.getElementById('worker-updated').textContent = 'Live updates interrupted. Displayed activity may be outdated; retrying automatically.';
    } finally { busy = false; }
  }
  refresh();
  setInterval(refresh, 3000);
  document.addEventListener('visibilitychange',refresh);
})();
