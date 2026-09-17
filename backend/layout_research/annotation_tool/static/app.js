const state = { manifest: null, page: null, annotation: null, prediction: null, selectedBlock: null, selectedTable: null, dirty: false, saveTimer: null, drawStart: null };
const $ = id => document.getElementById(id);

async function getJSON(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

function pageStatus(key) {
  return state.manifest?.progressByKey?.[key] || 'NOT_STARTED';
}

async function loadManifest() {
  state.manifest = await getJSON('/api/manifest');
  state.manifest.progressByKey = {};
  $('progress').textContent = `${state.manifest.progress.counts.REVIEWED}/${state.manifest.progress.total} reviewed · FINAL_GATE unconsumed`;
  renderPageList();
  if (state.manifest.pages.length) await loadPage(state.manifest.pages[0].key);
}

function renderPageList() {
  const filter = $('page-filter').value.toLowerCase();
  $('page-list').replaceChildren(...state.manifest.pages.filter(page => `${page.key} ${page.slices.join(' ')}`.toLowerCase().includes(filter)).map(page => {
    const button = document.createElement('button');
    button.className = `page-button ${state.page?.key === page.key ? 'active' : ''}`;
    button.innerHTML = `${page.doc_id} · page ${page.page_number}<small>${page.slices.join(' · ')}</small>`;
    button.onclick = () => loadPage(page.key);
    return button;
  }));
}

async function loadPage(key) {
  if (state.dirty) await saveAnnotation();
  const payload = await getJSON(`/api/page?key=${encodeURIComponent(key)}`);
  state.page = payload.page; state.annotation = payload.annotation; state.prediction = payload.prediction;
  state.selectedBlock = null; state.selectedTable = null; state.dirty = false;
  $('page-title').textContent = `${state.page.doc_id} · page ${state.page.page_number}`;
  $('slice-tags').innerHTML = state.page.slices.map(value => `<span class="tag">${value}</span>`).join('');
  $('page-preview').src = `/workspace/${state.page.preview}`;
  $('page-status').value = state.annotation.status;
  $('page-confirmed').checked = state.annotation.human_confirmed;
  $('show-predictions').checked = false;
  $('prediction-option').replaceChildren(...Object.keys(state.prediction.options).map(key => new Option(state.manifest.prediction_labels[key], key)));
  $('prediction-option').disabled = true; $('import-prediction').disabled = true;
  $('validation').textContent = JSON.stringify(state.annotation.audit?.validation || {}, null, 2);
  renderAll(); renderPageList(); setSaveState('Loaded · predictions hidden');
}

function renderAll() { renderOverlay(); renderBlocks(); renderTables(); renderGroups(); renderEdges(); renderEntityIds(); }

function renderOverlay() {
  if (!state.annotation) return;
  const svg = $('overlay');
  svg.setAttribute('viewBox', `0 0 ${state.page.width} ${state.page.height}`);
  const nodes = [];
  const rect = (item, kind, prediction = false) => {
    const bbox = item.bbox; if (!bbox || bbox.length !== 4) return;
    const node = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    node.setAttribute('x', bbox[0]); node.setAttribute('y', bbox[1]); node.setAttribute('width', bbox[2] - bbox[0]); node.setAttribute('height', bbox[3] - bbox[1]);
    node.setAttribute('class', `box ${kind} ${prediction ? 'prediction' : ''} ${(item.block_id === state.selectedBlock || item.table_region_id === state.selectedTable) ? 'selected' : ''}`);
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

function selectTable(id) { state.selectedTable = id; renderAll(); }

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
    $('validation').textContent = JSON.stringify(result.validation, null, 2); setSaveState(result.validation.valid ? 'Autosaved · schema valid' : 'Autosaved draft · validation issues');
  } catch (error) { setSaveState(`Save failed: ${error.message}`); }
}

function setSaveState(value) { $('save-state').textContent = value; }

function importPrediction() {
  const source = state.prediction.options[$('prediction-option').value]; if (!source) return;
  const clone = value => JSON.parse(JSON.stringify(value || []));
  state.annotation.blocks = clone(source.blocks).map(item => ({...item, human_confirmed: false}));
  state.annotation.table_regions = clone(source.table_regions).map(item => ({...item, human_confirmed: false}));
  state.annotation.rows = clone(source.rows).map(item => ({...item, human_confirmed: false}));
  state.annotation.columns = clone(source.columns).map(item => ({...item, human_confirmed: false}));
  state.annotation.cells = [];
  state.annotation.relationships = clone(source.relationships).map(item => ({...item, human_confirmed: false}));
  state.annotation.status = 'IN_PROGRESS'; state.annotation.human_confirmed = false; state.annotation.audit.prediction_imported = true;
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
$('page-status').onchange = () => { state.annotation.status = $('page-status').value; changed(); };
$('page-confirmed').onchange = () => { state.annotation.human_confirmed = $('page-confirmed').checked; changed(); };
$('validate-page').onclick = async () => { await saveAnnotation(); const result = await getJSON(`/api/validate?key=${encodeURIComponent(state.page.key)}`); $('validation').textContent = JSON.stringify(result, null, 2); };
$('confirm-all').onclick = () => { if (!confirm('Confirm that every current block, table, group and relationship was manually checked?')) return; ['blocks','table_regions','rows','columns','cells','relationships','continuations'].forEach(name => state.annotation[name].forEach(item => item.human_confirmed = true)); state.annotation.human_confirmed = true; state.annotation.status = 'REVIEWED'; state.annotation.audit.review_count = (state.annotation.audit.review_count || 0) + 1; $('page-confirmed').checked = true; $('page-status').value = 'REVIEWED'; changed(); };
$('update-block').onclick = () => { const block = state.annotation.blocks.find(item => item.block_id === state.selectedBlock); if (!block) return; const values = $('block-bbox').value.split(',').map(Number); if (values.length !== 4 || values.some(Number.isNaN)) return alert('BBox requires four numbers.'); block.block_type = $('block-type').value; block.reading_order = Number($('block-order').value); block.section_id = $('block-section').value.trim(); block.table_region_id = $('block-table').value.trim(); block.row_id = $('block-row').value.trim(); block.column_id = $('block-column').value.trim(); block.bbox = values; block.human_confirmed = true; changed(); };
$('delete-block').onclick = () => { state.annotation.blocks = state.annotation.blocks.filter(item => item.block_id !== state.selectedBlock); state.selectedBlock = null; changed(); };
$('delete-table').onclick = () => { state.annotation.table_regions = state.annotation.table_regions.filter(item => item.table_region_id !== state.selectedTable); state.selectedTable = null; changed(); };
$('add-group').onclick = () => { const kind = $('group-type').value; const id = $('group-id').value.trim(); if (!id) return; const key = kind === 'row' ? 'rows' : 'columns'; const field = `${kind}_id`; if (state.annotation[key].some(item => item[field] === id)) return; state.annotation[key].push({[field]: id, bbox: [0,0,state.page.width,state.page.height], page_number: state.page.page_number, cell_ids: [], human_confirmed: true}); $('group-id').value = ''; changed(); };
$('add-edge').onclick = () => { const type = $('edge-type').value; const source = $('edge-source').value.trim(); const target = $('edge-target').value.trim(); if (!target || (type !== 'AMBIGUOUS' && !source)) return; state.annotation.relationships.push({relationship_id: `${state.page.key}_rel_${crypto.randomUUID().slice(0,8)}`, source_id: source, target_id: target, relationship_type: type, page_number: state.page.page_number, human_confirmed: true}); changed(); };
window.addEventListener('beforeunload', event => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });

loadManifest().catch(error => setSaveState(`Load failed: ${error.message}`));
