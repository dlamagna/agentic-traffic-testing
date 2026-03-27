/**
 * AgentVerse JSON Viewer
 *
 * Drop a response.json file from a run and visualise it using the same
 * workflow UI as the live runner — no backend required.
 */

import { UIState } from './ui-state.js';
import { getDefaultEndpoint } from './utils.js';

class AgentVerseViewer {
  constructor() {
    this.elements = {
      workflowPanel:        document.querySelector('.workflow-panel'),
      statusIndicator:      document.getElementById('statusIndicator'),
      statusText:           document.getElementById('statusText'),
      statusDetail:         document.getElementById('statusDetail'),
      statusTime:           document.getElementById('statusTime'),
      progressFill:         document.getElementById('progressFill'),
      finalOutputContainer: document.getElementById('finalOutputContainer'),
      finalOutput:          document.getElementById('finalOutput'),
      finalOutputRaw:       document.getElementById('finalOutputRaw'),
      rawJson:              document.getElementById('rawJson'),
      iterationHistory:     document.getElementById('iterationHistory'),
      llmRequestCount:      document.getElementById('llmRequestCount'),
      liveBadge:            document.getElementById('liveBadge'),
      // not used by UIState but kept so resetUI() has no missing refs
      runBtn:               document.getElementById('runBtn'),
    };

    this.uiState = new UIState(this.elements);
    this.dropZone = document.getElementById('dropZone');
    this.fileInput = document.getElementById('fileInput');
    this.jsonPasteInput = document.getElementById('jsonPasteInput');
    this.loadPastedJsonBtn = document.getElementById('loadPastedJsonBtn');
    this.clearPastedJsonBtn = document.getElementById('clearPastedJsonBtn');

    this._bindEvents();
    this._loadFromUrlIfPresent();
  }

  _bindEvents() {
    // File picker
    this.fileInput.addEventListener('change', (e) => {
      const file = e.target.files[0];
      if (file) this._loadFile(file);
    });

    // Drag-and-drop
    const dz = this.dropZone;
    dz.addEventListener('dragover', (e) => {
      e.preventDefault();
      dz.classList.add('drag-over');
    });
    dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
    dz.addEventListener('drop', (e) => {
      e.preventDefault();
      dz.classList.remove('drag-over');
      const file = e.dataTransfer.files[0];
      if (file) this._loadFile(file);
    });

    // Click on drop zone opens file picker
    dz.addEventListener('click', () => this.fileInput.click());

    // Pasted JSON input
    if (this.loadPastedJsonBtn) {
      this.loadPastedJsonBtn.addEventListener('click', () => this._loadFromPastedJson());
    }
    if (this.clearPastedJsonBtn && this.jsonPasteInput) {
      this.clearPastedJsonBtn.addEventListener('click', () => {
        this.jsonPasteInput.value = '';
        this.jsonPasteInput.focus();
      });
    }
    if (this.jsonPasteInput) {
      this.jsonPasteInput.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
          e.preventDefault();
          this._loadFromPastedJson();
        }
      });
    }
  }

  _loadFile(file) {
    this._setLoadingState(`Loading file: ${file.name}`);
    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        const data = JSON.parse(e.target.result);
        this._render(data, file.name);
      } catch (err) {
        this._showError(`Could not parse JSON: ${err.message}`);
      }
    };
    reader.readAsText(file);
  }

  _loadFromPastedJson() {
    const raw = this.jsonPasteInput?.value || '';
    if (!raw.trim()) {
      this._showError('Paste JSON first.');
      return;
    }
    this._setLoadingState('Loading pasted JSON');
    try {
      const data = JSON.parse(raw);
      this._render(data, 'pasted-json');
    } catch (err) {
      this._showError(`Could not parse pasted JSON: ${err.message}`);
    }
  }

  async _loadFromUrlIfPresent() {
    const url = new URL(window.location.href);
    const taskId = (url.searchParams.get('task_id') || '').trim();
    const endpointParam = (url.searchParams.get('endpoint') || '').trim();
    const jsonPath = url.searchParams.get('json');

    // Priority: task_id (server-backed stable lookup) > json (static file fetch)
    if (taskId) {
      await this._loadFromTaskId(taskId, endpointParam);
      return;
    }
    if (!jsonPath) return;

    this._setLoadingState(`Loading JSON from URL: ${jsonPath}`);
    try {
      const response = await fetch(jsonPath, { cache: 'no-store' });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status} ${response.statusText}`);
      }
      const data = await response.json();
      this._render(data, jsonPath.split('/').pop() || 'response.json');
    } catch (err) {
      this._showError(`Could not load URL JSON (${jsonPath}): ${err.message}`);
    }
  }

  async _loadFromTaskId(taskId, endpointParam = '') {
    const baseEndpoint = endpointParam || getDefaultEndpoint();
    const base = baseEndpoint.replace(/\/+$/, '');
    const url = `${base}/${encodeURIComponent(taskId)}`;

    this._setLoadingState(`Loading task_id ${taskId} from ${base}`);
    try {
      const resp = await fetch(url, { method: 'GET' });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }

      const record = await resp.json();
      const resultData = record.result || record || {};
      this._render(resultData, `task_id:${taskId}`);
    } catch (err) {
      this._showError(`Could not load task_id ${taskId}: ${err.message}`);
    }
  }

  _setLoadingState(message) {
    this.elements.statusIndicator.className = 'status-indicator running';
    this.elements.statusText.textContent = 'Loading...';
    if (this.elements.statusDetail) this.elements.statusDetail.textContent = message;
  }

  _render(data, filename) {
    this.uiState.resetUI();

    // Show file info in the drop zone
    const label = document.getElementById('dropZoneLabel');
    if (label) {
      const taskPreview = (data.original_task || data.task || '').substring(0, 80);
      label.innerHTML = `
        <strong>${this._esc(filename)}</strong><br>
        <span style="font-size:13px;color:var(--text-secondary)">${this._esc(taskPreview)}${(data.original_task || '').length > 80 ? '…' : ''}</span>
      `;
    }

    // Populate task display
    const taskDisplay = document.getElementById('taskDisplay');
    if (taskDisplay && (data.original_task || data.task)) {
      taskDisplay.textContent = data.original_task || data.task;
      taskDisplay.closest('.task-display-card').style.display = 'block';
    }

    // Mark status complete
    this.elements.statusIndicator.className = 'status-indicator complete';
    this.elements.statusText.textContent = 'Loaded';
    if (this.elements.statusDetail) this.elements.statusDetail.textContent = filename;
    if (data.duration_seconds != null) {
      this.elements.statusTime.textContent = `${data.duration_seconds.toFixed(1)}s`;
    }

    this.uiState.updateWorkflowUI(data);
    this.setFinalOutputView('formatted');

    // Scroll to workflow
    document.getElementById('workflowSection').scrollIntoView({ behavior: 'smooth' });
  }

  _showError(msg) {
    this.elements.statusIndicator.className = 'status-indicator error';
    this.elements.statusText.textContent = 'Error';
    if (this.elements.statusDetail) this.elements.statusDetail.textContent = msg;
  }

  _esc(text) {
    const d = document.createElement('div');
    d.textContent = text || '';
    return d.innerHTML;
  }

  // --- methods called by inline onclick handlers in the HTML ---

  toggleStage(stageId)    { this.uiState.toggleStage(stageId); }

  toggleDetailedFlow() {
    const content = document.getElementById('detailedFlowContent');
    const header  = document.querySelector('#detailedFlowSection .collapsible-header');
    content.style.display = content.style.display === 'none' ? 'block' : 'none';
    header.classList.toggle('expanded', content.style.display !== 'none');
  }

  toggleRawJson() {
    const el = this.elements.rawJson;
    const toggle = document.getElementById('rawToggleText');
    if (!el || !toggle) return;
    el.classList.toggle('visible');
    toggle.textContent = el.classList.contains('visible') ? 'Hide' : 'Show';
  }

  setFlowView(view) {
    const graphView = document.getElementById('flowGraphView');
    const tableView = document.getElementById('flowTableView');
    const graphBtn  = document.getElementById('flowViewGraphBtn');
    const tableBtn  = document.getElementById('flowViewTableBtn');
    if (!graphView || !tableView || !graphBtn || !tableBtn) return;
    if (view === 'table') {
      tableView.style.display = 'block';
      graphView.style.display = 'none';
      tableBtn.classList.add('active');
      graphBtn.classList.remove('active');
    } else {
      graphView.style.display = 'block';
      tableView.style.display = 'none';
      graphBtn.classList.add('active');
      tableBtn.classList.remove('active');
    }
  }

  toggleFlowRow(seq) {
    const detailRow = document.getElementById('flow-detail-' + seq);
    if (!detailRow) return;
    const parentRow = detailRow.previousElementSibling;
    if (detailRow.style.display === 'none') {
      detailRow.style.display = 'table-row';
      if (parentRow) parentRow.classList.add('expanded');
    } else {
      detailRow.style.display = 'none';
      if (parentRow) parentRow.classList.remove('expanded');
    }
  }

  setFinalOutputView(view) {
    const formattedEl = this.elements.finalOutput;
    const rawEl       = this.elements.finalOutputRaw;
    if (!formattedEl || !rawEl) return;
    document.querySelectorAll('.final-output-tab').forEach(t =>
      t.classList.toggle('active', t.dataset.view === view));
    formattedEl.style.display = view === 'raw' ? 'none' : 'block';
    rawEl.style.display       = view === 'raw' ? 'block' : 'none';
  }

  copyFinalOutput() {
    const text = this.uiState?.currentData?.final_output || this.elements.finalOutput?.textContent || '';
    if (!text.trim()) { alert('No final output to copy.'); return; }
    this._copyText(text);
    alert(`Copied ${text.length} characters to clipboard.`);
  }

  copyRawJson() {
    const text = this.elements.rawJson?.textContent || '';
    if (!text.trim()) { alert('No raw JSON to copy.'); return; }
    this._copyText(text);
    alert(`Copied ${text.length} characters of raw JSON to clipboard.`);
  }

  _copyText(text) {
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).catch(() => this._fallbackCopy(text));
    } else {
      this._fallbackCopy(text);
    }
  }

  _fallbackCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (_) {}
    document.body.removeChild(ta);
  }

  selectIteration(idx) {
    document.querySelectorAll('.iteration-tab').forEach((t, i) =>
      t.classList.toggle('active', i === idx));
    document.querySelectorAll('.iteration-details').forEach((d, i) =>
      d.classList.toggle('active', i === idx));
    document.querySelectorAll('.score-bar-container').forEach((b, i) =>
      b.classList.toggle('selected', i === idx));
  }
}

document.addEventListener('DOMContentLoaded', () => {
  window.agentverse = new AgentVerseViewer();

  // Tooltip for flow graph
  document.body.addEventListener('mouseover', (e) => {
    const el  = e.target.closest('.flow-graph-hoverable');
    const tip = document.getElementById('flowGraphTooltip');
    if (!tip) return;
    if (el?.dataset.tooltip) {
      tip.textContent = el.dataset.tooltip;
      tip.setAttribute('aria-hidden', 'false');
      tip.style.left = (e.pageX + 12) + 'px';
      tip.style.top  = (e.pageY + 12) + 'px';
    } else {
      tip.setAttribute('aria-hidden', 'true');
    }
  });
  document.body.addEventListener('mousemove', (e) => {
    const tip = document.getElementById('flowGraphTooltip');
    if (tip && tip.getAttribute('aria-hidden') === 'false') {
      tip.style.left = (e.pageX + 12) + 'px';
      tip.style.top  = (e.pageY + 12) + 'px';
    }
  });
});
