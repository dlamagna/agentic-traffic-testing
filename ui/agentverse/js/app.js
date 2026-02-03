/**
 * Main Application
 */

import { CONFIG, EXAMPLE_TASKS } from './config.js';
import { getDefaultEndpoint } from './utils.js';
import { UIState } from './ui-state.js';
import { StreamingHandler } from './streaming.js';

class AgentVerseApp {
  constructor() {
    // Get DOM elements
    this.elements = {
      taskEl: document.getElementById('task'),
      maxIterationsEl: document.getElementById('maxIterations'),
      scoreThresholdEl: document.getElementById('scoreThreshold'),
      endpointEl: document.getElementById('endpoint'),
      runBtn: document.getElementById('runBtn'),
      cancelBtn: document.getElementById('cancelBtn'),
      clearBtn: document.getElementById('clearBtn'),
      workflowPanel: document.querySelector('.workflow-panel'),
      statusIndicator: document.getElementById('statusIndicator'),
      statusText: document.getElementById('statusText'),
      statusTime: document.getElementById('statusTime'),
      progressFill: document.getElementById('progressFill'),
      finalOutputContainer: document.getElementById('finalOutputContainer'),
      finalOutput: document.getElementById('finalOutput'),
      finalOutputRaw: document.getElementById('finalOutputRaw'),
      rawJson: document.getElementById('rawJson'),
      iterationHistory: document.getElementById('iterationHistory'),
      requestHistory: document.getElementById('requestHistory'),
      llmRequestCount: document.getElementById('llmRequestCount'),
      liveBadge: document.getElementById('liveBadge'),
    };

    // Initialize state
    this.uiState = new UIState(this.elements);
    this.streamingHandler = new StreamingHandler(this.uiState);

    // Bind methods
    this.runWorkflow = this.runWorkflow.bind(this);
    this.clearAll = this.clearAll.bind(this);
    this.copyFinalOutput = this.copyFinalOutput.bind(this);
    this.loadExample = this.loadExample.bind(this);
    this.toggleStage = this.toggleStage.bind(this);
    this.toggleDetailedFlow = this.toggleDetailedFlow.bind(this);
    this.toggleRawJson = this.toggleRawJson.bind(this);
    this.setFlowView = this.setFlowView.bind(this);
    this.toggleFlowRow = this.toggleFlowRow.bind(this);
    this.selectIteration = this.selectIteration.bind(this);
    this.clearRequestHistory = this.clearRequestHistory.bind(this);
    this.loadRequestFromHistory = this.loadRequestFromHistory.bind(this);
    this.saveRequestToHistory = this.saveRequestToHistory.bind(this);
    this.getRequestHistory = this.getRequestHistory.bind(this);
    this.loadRequestHistory = this.loadRequestHistory.bind(this);

    // Initialize
    this.init();
  }

  init() {
    // Set default endpoint
    this.elements.endpointEl.value = getDefaultEndpoint();

    // Add event listeners
    this.elements.runBtn.addEventListener('click', this.runWorkflow);
    this.elements.clearBtn.addEventListener('click', this.clearAll);

    // Flow graph tooltip via event delegation (graph is dynamically rendered)
    document.body.addEventListener('mouseover', (e) => {
      const el = e.target.closest('.flow-graph-hoverable');
      const tip = document.getElementById('flowGraphTooltip');
      if (!tip) return;
      if (el && el.dataset.tooltip) {
        tip.textContent = el.dataset.tooltip;
        tip.setAttribute('aria-hidden', 'false');
        tip.style.left = (e.pageX + 12) + 'px';
        tip.style.top = (e.pageY + 12) + 'px';
      } else {
        tip.setAttribute('aria-hidden', 'true');
      }
    });
    document.body.addEventListener('mousemove', (e) => {
      const tip = document.getElementById('flowGraphTooltip');
      if (tip && tip.getAttribute('aria-hidden') === 'false') {
        tip.style.left = (e.pageX + 12) + 'px';
        tip.style.top = (e.pageY + 12) + 'px';
      }
    });
    
    // Keyboard shortcut
    this.elements.taskEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        this.runWorkflow();
      }
    });
    
    // Load and display request history
    this.loadRequestHistory();
  }

  /**
   * Load example task
   */
  loadExample(type) {
    if (EXAMPLE_TASKS[type]) {
      this.elements.taskEl.value = EXAMPLE_TASKS[type];
    }
  }

  /**
   * Toggle stage expansion
   */
  toggleStage(stageId) {
    this.uiState.toggleStage(stageId);
  }

  /**
   * Toggle detailed flow section
   */
  toggleDetailedFlow() {
    const content = document.getElementById('detailedFlowContent');
    const header = document.querySelector('#detailedFlowSection .collapsible-header');
    content.style.display = content.style.display === 'none' ? 'block' : 'none';
    header.classList.toggle('expanded', content.style.display !== 'none');
  }

  /**
   * Toggle raw JSON
   */
  toggleRawJson() {
    const el = this.elements.rawJson;
    const toggle = document.getElementById('rawToggleText');
    if (!el || !toggle) return;
    el.classList.toggle('visible');
    toggle.textContent = el.classList.contains('visible') ? 'Hide' : 'Show';
  }

  /**
   * Toggle between graph and table views
   */
  setFlowView(view) {
    const graphView = document.getElementById('flowGraphView');
    const tableView = document.getElementById('flowTableView');
    const graphBtn = document.getElementById('flowViewGraphBtn');
    const tableBtn = document.getElementById('flowViewTableBtn');
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

  /**
   * Toggle flow row expansion
   */
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

  /**
   * Select an iteration to view details
   */
  selectIteration(iterationIndex) {
    // Update tabs
    const tabs = document.querySelectorAll('.iteration-tab');
    tabs.forEach((tab, idx) => {
      if (idx === iterationIndex) {
        tab.classList.add('active');
      } else {
        tab.classList.remove('active');
      }
    });
    
    // Update details panels
    const details = document.querySelectorAll('.iteration-details');
    details.forEach((detail, idx) => {
      if (idx === iterationIndex) {
        detail.classList.add('active');
      } else {
        detail.classList.remove('active');
      }
    });
    
    // Highlight score bar
    const scoreBars = document.querySelectorAll('.score-bar-container');
    scoreBars.forEach((bar, idx) => {
      if (idx === iterationIndex) {
        bar.classList.add('selected');
      } else {
        bar.classList.remove('selected');
      }
    });
  }

  /**
   * Save request to history
   */
  saveRequestToHistory(task, endpoint, maxIterations, resultData, scoreThreshold = 70) {
    const history = this.getRequestHistory();
    const requestEntry = {
      id: Date.now().toString(),
      timestamp: new Date().toISOString(),
      task: task,
      endpoint: endpoint,
      maxIterations: maxIterations,
      scoreThreshold: scoreThreshold,
      result: {
        finalScore: resultData?.evaluation?.score || resultData?.stages?.evaluation?.score || 0,
        goalAchieved: resultData?.evaluation?.goal_achieved || resultData?.stages?.evaluation?.goal_achieved || false,
        iterationCount: resultData?.iteration_history?.length || 0,
        duration: resultData?.duration_seconds || 0,
      },
      // Store summary data for quick display
      summary: {
        experts: resultData?.stages?.recruitment?.experts?.map(e => e.role).join(', ') || 
                resultData?.iteration_history?.[0]?.recruitment?.experts?.join(', ') || 'N/A',
        finalOutput: resultData?.final_output ? resultData.final_output.substring(0, 100) + '...' : null,
      }
    };
    
    // Add to beginning of history (most recent first)
    history.unshift(requestEntry);
    
    // Keep only last 50 requests
    if (history.length > 50) {
      history.splice(50);
    }
    
    try {
      localStorage.setItem('agentverse_request_history', JSON.stringify(history));
      console.log('[AgentVerse] Saved to localStorage, history length:', history.length);
    } catch (e) {
      console.warn('[AgentVerse] Could not save request history:', e);
    }
    console.log('[AgentVerse] Request history container:', this.elements.requestHistory);
    if (this.elements.requestHistory) {
      try {
        this.loadRequestHistory();
        console.log('[AgentVerse] loadRequestHistory() completed');
      } catch (e) {
        console.error('[AgentVerse] Failed to load request history:', e);
      }
    } else {
      console.warn('[AgentVerse] Request history container not found');
    }
  }

  /**
   * Get request history from localStorage
   */
  getRequestHistory() {
    try {
      const stored = localStorage.getItem('agentverse_request_history');
      return stored ? JSON.parse(stored) : [];
    } catch (e) {
      console.error('Error loading request history:', e);
      return [];
    }
  }

  /**
   * Load and display request history
   */
  loadRequestHistory() {
    const history = this.getRequestHistory();
    const container = document.getElementById('requestHistory') || this.elements.requestHistory;
    
    if (!container) return;
    
    if (!history || history.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">📋</div>
          <p>Previous requests will appear here</p>
        </div>
      `;
      return;
    }
    
    let html = '<div class="request-history-list">';
    history.forEach((entry, idx) => {
      const date = new Date(entry.timestamp);
      const dateStr = date.toLocaleDateString() + ' ' + date.toLocaleTimeString();
      const isCancelled = entry.result?.cancelled === true;
      const score = entry.result?.finalScore ?? 0;
      const scoreColor = isCancelled ? 'var(--text-secondary)' : (score >= 70 ? 'var(--success)' : score >= 40 ? 'var(--warning)' : 'var(--error)');
      const goalIcon = isCancelled ? '—' : (entry.result?.goalAchieved ? '✓' : '✗');
      const statusLabel = isCancelled ? 'Cancelled' : `${score}/100`;
      const itemClass = isCancelled ? 'request-history-item request-history-item--cancelled' : 'request-history-item';

      const taskPreview = (entry.task || '').substring(0, 60);
      const taskFull = entry.task || '';
      html += `
        <div class="${itemClass}" onclick="window.agentverse.loadRequestFromHistory('${entry.id}')">
          <div class="request-history-header">
            <div class="request-history-title">${this.escapeHtml(taskPreview)}${taskFull.length > 60 ? '...' : ''}</div>
            <div class="request-history-meta">
              <span class="request-history-score" style="color: ${scoreColor}">${statusLabel}</span>
              <span class="request-history-goal">${goalIcon}</span>
            </div>
          </div>
          <div class="request-history-details">
            <div class="request-history-info">
              <span>${this.escapeHtml(dateStr)}</span>
              ${!isCancelled ? `<span>•</span><span>${entry.result?.iterationCount || 0} iteration${entry.result?.iterationCount !== 1 ? 's' : ''}</span>` : ''}
              ${!isCancelled && entry.result?.duration ? `<span>•</span><span>${entry.result.duration.toFixed(1)}s</span>` : ''}
            </div>
            ${entry.summary?.experts ? `<div class="request-history-experts">Experts: ${this.escapeHtml(entry.summary.experts)}</div>` : ''}
          </div>
        </div>
      `;
    });
    html += '</div>';
    
    container.innerHTML = html;
  }

  /**
   * Load a request from history
   */
  loadRequestFromHistory(requestId) {
    const history = this.getRequestHistory();
    const entry = history.find(h => h.id === requestId);
    
    if (!entry) {
      alert('Request not found in history');
      return;
    }
    
    // Load the request into the form
    this.elements.taskEl.value = entry.task;
    this.elements.endpointEl.value = entry.endpoint;
    this.elements.maxIterationsEl.value = entry.maxIterations.toString();
    if (this.elements.scoreThresholdEl && entry.scoreThreshold != null) {
      this.elements.scoreThresholdEl.value = entry.scoreThreshold;
    }
    
    // Scroll to top
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  /**
   * Clear request history
   */
  clearRequestHistory() {
    if (confirm('Are you sure you want to clear all request history?')) {
      localStorage.removeItem('agentverse_request_history');
      this.loadRequestHistory();
    }
  }

  /**
   * Escape HTML helper
   */
  escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  /**
   * Copy final output to clipboard
   */
  copyFinalOutput() {
    // Try to get from the stored data first (full text), fallback to DOM
    const storedFinalOutput = this.uiState?.currentData?.final_output;
    const domText = this.elements.finalOutput?.textContent || '';
    const text = storedFinalOutput || domText;
    
    console.log('[AgentVerse] Copy: stored length:', storedFinalOutput?.length, 'DOM length:', domText.length);
    
    if (!text.trim()) {
      alert('No final output to copy yet.');
      return;
    }

    const doFallbackCopy = () => {
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      try {
        document.execCommand('copy');
      } catch (e) {
        console.error('Fallback copy failed:', e);
      }
      document.body.removeChild(textarea);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(err => {
        console.error('Clipboard API copy failed, falling back:', err);
        doFallbackCopy();
      });
    } else {
      doFallbackCopy();
    }

    alert(`Copied ${text.length} characters to clipboard.`);
  }

  /**
   * Switch between Formatted and Raw final output view
   */
  setFinalOutputView(view) {
    const formattedEl = this.elements.finalOutput;
    const rawEl = this.elements.finalOutputRaw;
    if (!formattedEl || !rawEl) return;

    const tabs = document.querySelectorAll('.final-output-tab');
    tabs?.forEach(t => t.classList.toggle('active', t.dataset.view === view));

    if (view === 'raw') {
      formattedEl.style.display = 'none';
      rawEl.style.display = 'block';
    } else {
      formattedEl.style.display = 'block';
      rawEl.style.display = 'none';
    }
  }

  /**
   * Run workflow
   */
  async runWorkflow() {
    const task = this.elements.taskEl.value.trim();
    if (!task) {
      alert('Please enter a task description.');
      return;
    }

    const endpoint = this.elements.endpointEl.value.trim();
    if (!endpoint) {
      alert('Please enter the Agent A endpoint.');
      return;
    }

    const maxIterations = parseInt(this.elements.maxIterationsEl.value, 10);
    const scoreThreshold = parseInt(this.elements.scoreThresholdEl?.value ?? 70, 10) || 70;

    // Store the request parameters before running (so complete/cancel handler can save to history)
    this.currentRequest = { task, endpoint, maxIterations, scoreThreshold };

    this.currentAbortController = new AbortController();
    this.showCancelButton();

    const onComplete = (resultData) => {
      this.saveRequestToHistory(task, endpoint, maxIterations, resultData, scoreThreshold);
    };
    await this.streamingHandler.runWorkflowStreaming(
      task,
      endpoint,
      maxIterations,
      scoreThreshold,
      this.currentAbortController.signal,
      () => this.onRequestCancelled(),
      onComplete
    );
  }

  /**
   * Show Cancel request button (while a request is running)
   */
  showCancelButton() {
    if (this.elements.cancelBtn) this.elements.cancelBtn.style.display = 'inline-block';
  }

  /**
   * Hide Cancel request button
   */
  hideCancelButton() {
    if (this.elements.cancelBtn) this.elements.cancelBtn.style.display = 'none';
  }

  /**
   * Cancel the current request (abort fetch stream and free Run button)
   */
  cancelRequest() {
    if (this.currentAbortController) {
      this.currentAbortController.abort();
    }
  }

  /**
   * Called when the user cancels the request (after stream is aborted)
   */
  onRequestCancelled() {
    this.hideCancelButton();
    this.uiState.stopTimer(false);
    this.uiState.elements.liveBadge.style.display = 'none';
    this.uiState.elements.statusText.textContent = 'Cancelled';
    this.uiState.elements.runBtn.disabled = false;
    if (this.elements.workflowPanel) this.elements.workflowPanel.classList.add('workflow-panel--cancelled');
    if (window.agentverse && this.currentRequest) {
      this.saveRequestToHistory(
        this.currentRequest.task,
        this.currentRequest.endpoint,
        this.currentRequest.maxIterations,
        this.uiState.currentData || {},
        this.currentRequest.scoreThreshold,
        true
      );
    }
  }

  /**
   * Clear all
   */
  clearAll() {
    this.elements.taskEl.value = '';
    this.uiState.resetUI();
    this.setFinalOutputView('formatted');
    if (this.elements.workflowPanel) this.elements.workflowPanel.classList.remove('workflow-panel--cancelled');
    this.hideCancelButton();
    this.elements.iterationHistory.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">📊</div>
        <p>Run a workflow to see iteration history</p>
      </div>
    `;
  }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  window.agentverse = new AgentVerseApp();
});

// Export for inline onclick handlers
export { AgentVerseApp };
