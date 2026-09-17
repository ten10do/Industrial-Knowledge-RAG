const state = {
  manifest: null, page: null, annotation: null, prediction: null,
  selectedBlock: null, selectedTable: null, dirty: false, saveTimer: null, drawStart: null,
  mode: 'PAGE_OVERVIEW', reviewData: null, queuePosition: 0, undoStack: [],
  currentEdgeId: null, activeTableId: null,
};
const $ = id => document.getElementById(id);
const RISK_ORDER = { AMBIGUOUS: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };

async function getJSON(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

async function loadManifest() {
  state.manifest = await getJSON('/api/manifest');
  state.manifest.progressByKey = {};
  $('progress').textContent = `${state.manifest.progress.counts.REVIEWED}/${state.manifest.progress.total} reviewed · FINAL_GATE unconsumed`;
  renderPageList();
}

async function loadReviewData() {
  state.reviewData = await getJSON('/api/review-data');
  renderQueueSummary();
}

async function loadDashboard() {
  try {
    const workload = await getJSON('/api/workload');
    $('dashboard').innerHTML = `
      <div class="dash-row"><span>Pages reviewed</span><strong>${workload.pages_reviewed}/${workload.pages_total}</strong></div>
      <div class="dash-row"><span>Tables reviewed</span><strong>${workload.tables_reviewed}/${workload.tables_total}</strong></div>
      <div class="dash-row"><span>High-risk remaining</span><strong>${workload.high_risk_remaining}</strong></div>
      <div class="dash-row"><span>Ambiguous remaining</span><strong>${workload.ambiguous_remaining}</strong></div>
      <div class="dash-row"><span>Ownership unresolved</span><strong>${workload.ownership_unresolved}</strong></div>
      <div class="dash-row"><span>Second-review pending</span><strong>${workload.second_review.filter(item => item.pending).length}/${workload.second_review.length}</strong></div>`;
  } catch (error) { $('dashboard').textContent = `Dashboard unavailable: ${error.message}`; }
}

function pageStatus(key) {
  return state.manifest?.progressByKey?.[key] || 'NOT_STARTED';
}

function pageMeta(key) {
  return state.reviewData?.page_risk?.[key] || null;
}

function edgeRisk(key, relationshipId) {
  const meta = pageMeta(key);
  if (!meta) return null;
  return meta.edges.find(edge => edge.relationship_id === relationshipId) || null;
}

function currentQueue() {
  if (!state.reviewData?.available) return [];
  return state.reviewData.review_queue || [];
}

function renderQueueSummary() {
  const queue = currentQueue();
  if (!queue.length) { $('queue-summary').textContent = 'No review queue (run build-review-queue).'; return; }
  const counts = { AMBIGUOUS: 0, HIGH: 0, MEDIUM: 0, LOW: 0 };
  queue.forEach(item => { counts[item.risk] = (counts[item.risk] || 0) + 1; });
  const second = state.reviewData.second_review_queue || [];
  $('queue-summary').innerHTML =
    `<div class="tag risk-AMBIGUOUS">AMBIGUOUS ${counts.AMBIGUOUS}</div>` +
    `<div class="tag risk-HIGH">HIGH ${counts.HIGH}</div>` +
    `<div class="tag risk-MEDIUM">MEDIUM ${counts.MEDIUM}</div>` +
    `<div class="tag risk-LOW">LOW ${counts.LOW}</div>` +
    `<div class="hint">${second.length} pages flagged SECOND_REVIEW_REQUIRED</div>`;
}

function renderPageList() {
  const filter = $('page-filter').value.toLowerCase();
  const secondKeys = new Set((state.reviewData?.second_review_queue || []).map(item => item.page_key));
  $('page-list').replaceChildren(...state.manifest.pages.filter(page => `${page.key} ${page.slices.join(' ')}`.toLowerCase().includes(filter)).map(page => {
    const button = document.createElement('button');
    const secondBadge = secondKeys.has(page.key) ? ' <span class="badge">2nd</span>' : '';
    button.className = `page-button ${state.page?.key === page.key ? 'active' : ''}`;
    button.innerHTML = `${page.doc_id} · page ${page.page_number}${secondBadge}<small>${page.slices.join(' · ')}${pageMeta(page.key) ? ' · risk ' + (pageMeta(page.key).layout_risk_score ?? '') : ''}</small>`;
    button.onclick = () => loadPage(page.key);
    return button;
  }));
}

async function loadPage(key) {
  if (state.dirty) await saveAnnotation();
  const payload = await getJSON(`/api/page?key=${encodeURIComponent(key)}`);
  state.page = payload.page; state.annotation = payload.annotation; state.prediction = payload.prediction;
  state.selectedBlock = null; state.selectedTable = null; state.dirty = false;
  state.undoStack = []; state.currentEdgeId = null;
  const meta = pageMeta(key);
  state.activeTableId = highestRiskTable(key);
  $('page-title').textContent = `${state.page.doc_id} · page ${state.page.page_number}`;
  $('slice-tags').innerHTML = state.page.slices.map(value => `<span class="tag">${value}</span>`).join('');
  $('page-preview').src = `/workspace/${state.page.preview}`;
  $('page-status').value = state.annotation.status;
  $('page-confirmed').checked = state.annotation.human_confirmed;
  $('show-predictions').checked = false;
  $('prediction-option').replaceChildren(...Object.keys(state.prediction.options).map(key2 => new Option(state.manifest.prediction_labels[key2], key2)));
  $('prediction-option').disabled = true; $('import-prediction').disabled = true;
  $('validation').textContent = JSON.stringify(state.annotation.audit?.validation || {}, null, 2);
  $('reviewed-gate').textContent = '';
  renderAll(); renderPageList(); renderDashboard(); setSaveState('Loaded · predictions hidden');
}

function highestRiskTable(key) {
  const meta = pageMeta(key);
  const tables = state.annotation?.table_regions?.length ? state.annotation.table_regions : [];
  if (!meta || !tables.length) return tables[0]?.table_region_id || null;
  const scores = {};
  meta.edges.forEach(edge => {
    if (!edge.table_region_id) return;
    const weight = { AMBIGUOUS: 3, HIGH: 2, MEDIUM: 1, LOW: 0 }[edge.risk] ?? 0;
    scores[edge.table_region_id] = (scores[edge.table_region_id] || 0) + weight;
  });
  return tables.map(table => table.table_region_id).sort((a, b) => (scores[b] || 0) - (scores[a] || 0))[0] || tables[0].table_region_id;
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll('.mode-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.mode === mode));
  document.querySelectorAll('.mode-panel').forEach(panel => panel.classList.add('hidden'));
  const panelFor = { PAGE_OVERVIEW: 'overview-panel', TABLE_STRUCTURE_REVIEW: 'table-review-panel', OWNERSHIP_RISK_REVIEW: 'ownership-panel', AMBIGUOUS_REVIEW: 'ambiguous-panel', FINAL_PAGE_REVIEW: 'final-review-panel' };
  $(panelFor[mode]).classList.remove('hidden');
  renderAll();
}

function renderAll() {
  renderOverlay(); renderBlocks(); renderTables(); renderGroups(); renderEdges(); renderEntityIds();
  renderOverview(); renderTableReview(); renderOwnership(); renderAmbiguous(); renderFinal();
}

function renderOverlay() {
  if (!state.annotation) return;
  const svg = $('overlay');
  svg.setAttribute('viewBox', `0 0 ${state.page.width} ${state.page.height}`);
  const nodes = [];
  const rect = (item, kind, prediction = false) => {
    const bbox = item.bbox; if (!bbox || bbox.length !== 4) return;
    const node = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    node.setAttribute('x', bbox[0]); node.setAttribute('y', bbox[1]); node.setAttribute('width', bbox[2] - bbox[0]); node.setAttribute('height', bbox[3] - bbox[1]);
    const selected = item.block_id === state.selectedBlock || item.table_region_id === state.selectedTable || item.table_region_id === state.activeTableId;
    node.setAttribute('class', `box ${kind} ${prediction ? 'prediction' : ''} ${selected ? 'selected' : ''}`);
    if (!prediction) node.onclick = event => { event.stopPropagation(); if (item.block_id) selectBlock(item.block_id); if (kind === 'table') selectTable(item.table_region_id); };
    nodes.push(node);
  };
  state.annotation.table_regions.forEach(item => rect(item, 'table'));
  state.annotation.blocks.forEach(item => rect(item, 'block'));
  if ($('show-predictions').checked) {
    const option = state.prediction.options[$('prediction-option').value];
    option?.table_regions?.forEach(item => rect(item, 'table', true));
    option?.blocks?.forEach(item => rect(item, 'block', true));
  }
  svg.replaceChildren(...nodes);
}

// ---------------------------------------------------------------- overview
function renderOverview() {
  if (!state.annotation || state.mode !== 'PAGE_OVERVIEW') return;
  const meta = pageMeta(state.page.key);
  const counts = meta?.counts || {};
  const primary = meta?.primary_option || '—';
  const cells = [
    ['Detected tables', state.annotation.table_regions.length],
    ['Blocks', state.annotation.blocks.length],
    ['Row groups', state.annotation.rows.length],
    ['Column groups', state.annotation.columns.length],
    ['Ownership edges', state.annotation.relationships.length],
    ['High-risk edges', counts.HIGH || 0],
    ['Ambiguous candidates', counts.AMBIGUOUS || 0],
    ['Layout risk score', meta?.layout_risk_score ?? '—'],
    ['Primary suggestion', primary],
  ];
  $('overview-counts').replaceChildren(...cells.map(([label, value]) => {
    const node = document.createElement('div');
    node.className = 'count-item';
    node.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
    return node;
  }));
}

// ----------------------------------------------------------- table review
function tableRows(tableId) { return state.annotation.rows; }
function tableEdgeIds(tableId) {
  return state.annotation.relationships.filter(edge => (edge.table_region_id || '') === tableId).map(edge => edge.relationship_id);
}

function renderTableReview() {
  if (!state.annotation || state.mode !== 'TABLE_STRUCTURE_REVIEW') return;
  const meta = pageMeta(state.page.key);
  $('table-review-list').replaceChildren(...state.annotation.table_regions.map(table => {
    const id = table.table_region_id;
    const edges = tableEdgeIds(id);
    const risks = { HIGH: 0, AMBIGUOUS: 0, MEDIUM: 0, LOW: 0 };
    edges.forEach(edgeId => { const risk = edgeRisk(state.page.key, edgeId)?.risk; if (risk) risks[risk] += 1; });
    const node = document.createElement('div');
    node.className = `table-review ${state.activeTableId === id ? 'selected' : ''}`;
    node.innerHTML = `
      <div class="table-head"><strong>${id}</strong><span>${table.human_confirmed ? '✓ structure accepted' : 'not accepted'}</span></div>
      <div class="table-stats">rows ${state.annotation.rows.length} · columns ${state.annotation.columns.length} · blocks ${state.annotation.blocks.filter(b => b.table_region_id === id).length} · edges ${edges.length} <span class="tag risk-HIGH">HIGH ${risks.HIGH}</span> <span class="tag risk-AMBIGUOUS">AMBIG ${risks.AMBIGUOUS}</span> <span class="tag risk-LOW">LOW ${risks.LOW}</span></div>
      <div class="inline-buttons">
        <button class="accept-table">Accept Table Structure</button>
        <button class="accept-low">Accept visible low-risk ownership</button>
        <button class="focus-table">Focus</button>
      </div>`;
    node.querySelector('.accept-table').onclick = () => bulkOperation('accept_table_structure', id);
    node.querySelector('.accept-low').onclick = () => bulkOperation('accept_low_risk_ownership', id);
    node.querySelector('.focus-table').onclick = () => { state.activeTableId = id; state.selectedTable = id; renderAll(); };
    node.onclick = event => { if (event.target.tagName !== 'BUTTON') { state.activeTableId = id; state.selectedTable = id; renderAll(); } };
    return node;
  }));
  if (!state.annotation.table_regions.length) {
    $('table-review-list').innerHTML = '<p class="hint">No tables on this page. Import a suggestion first if the page has tabular structure.</p>';
  }
}

async function bulkOperation(operation, tableId) {
  setSaveState('Applying bulk confirmation…');
  try {
    const result = await getJSON(`/api/bulk?key=${encodeURIComponent(state.page.key)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operation, table_region_id: tableId, owner_clicked: true }),
    });
    if (result.saved === false) throw new Error(JSON.stringify(result.validation?.errors || result.error || 'rejected'));
    await loadPage(state.page.key);
    setSaveState(`Bulk ${operation} · ${result.affected?.length ?? 0} items accepted`);
  } catch (error) { setSaveState(`Bulk failed: ${error.message}`); }
}

// -------------------------------------------------------- ownership review
function edgeFilterActive(edge, riskEntry) {
  const filter = $('edge-filter').value;
  if (filter === 'all') return true;
  if (filter === 'unresolved') return !edge.human_confirmed && edge.relationship_type !== 'AMBIGUOUS';
  if (filter === 'HIGH' || filter === 'AMBIGUOUS') return riskEntry?.risk === filter;
  if (filter === 'HEADER_APPLIES_TO') return edge.relationship_type === 'HEADER_APPLIES_TO';
  if (!riskEntry) return false;
  if (filter === 'unit') return riskEntry.reasons.includes('unit_header_binding');
  if (filter === 'model') return riskEntry.reasons.includes('model_column_binding');
  if (filter === 'merged') return riskEntry.reasons.includes('cross_column_span_or_merged_header');
  if (filter === 'parameter') return riskEntry.reasons.includes('parameter_code_composite_row');
  if (filter === 'fault') return riskEntry.reasons.includes('fault_code_composite_row');
  return true;
}

function ownershipStatus(relationshipId) {
  return state.annotation.authority?.ownership_status?.[relationshipId] || 'UNREVIEWED';
}

function renderOwnership() {
  if (!state.annotation || state.mode !== 'OWNERSHIP_RISK_REVIEW') return;
  const meta = pageMeta(state.page.key);
  const counts = meta?.counts || {};
  $('ownership-counts').innerHTML = ['HIGH', 'MEDIUM', 'LOW', 'AMBIGUOUS'].map(level => `<div class="count-item"><span>${level}</span><strong>${counts[level] || 0}</strong></div>`).join('');
  renderBundles(meta);
  renderEdgeList(meta);
}

function renderBundles(meta) {
  const bundles = meta?.bundles || [];
  if (!bundles.length) {
    $('bundle-list-high').innerHTML = '<p class="hint">No bundles. Import a suggestion first; predictions stay hidden until you enable them.</p>';
    $('bundle-list-ordinary').innerHTML = '';
    return;
  }
  const card = bundle => {
    const statuses = bundle.relationship_ids.map(id => ownershipStatus(id));
    const allReviewed = statuses.every(status => status !== 'UNREVIEWED');
    const node = document.createElement('div');
    node.className = `table-review risk-border-${bundle.risk}`;
    node.innerHTML = `
      <div class="table-head"><strong>${bundle.source_id || 'AMBIGUOUS source'}</strong><span class="tag risk-${bundle.risk}">${bundle.risk}</span><span>${allReviewed ? '✓ reviewed' : 'pending'}</span></div>
      <div class="table-stats">rule: header → ${bundle.target_value_count} values · edges ${bundle.relationship_ids.length}</div>
      <div class="table-stats">why: ${bundle.reasons.join(', ') || 'single confident header'}</div>
      <div class="inline-buttons">
        <button class="accept-bundle">ACCEPT BUNDLE</button>
        <button class="reject-bundle">REJECT BUNDLE</button>
        <button class="ambiguous-bundle">AMBIGUOUS BUNDLE</button>
        <button class="edit-bundle">EDIT</button>
      </div>
      <div class="bundle-edges hidden"></div>`;
    node.querySelector('.accept-bundle').onclick = () => bundleOperation('accept_ownership_bundle', bundle);
    node.querySelector('.reject-bundle').onclick = () => bundleOperation('reject_ownership_bundle', bundle);
    node.querySelector('.ambiguous-bundle').onclick = () => bundleOperation('ambiguous_ownership_bundle', bundle);
    node.querySelector('.edit-bundle').onclick = () => {
      const panel = node.querySelector('.bundle-edges');
      panel.classList.toggle('hidden');
      if (!panel.classList.contains('hidden') && !panel.children.length) {
        panel.replaceChildren(...bundle.relationship_ids.map(id => {
          const edgeRow = document.createElement('div');
          edgeRow.className = 'entity-row';
          edgeRow.innerHTML = `<span class="grow">${id}</span><span>${ownershipStatus(id)}</span>`;
          edgeRow.onclick = () => { state.currentEdgeId = id; edgeRow.scrollIntoView({ block: 'center' }); };
          return edgeRow;
        }));
      }
    };
    return node;
  };
  const highBundles = bundles.filter(bundle => bundle.requires_independent_review);
  const ordinaryBundles = bundles.filter(bundle => !bundle.requires_independent_review);
  $('bundle-list-high').replaceChildren(...(highBundles.length ? highBundles.map(card) : []));
  if (!highBundles.length) $('bundle-list-high').innerHTML = '<p class="hint">No high-risk bundles on this page.</p>';
  $('bundle-list-ordinary').replaceChildren(...(ordinaryBundles.length ? ordinaryBundles.map(card) : []));
  if (!ordinaryBundles.length) $('bundle-list-ordinary').innerHTML = '<p class="hint">No ordinary bundles on this page.</p>';
}

async function bundleOperation(operation, bundle) {
  setSaveState(`Applying ${operation}…`);
  try {
    const result = await getJSON(`/api/bulk?key=${encodeURIComponent(state.page.key)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operation, relationship_ids: bundle.relationship_ids, owner_clicked: true }),
    });
    if (result.saved === false) throw new Error(JSON.stringify(result.validation?.errors || result.error || 'rejected'));
    const prior = result.prior_state || {};
    if (Object.keys(prior).length) {
      pushUndo({
        label: `${operation} (${bundle.relationship_ids.length} edges)`,
        undo: () => restoreOwnershipState(prior),
      });
    }
    await loadPage(state.page.key);
    setSaveState(`${operation} · ${result.affected?.length ?? 0} edges`);
  } catch (error) { setSaveState(`Bundle failed: ${error.message}`); }
}

async function restoreOwnershipState(prior) {
  try {
    await getJSON(`/api/bulk?key=${encodeURIComponent(state.page.key)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operation: 'restore_ownership_state', prior_state: prior, owner_clicked: true }),
    });
    await loadPage(state.page.key);
  } catch (error) { setSaveState(`Undo failed: ${error.message}`); }
}

function renderEdgeList(meta) {
  if (!state.annotation.relationships.length) {
    $('edge-review-list').innerHTML = '<p class="hint">No imported relations yet. Open PAGE_OVERVIEW and import a suggestion first; predictions stay hidden until you enable them.</p>';
    return;
  }
  const edges = state.annotation.relationships
    .map(edge => ({ edge, riskEntry: edgeRisk(state.page.key, edge.relationship_id) }))
    .filter(item => edgeFilterActive(item.edge, item.riskEntry))
    .sort((a, b) => (RISK_ORDER[a.riskEntry?.risk || 'LOW'] - RISK_ORDER[b.riskEntry?.risk || 'LOW']));
  $('edge-review-list').replaceChildren(...edges.map(({ edge, riskEntry }) => {
    const row = document.createElement('div');
    row.className = `edge-row risk-border-${riskEntry?.risk || 'LOW'} ${state.currentEdgeId === edge.relationship_id ? 'selected' : ''}`;
    row.dataset.edgeId = edge.relationship_id;
    row.innerHTML = `
      <span class="tag risk-${riskEntry?.risk || 'LOW'}">${riskEntry?.risk || 'LOW'}</span>
      <strong>${edge.relationship_type}</strong>
      <span class="grow">${edge.source_id || '∅'} → ${edge.target_id}</span>
      <span class="reasons">${(riskEntry?.reasons || []).join(', ')}</span>
      <span>${ownershipStatus(edge.relationship_id)}</span>
      <div class="inline-buttons">
        <button class="accept" title="Accept (A)">✓</button>
        <button class="reject" title="Reject (R)">✗</button>
        <button class="ambiguous" title="Ambiguous (M)">?</button>
      </div>`;
    row.querySelector('.accept').onclick = () => acceptEdge(edge.relationship_id);
    row.querySelector('.reject').onclick = () => rejectEdge(edge.relationship_id);
    row.querySelector('.ambiguous').onclick = () => markAmbiguousEdge(edge.relationship_id);
    row.onclick = event => { if (event.target.tagName !== 'BUTTON') { state.currentEdgeId = edge.relationship_id; renderOwnership(); renderOverlay(); } };
    return row;
  }));
}

function logOperation(operation, affectedIds) {
  const audit = state.annotation.audit = state.annotation.audit || {};
  audit.operations = audit.operations || [];
  audit.operations.push({
    timestamp: new Date().toISOString(), operation, affected_ids: affectedIds, actor: 'owner',
  });
}

function pushUndo(entry) {
  state.undoStack.push(entry);
  if (state.undoStack.length > 200) state.undoStack.shift();
}

function acceptEdge(id) {
  const edge = state.annotation.relationships.find(item => item.relationship_id === id);
  if (!edge || edge.human_confirmed) return;
  pushUndo({ label: `accept ${id}`, undo: () => { edge.human_confirmed = false; } });
  edge.human_confirmed = true;
  logOperation('accept_relationship', [id]);
  changed();
}

function rejectEdge(id) {
  const index = state.annotation.relationships.findIndex(item => item.relationship_id === id);
  if (index < 0) return;
  const prior = state.annotation.relationships[index];
  pushUndo({ label: `reject ${id}`, undo: () => { state.annotation.relationships.splice(index, 0, prior); } });
  state.annotation.relationships.splice(index, 1);
  logOperation('reject_relationship', [id]);
  changed();
}

function markAmbiguousEdge(id) {
  const edge = state.annotation.relationships.find(item => item.relationship_id === id);
  if (!edge) return;
  const prior = { type: edge.relationship_type, confirmed: edge.human_confirmed };
  pushUndo({ label: `ambiguous ${id}`, undo: () => { edge.relationship_type = prior.type; edge.human_confirmed = prior.confirmed; } });
  edge.relationship_type = 'AMBIGUOUS';
  edge.human_confirmed = true; // confirming the ambiguity is an explicit owner action
  logOperation('mark_ambiguous', [id]);
  changed();
}

function undoLast() {
  const entry = state.undoStack.pop();
  if (!entry) { setSaveState('Nothing to undo'); return; }
  entry.undo();
  logOperation('undo', [entry.label]);
  setSaveState(`Undid: ${entry.label}`);
  changed();
}

// ------------------------------------------------------------ ambiguous
function renderAmbiguous() {
  if (!state.annotation || state.mode !== 'AMBIGUOUS_REVIEW') return;
  const candidates = state.annotation.relationships.filter(edge => {
    const risk = edgeRisk(state.page.key, edge.relationship_id);
    return edge.relationship_type === 'AMBIGUOUS' || risk?.risk === 'AMBIGUOUS';
  });
  $('ambiguous-list').replaceChildren(...candidates.map(edge => {
    const row = document.createElement('div');
    row.className = 'edge-row';
    row.innerHTML = `
      <span class="tag risk-AMBIGUOUS">AMBIGUOUS</span>
      <span class="grow">${edge.source_id || '∅'} → ${edge.target_id}</span>
      <span>${edge.human_confirmed ? 'resolved' : 'explicit'}</span>
      <div class="inline-buttons"><button class="keep">Keep AMBIGUOUS</button></div>`;
    row.querySelector('.keep').onclick = () => markAmbiguousEdge(edge.relationship_id);
    return row;
  }));
  if (!candidates.length) $('ambiguous-list').innerHTML = '<p class="hint">No ambiguous candidates on this page.</p>';
}

// ----------------------------------------------------------- final review
function renderFinal() {
  if (!state.annotation || state.mode !== 'FINAL_PAGE_REVIEW') return;
  const meta = pageMeta(state.page.key);
  const counts = meta?.counts || {};
  const annotationEdges = new Map(state.annotation.relationships.map(edge => [edge.relationship_id, edge]));
  let unresolvedHigh = 0, unresolvedAmbiguous = 0;
  (meta?.edges || []).forEach(riskEdge => {
    const current = annotationEdges.get(riskEdge.relationship_id);
    if (current?.relationship_type === 'AMBIGUOUS') return;
    if (current && !current.human_confirmed) {
      if (riskEdge.risk === 'HIGH') unresolvedHigh += 1;
      if (riskEdge.risk === 'AMBIGUOUS') unresolvedAmbiguous += 1;
    }
  });
  const tablesAccepted = state.annotation.table_regions.every(table => table.human_confirmed);
  const ownerAction = (state.annotation.audit?.operations?.length || 0) > 0 || state.annotation.audit?.prediction_imported;
  const checks = [
    ['Suggestion imported / owner action recorded', ownerAction],
    ['Table structures accepted', state.annotation.table_regions.length === 0 || tablesAccepted],
    ['High-risk relations resolved', unresolvedHigh === 0],
    ['Ambiguous relations explicitly marked', unresolvedAmbiguous === 0],
    ['Page human-confirmed', state.annotation.human_confirmed],
  ];
  $('final-checklist').replaceChildren(...checks.map(([label, ok]) => {
    const node = document.createElement('div');
    node.className = `check ${ok ? 'ok' : 'missing'}`;
    node.textContent = `${ok ? '✓' : '·'} ${label}`;
    return node;
  }));
  $('mark-reviewed').disabled = !checks.every(([, ok]) => ok);
  $('mark-reviewed').onclick = markReviewed;
}

async function markReviewed() {
  state.annotation.human_confirmed = true;
  state.annotation.status = 'REVIEWED';
  state.annotation.audit = state.annotation.audit || {};
  state.annotation.audit.review_count = (state.annotation.audit.review_count || 0) + 1;
  state.annotation.owner_clicked = true;
  setSaveState('Submitting REVIEWED…');
  try {
    const result = await getJSON(`/api/save?key=${encodeURIComponent(state.page.key)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(state.annotation),
    });
    delete state.annotation.owner_clicked;
    state.annotation.audit.validation = result.validation;
    $('reviewed-gate').textContent = result.saved
      ? 'Page marked REVIEWED.'
      : `REVIEWED rejected:\n${(result.validation?.errors || []).join('\n')}`;
    $('validation').textContent = JSON.stringify(result.validation, null, 2);
    if (!result.saved) {
      state.annotation.status = 'IN_PROGRESS';
      state.annotation.human_confirmed = false;
      $('page-status').value = 'IN_PROGRESS'; $('page-confirmed').checked = false;
    }
    state.dirty = false;
    setSaveState(result.saved ? 'REVIEWED saved' : 'REVIEWED blocked by completion gate');
    renderDashboard(); renderPageList();
  } catch (error) {
    delete state.annotation.owner_clicked;
    state.annotation.status = 'IN_PROGRESS';
    state.annotation.human_confirmed = false;
    setSaveState(`REVIEWED failed: ${error.message}`);
  }
}

// ------------------------------------------------------------- queue nav
function queueStep(direction) {
  const queue = currentQueue();
  if (!queue.length) return;
  const pageQueue = queue.filter(item => item.page_key === state.page?.key);
  const pool = pageQueue.length ? pageQueue : queue;
  const ordered = pool;
  let index = state.queuePosition;
  if (direction > 0) index = Math.min(ordered.length - 1, index + 1);
  else index = Math.max(0, index - 1);
  if (pageQueue.length) state.queuePosition = index;
  const item = ordered[index];
  if (item.page_key !== state.page.key) { loadPage(item.page_key).then(() => focusQueueItem(item)); return; }
  focusQueueItem(item);
}

function nextHighRiskRelation() {
  const meta = pageMeta(state.page.key);
  if (!meta) return;
  const high = meta.edges.filter(edge => edge.risk === 'HIGH' || edge.risk === 'AMBIGUOUS');
  if (!high.length) return;
  const index = high.findIndex(edge => edge.relationship_id === state.currentEdgeId);
  const nextEdge = high[(index + 1) % high.length];
  state.currentEdgeId = nextEdge.relationship_id;
  setMode('OWNERSHIP_RISK_REVIEW');
  const row = document.querySelector(`[data-edge-id="${CSS.escape(state.currentEdgeId)}"]`);
  row?.scrollIntoView({ block: 'center' });
  setSaveState(`High-risk: ${state.currentEdgeId}`);
}

function focusQueueItem(item) {
  state.currentEdgeId = item.relationship_id;
  if (item.risk === 'AMBIGUOUS') setMode('AMBIGUOUS_REVIEW'); else setMode('OWNERSHIP_RISK_REVIEW');
  const row = document.querySelector(`[data-edge-id="${CSS.escape(item.relationship_id)}"]`);
  row?.scrollIntoView({ block: 'center' });
  setSaveState(`Queue ${item.risk}: ${item.relationship_id}`);
}

function nextTable() {
  const tables = state.annotation?.table_regions || [];
  if (!tables.length) return;
  const index = tables.findIndex(table => table.table_region_id === state.activeTableId);
  const nextTableItem = tables[(index + 1) % tables.length];
  state.activeTableId = nextTableItem.table_region_id;
  state.selectedTable = nextTableItem.table_region_id;
  setMode('TABLE_STRUCTURE_REVIEW');
  setSaveState(`Table: ${state.activeTableId}`);
}

// ------------------------------------------------------------ entity UIs
function renderBlocks() {
  $('block-list').replaceChildren(...state.annotation.blocks.map(block => {
    const row = document.createElement('div'); row.className = `entity-row ${state.selectedBlock === block.block_id ? 'selected' : ''}`;
    row.innerHTML = `<span>${block.reading_order}</span><strong>${block.block_type}</strong><span class="grow">${block.text || block.block_id}</span><span>${block.human_confirmed ? '✓' : 'suggestion'}</span>`;
    row.onclick = () => selectBlock(block.block_id); return row;
  }));
}

function selectBlock(id) {
  state.selectedBlock = id; const block = state.annotation.blocks.find(item => item.block_id === id); if (!block) return;
  $('block-id').value = block.block_id; $('block-type').value = block.block_type; $('block-order').value = block.reading_order;
  $('block-section').value = block.section_id || ''; $('block-table').value = block.table_region_id || ''; $('block-row').value = block.row_id || ''; $('block-column').value = block.column_id || ''; $('block-bbox').value = block.bbox.join(',');
  renderAll();
}

function renderTables() {
  $('table-list').replaceChildren(...state.annotation.table_regions.map(table => {
    const row = document.createElement('div'); row.className = `entity-row ${state.selectedTable === table.table_region_id ? 'selected' : ''}`;
    row.innerHTML = `<strong>${table.table_region_id}</strong><span class="grow">${table.bbox.join(', ')}</span><span>${table.human_confirmed ? '✓' : 'suggestion'}</span>`;
    row.onclick = () => selectTable(table.table_region_id); return row;
  }));
}

function selectTable(id) { state.selectedTable = id; state.activeTableId = id; renderAll(); }

function renderGroups() {
  const groups = [...state.annotation.rows.map(item => ({...item, kind: 'row', id: item.row_id})), ...state.annotation.columns.map(item => ({...item, kind: 'column', id: item.column_id}))];
  $('group-list').replaceChildren(...groups.map(group => {
    const row = document.createElement('div'); row.className = 'entity-row'; row.innerHTML = `<strong>${group.kind}</strong><span class="grow">${group.id}</span><button>×</button>`;
    row.querySelector('button').onclick = () => { state.annotation[group.kind === 'row' ? 'rows' : 'columns'] = state.annotation[group.kind === 'row' ? 'rows' : 'columns'].filter(item => item[`${group.kind}_id`] !== group.id); changed(); };
    return row;
  }));
}

function renderEdges() {
  $('edge-list').replaceChildren(...state.annotation.relationships.map(edge => {
    const row = document.createElement('div'); row.className = 'entity-row'; row.innerHTML = `<strong>${edge.relationship_type}</strong><span class="grow">${edge.source_id || '∅'} → ${edge.target_id}</span><span>${edge.human_confirmed ? '✓' : 'suggestion'}</span><button>×</button>`;
    row.querySelector('button').onclick = () => { state.annotation.relationships = state.annotation.relationships.filter(item => item.relationship_id !== edge.relationship_id); changed(); };
    return row;
  }));
}

function renderEntityIds() {
  const ids = [...state.annotation.blocks.map(item => item.block_id), ...state.annotation.table_regions.map(item => item.table_region_id), ...state.annotation.rows.map(item => item.row_id), ...state.annotation.columns.map(item => item.column_id), ...state.annotation.blocks.map(item => item.section_id).filter(Boolean)];
  $('entity-ids').replaceChildren(...[...new Set(ids)].map(id => new Option(id)));
}

function changed() {
  if (state.annotation.status === 'NOT_STARTED') { state.annotation.status = 'IN_PROGRESS'; $('page-status').value = 'IN_PROGRESS'; }
  state.dirty = true; setSaveState('Unsaved changes'); clearTimeout(state.saveTimer); state.saveTimer = setTimeout(saveAnnotation, 700); renderAll();
}

async function saveAnnotation() {
  if (!state.annotation || !state.dirty) return;
  setSaveState('Saving…');
  try {
    const result = await getJSON(`/api/save?key=${encodeURIComponent(state.page.key)}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(state.annotation)});
    state.dirty = false; state.annotation.audit = state.annotation.audit || {}; state.annotation.audit.validation = result.validation;
    $('validation').textContent = JSON.stringify(result.validation, null, 2); setSaveState(result.saved !== false && result.validation.valid ? 'Autosaved · schema valid' : 'Autosaved draft · validation issues');
  } catch (error) { setSaveState(`Save failed: ${error.message}`); }
}

function setSaveState(value) { $('save-state').textContent = value; }

function importPrediction() {
  const source = state.prediction.options[$('prediction-option').value]; if (!source) return;
  const clone = value => JSON.parse(JSON.stringify(value || []));
  const prior = {
    blocks: state.annotation.blocks, table_regions: state.annotation.table_regions,
    rows: state.annotation.rows, columns: state.annotation.columns, relationships: state.annotation.relationships,
  };
  state.annotation.blocks = clone(source.blocks).map(item => ({...item, human_confirmed: false}));
  state.annotation.table_regions = clone(source.table_regions).map(item => ({...item, human_confirmed: false}));
  state.annotation.rows = clone(source.rows).map(item => ({...item, human_confirmed: false}));
  state.annotation.columns = clone(source.columns).map(item => ({...item, human_confirmed: false}));
  state.annotation.cells = [];
  state.annotation.relationships = clone(source.relationships).map(item => ({...item, human_confirmed: false}));
  state.annotation.status = 'IN_PROGRESS'; state.annotation.human_confirmed = false;
  state.annotation.audit.prediction_imported = true;
  pushUndo({ label: 'import prediction', undo: () => Object.assign(state.annotation, prior) });
  logOperation('import_prediction', [state.prediction.prediction_key || $('prediction-option').value]);
  state.activeTableId = highestRiskTable(state.page.key);
  $('page-status').value = 'IN_PROGRESS'; $('page-confirmed').checked = false; changed();
}

function addDrawnEntity(kind, bbox) {
  const suffix = crypto.randomUUID().slice(0, 8);
  if (kind === 'block') {
    const item = {block_id: `${state.page.key}_block_${suffix}`, block_type: 'OTHER', bbox, reading_order: state.annotation.blocks.length, text: '', page_number: state.page.page_number, section_id: '', table_region_id: '', row_id: '', column_id: '', header_ids: [], human_confirmed: true};
    state.annotation.blocks.push(item); state.selectedBlock = item.block_id;
  } else {
    const item = {table_region_id: `${state.page.key}_table_${suffix}`, bbox, page_number: state.page.page_number, row_ids: [], column_ids: [], cell_ids: [], human_confirmed: true};
    state.annotation.table_regions.push(item); state.selectedTable = item.table_region_id;
  }
  changed();
}

function canvasPoint(event) {
  const svg = $('overlay'); const point = svg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY; return point.matrixTransform(svg.getScreenCTM().inverse());
}

$('overlay').onpointerdown = event => { if ($('draw-mode').value !== 'select') state.drawStart = canvasPoint(event); };
$('overlay').onpointerup = event => { if (!state.drawStart) return; const end = canvasPoint(event); const bbox = [Math.min(state.drawStart.x, end.x), Math.min(state.drawStart.y, end.y), Math.max(state.drawStart.x, end.x), Math.max(state.drawStart.y, end.y)].map(value => Math.round(value * 100) / 100); state.drawStart = null; addDrawnEntity($('draw-mode').value, bbox); $('draw-mode').value = 'select'; };

$('show-predictions').onchange = () => { $('prediction-option').disabled = !$('show-predictions').checked; $('import-prediction').disabled = !$('show-predictions').checked; renderOverlay(); };
$('prediction-option').onchange = renderOverlay;
$('import-prediction').onclick = importPrediction;
$('page-filter').oninput = renderPageList;
$('edge-filter').onchange = renderOwnership;
$('page-status').onchange = () => { state.annotation.status = $('page-status').value; changed(); };
$('page-confirmed').onchange = () => { state.annotation.human_confirmed = $('page-confirmed').checked; changed(); };
$('validate-page').onclick = async () => { await saveAnnotation(); const result = await getJSON(`/api/validate?key=${encodeURIComponent(state.page.key)}`); $('validation').textContent = JSON.stringify(result, null, 2); };
$('update-block').onclick = () => { const block = state.annotation.blocks.find(item => item.block_id === state.selectedBlock); if (!block) return; const values = $('block-bbox').value.split(',').map(Number); if (values.length !== 4 || values.some(Number.isNaN)) return alert('BBox requires four numbers.'); block.block_type = $('block-type').value; block.reading_order = Number($('block-order').value); block.section_id = $('block-section').value.trim(); block.table_region_id = $('block-table').value.trim(); block.row_id = $('block-row').value.trim(); block.column_id = $('block-column').value.trim(); block.bbox = values; block.human_confirmed = true; changed(); };
$('delete-block').onclick = () => { state.annotation.blocks = state.annotation.blocks.filter(item => item.block_id !== state.selectedBlock); state.selectedBlock = null; changed(); };
$('delete-table').onclick = () => { state.annotation.table_regions = state.annotation.table_regions.filter(item => item.table_region_id !== state.selectedTable); state.selectedTable = null; changed(); };
$('add-group').onclick = () => { const kind = $('group-type').value; const id = $('group-id').value.trim(); if (!id) return; const key = kind === 'row' ? 'rows' : 'columns'; const field = `${kind}_id`; if (state.annotation[key].some(item => item[field] === id)) return; state.annotation[key].push({[field]: id, bbox: [0,0,state.page.width,state.page.height], page_number: state.page.page_number, cell_ids: [], human_confirmed: true}); $('group-id').value = ''; changed(); };
$('add-edge').onclick = () => { const type = $('edge-type').value; const source = $('edge-source').value.trim(); const target = $('edge-target').value.trim(); if (!target || (type !== 'AMBIGUOUS' && !source)) return; const id = `${state.page.key}_rel_${crypto.randomUUID().slice(0,8)}`; state.annotation.relationships.push({relationship_id: id, source_id: source, target_id: target, relationship_type: type, page_number: state.page.page_number, human_confirmed: true}); changed(); };
$('open-highest-risk-table').onclick = () => { state.activeTableId = highestRiskTable(state.page.key); setMode('TABLE_STRUCTURE_REVIEW'); };
$('goto-next-high-risk').onclick = nextHighRiskRelation;
$('queue-next').onclick = () => queueStep(1);
$('queue-prev').onclick = () => queueStep(-1);
document.querySelectorAll('.mode-tab').forEach(tab => tab.onclick = () => setMode(tab.dataset.mode));

document.addEventListener('keydown', event => {
  if (event.ctrlKey || event.metaKey) {
    if (event.key.toLowerCase() === 'z') { event.preventDefault(); undoLast(); }
    return;
  }
  const tag = document.activeElement?.tagName;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
  if (event.key === 'a' || event.key === 'A') { if (state.currentEdgeId) acceptEdge(state.currentEdgeId); }
  else if (event.key === 'r' || event.key === 'R') { if (state.currentEdgeId) rejectEdge(state.currentEdgeId); }
  else if (event.key === 'm' || event.key === 'M') { if (state.currentEdgeId) markAmbiguousEdge(state.currentEdgeId); }
  else if (event.key === 'n' || event.key === 'N') queueStep(1);
  else if (event.key === 'p' || event.key === 'P') queueStep(-1);
  else if (event.key === 't' || event.key === 'T') nextTable();
  else if (event.key === 'h' || event.key === 'H') nextHighRiskRelation();
  else if (event.key.toLowerCase() === 'u') undoLast();
});

window.addEventListener('beforeunload', event => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });

(async () => {
  await loadManifest();
  await loadReviewData();
  await loadDashboard();
  setMode('PAGE_OVERVIEW');
  if (state.manifest.pages.length) await loadPage(state.manifest.pages[0].key);
})().catch(error => setSaveState(`Load failed: ${error.message}`));
