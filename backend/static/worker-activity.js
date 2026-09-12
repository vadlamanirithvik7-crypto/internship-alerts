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
      action.href = task.has_questions ? '#application-questions' : task.task_url;
      action.className = 'button secondary';
      article.append(element('br'), action);
    }
    return article;
  }
  const forms = new Map();
  let saving = false;
  function answerForm(task) {
    const article = element('article');
    article.style.cssText = 'padding:16px 0;border-bottom:1px solid var(--border)';
    article.append(element('h3', `${task.company} · ${task.title}`));
    if (task.blockers.length) {
      article.append(element('p', 'This form also has an employer-site issue. Saving answers lets the worker retry, but it may still need manual review:'));
      task.blockers.forEach(reason => article.append(element('p', reason)));
    }
    const form = element('form');
    form.className = 'assistant-form';
    form.method = 'post';
    form.action = `/autopilot/tasks/${task.id}/answers`;
    const version = element('input');
    version.type = 'hidden'; version.name = 'version'; version.value = task.version;
    form.append(version);
    task.fields.forEach((field, i) => {
      const label = element('label');
      label.append(element('span', field.label));
      const input = element(field.options.length ? 'select' : 'textarea');
      input.name = `answer_${i}`; input.required = true;
      if (field.options.length) {
        const empty = element('option', 'Choose your answer'); empty.value = ''; input.append(empty);
        field.options.forEach(value => { const option = element('option', value); option.value = value; input.append(option); });
      } else { input.rows = 2; input.maxLength = 1500; }
      label.append(input); form.append(label);
    });
    const rememberLabel = element('label');
    const remember = element('input'); remember.type = 'checkbox'; remember.name = 'remember'; remember.value = 'yes';
    rememberLabel.append(remember, element('span', 'Remember these exact answers for future applications'));
    const button = element('button', 'Save and continue'); button.type = 'submit';
    const feedback = element('p'); feedback.setAttribute('role', 'status');
    form.append(rememberLabel, button, feedback);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (saving) return;
      saving = true; button.disabled = true; feedback.textContent = 'Saving your answers…';
      try {
        const response = await fetch(form.action, {method:'POST', body:new FormData(form),
          headers:{Accept:'application/json'}, signal:AbortSignal.timeout(15000)});
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Could not save answers. Please try again.');
        document.getElementById('answers-result').textContent = `${task.company}: ${data.message}`;
        forms.delete(task.id); article.remove();
      } catch (error) {
        feedback.textContent = error.message || 'Could not save answers. Your text is still here; please try again.';
      } finally { saving = false; button.disabled = false; refresh(); }
    });
    article.append(form);
    return {article, version:task.version};
  }
  function updateQuestions(data) {
    // Never rebuild existing forms during polling: this preserves typing and focus.
    if (saving) return;
    const container = document.getElementById('worker-questions');
    // Older pages/API responses may still be open during a rolling deployment.
    if (!container || !Array.isArray(data.attention)) return;
    const ids = new Set(data.answer_ids);
    for (const [id, entry] of forms) {
      if (!ids.has(id)) { entry.article.remove(); forms.delete(id); }
    }
    for (const task of data.attention) {
      const existing = forms.get(task.id);
      if (existing && existing.version === task.version) continue;
      if (existing) existing.article.remove();
      const entry = answerForm(task); forms.set(task.id, entry); container.append(entry.article);
    }
    document.getElementById('questions-summary').textContent = `${data.answer_count} applications have questions you can answer here. ` +
      (data.answer_count > forms.size ? `Showing ${forms.size}; more appear as you finish these. ` : '') +
      `${data.manual_count} other applications have employer-site issues with no question to answer.`;
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
      document.getElementById('worker-counts').textContent = `${data.total} applications · Confirmed submissions: ${data.counts.submitted || 0} · ` +
        Object.entries(data.counts).filter(([state]) => state !== 'submitted').map(([state,count]) => `${label(state)}: ${count}`).join(' · ');
      updateQuestions(data);
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
