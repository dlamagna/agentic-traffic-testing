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
      endpointEl: document.getElementById('endpoint'),
      runBtn: document.getElementById('runBtn'),
      clearBtn: document.getElementById('clearBtn'),
      statusIndicator: document.getElementById('statusIndicator'),
      statusText: document.getElementById('statusText'),
      statusTime: document.getElementById('statusTime'),
      progressFill: document.getElementById('progressFill'),
      finalOutputContainer: document.getElementById('finalOutputContainer'),
      finalOutput: document.getElementById('finalOutput'),
      rawJson: document.getElementById('rawJson'),
      iterationHistory: document.getElementById('iterationHistory'),
      llmRequestCount: document.getElementById('llmRequestCount'),
      liveBadge: document.getElementById('liveBadge'),
    };

    // Initialize state
    this.uiState = new UIState(this.elements);
    this.streamingHandler = new StreamingHandler(this.uiState);

    // Bind methods
    this.runWorkflow = this.runWorkflow.bind(this);
    this.clearAll = this.clearAll.bind(this);
    this.loadExample = this.loadExample.bind(this);
    this.toggleStage = this.toggleStage.bind(this);
    this.toggleDetailedFlow = this.toggleDetailedFlow.bind(this);
    this.toggleRawJson = this.toggleRawJson.bind(this);
    this.setFlowView = this.setFlowView.bind(this);
    this.toggleFlowRow = this.toggleFlowRow.bind(this);

    // Initialize
    this.init();
  }

  init() {
    // Set default endpoint
    this.elements.endpointEl.value = getDefaultEndpoint();

    // Add event listeners
    this.elements.runBtn.addEventListener('click', this.runWorkflow);
    this.elements.clearBtn.addEventListener('click', this.clearAll);
    
    // Keyboard shortcut
    this.elements.taskEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        this.runWorkflow();
      }
    });
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
    el.classList.toggle('visible');
    toggle.textContent = el.classList.contains('visible') ? 'Hide Raw JSON Response' : 'Show Raw JSON Response';
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
    
    // Always try streaming mode
    await this.streamingHandler.runWorkflowStreaming(task, endpoint, maxIterations);
  }

  /**
   * Clear all
   */
  clearAll() {
    this.elements.taskEl.value = '';
    this.uiState.resetUI();
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
