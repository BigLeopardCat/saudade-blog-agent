/* Structured work updates are display-only; IDs locate rows and never dispatch actions. */
(function (g) {
  const statuses = new Set(['running', 'waiting', 'completed', 'failed', 'cancelled', 'unknown']);
  const valid = value => value && typeof value === 'object' && !Array.isArray(value)
    && typeof value.id === 'string' && value.id.length > 0 && value.id.length <= 128
    && typeof value.stage === 'string' && value.stage.length > 0 && value.stage.length <= 64
    && statuses.has(value.status);
  const upsert = (box, data, append) => {
    if (!valid(data?.work) || typeof data.text !== 'string' || data.text.length > 300) return false;
    const body = box.querySelector('.agent-process-body');
    let line = [...body.querySelectorAll('.agent-process-line')]
      .find(item => item.dataset.workId === data.work.id);
    const created = !line;
    if (created) line = document.createElement('div');
    line.className = 'agent-process-line work';
    line.dataset.workId = data.work.id;
    line.dataset.workStage = data.work.stage;
    line.dataset.workStatus = data.work.status;
    line.textContent = data.text;
    if (created) append(line);
    return true;
  };
  g.__waifuWorkProgress = {valid, upsert};
})(globalThis);
