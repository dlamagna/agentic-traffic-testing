/**
 * UI State Management
 */

import { CONFIG } from './config.js';
import { renderExperts, renderDiscussion, renderExecution, renderEvaluation, renderIterationHistory, renderLlmRequestsTable } from './renderers.js';
import { escapeHtml, truncate } from './utils.js';

export class UIState {
  constructor(elements) {
    this.elements = elements;
    this.startTime = null;
    this.timerInterval = null;
    this.currentData = null;
  }

  /**
   * Update timer display
   */
  updateTimer() {
    if (!this.startTime) return;
    const elapsed = ((Date.now() - this.startTime) / 1000).toFixed(1);
    this.elements.statusTime.textContent = `${elapsed}s`;
  }

  /**
   * Start timer
   */
  startTimer() {
    this.startTime = Date.now();
    this.elements.statusIndicator.className = 'status-indicator running';
    this.elements.statusText.textContent = 'Running workflow...';
    this.timerInterval = setInterval(() => this.updateTimer(), CONFIG.TIMER_UPDATE_INTERVAL_MS);
  }

  /**
   * Stop timer
   */
  stopTimer(success = true) {
    if (this.timerInterval) clearInterval(this.timerInterval);
    this.timerInterval = null;
    this.elements.statusIndicator.className = success ? 'status-indicator complete' : 'status-indicator error';
    this.elements.statusText.textContent = success ? 'Complete' : 'Error';
  }

  /**
   * Reset UI to initial state
   */
  resetUI() {
    // Reset stages
    for (let i = 1; i <= 4; i++) {
      const stage = document.getElementById(`stage${i}`);
      const badge = document.getElementById(`stage${i}Badge`);
      const results = document.getElementById(`stage${i}Results`);
      stage.classList.remove('active', 'completed', 'error');
      badge.className = 'badge badge-pending';
      badge.textContent = 'Pending';
      results.innerHTML = '';
    }
    
    // Reset progress
    this.elements.progressFill.style.width = '0%';
    
    // Reset final output
    this.elements.finalOutputContainer.style.display = 'none';
    this.elements.finalOutput.textContent = '';
    
    // Reset raw JSON
    this.elements.rawJson.textContent = '';
    this.elements.rawJson.classList.remove('visible');
    document.getElementById('rawToggleText').textContent = 'Show Raw JSON Response';
    
    // Reset detailed flow
    const detailedSection = document.getElementById('detailedFlowSection');
    detailedSection.style.display = 'none';
    document.getElementById('llmRequestsTable').innerHTML = '';
    
    // Reset status
    this.elements.statusIndicator.className = 'status-indicator';
    this.elements.statusText.textContent = 'Ready';
    this.elements.statusTime.textContent = '';
    this.elements.llmRequestCount.textContent = '';
    
    // Hide live badge
    this.elements.liveBadge.style.display = 'none';
    
    // Clear cancelled state on workflow panel
    if (this.elements.workflowPanel) this.elements.workflowPanel.classList.remove('workflow-panel--cancelled');
  }

  /**
   * Update a specific stage
   */
  updateStage(stageNum, status, badgeText, content) {
    const stage = document.getElementById(`stage${stageNum}`);
    const badge = document.getElementById(`stage${stageNum}Badge`);
    const results = document.getElementById(`stage${stageNum}Results`);
    
    // Stages 5+ (e.g. synthesis) may not exist in the UI - skip
    if (!stage || !badge) return;
    
    // Remove previous states
    stage.classList.remove('active', 'completed', 'error');
    
    // Add new state
    if (status === 'running') {
      stage.classList.add('active');
      badge.className = 'badge badge-running';
    } else if (status === 'completed') {
      stage.classList.add('completed');
      badge.className = 'badge badge-complete';
    } else if (status === 'error') {
      stage.classList.add('error');
      badge.className = 'badge badge-error';
    }
    
    badge.textContent = badgeText;
    if (content !== undefined && results) {
      results.innerHTML = content;
    }
  }

  /**
   * Update workflow UI with complete response data
   */
  updateWorkflowUI(data) {
    this.currentData = data;
    
    // Update raw JSON
    this.elements.rawJson.textContent = JSON.stringify(data, null, 2);
    
    // Update stages
    const stages = data.stages || {};
    
    // Stage 1: Recruitment
    if (stages.recruitment && stages.recruitment.experts) {
      this.updateStage(1, 'completed', `${stages.recruitment.experts.length} Experts`, `
        <p><strong>Structure:</strong> ${escapeHtml(stages.recruitment.communication_structure || 'horizontal')}</p>
        <p><strong>Reasoning:</strong> ${escapeHtml(stages.recruitment.reasoning || 'N/A')}</p>
        ${renderExperts(stages.recruitment.experts)}
      `);
    }
    
    // Stage 2: Decision
    if (stages.decision) {
      const solverRole = stages.decision.solver_role || null;
      const reviewerRoles = Array.isArray(stages.decision.reviewer_roles) ? stages.decision.reviewer_roles : [];
      let roleSummaryHtml = '';
      if (solverRole) {
        roleSummaryHtml += `<p><strong>Solver:</strong> ${escapeHtml(solverRole)}</p>`;
      } else if (stages.decision.structure_used === 'horizontal' && stages.recruitment && Array.isArray(stages.recruitment.experts)) {
        const allRoles = stages.recruitment.experts.map(e => e.role).filter(Boolean);
        if (allRoles.length > 0) {
          roleSummaryHtml += `<p><strong>Contributors:</strong> ${allRoles.map(r => escapeHtml(r)).join(', ')}</p>`;
        }
      }
      if (reviewerRoles.length > 0) {
        roleSummaryHtml += `<p><strong>Reviewers:</strong> ${reviewerRoles.map(r => escapeHtml(r)).join(', ')}</p>`;
      }
      this.updateStage(2, 'completed', stages.decision.consensus_reached ? 'Consensus' : 'Decided', 
        roleSummaryHtml + renderDiscussion(
          stages.decision.discussion_rounds,
          stages.decision.structure_used
        )
      );
    }
    
    // Stage 3: Execution
    if (stages.execution) {
      this.updateStage(3, 'completed', `${stages.execution.success_count}/${stages.execution.outputs?.length || 0}`,
        renderExecution(
          stages.execution.outputs,
          stages.execution.success_count,
          stages.execution.failure_count
        )
      );
    }
    
    // Stage 4: Evaluation
    if (stages.evaluation) {
      this.updateStage(4, stages.evaluation.goal_achieved ? 'completed' : 'completed', 
        `${stages.evaluation.score}/100`,
        renderEvaluation(stages.evaluation)
      );
    }
    
    // Update progress
    this.elements.progressFill.style.width = '100%';
    
    // Update final output
    if (data.final_output) {
      this.elements.finalOutputContainer.style.display = 'block';
      this.elements.finalOutput.textContent = data.final_output;
    }
    
    // Update iteration history (ensure we have an array and container exists)
    const iterationHistory = Array.isArray(data.iteration_history) ? data.iteration_history : [];
    console.log('[AgentVerse] Rendering iteration history, count:', iterationHistory.length, 'container:', this.elements.iterationHistory);
    if (this.elements.iterationHistory) {
      try {
        renderIterationHistory(iterationHistory, this.elements.iterationHistory);
        console.log('[AgentVerse] Iteration history rendered successfully');
      } catch (err) {
        console.error('[AgentVerse] Failed to render iteration history:', err);
      }
    } else {
      console.warn('[AgentVerse] Iteration history container not found');
    }
    
    // Update detailed flow section
    const detailedSection = document.getElementById('detailedFlowSection');
    const llmTableEl = document.getElementById('llmRequestsTable');
    if (data.llm_requests && data.llm_requests.length > 0) {
      detailedSection.style.display = 'block';
      llmTableEl.innerHTML = renderLlmRequestsTable(data.llm_requests);
    }
    
    // Expand stages with content
    if (stages.recruitment) this.toggleStage('stage1');
    if (data.final_output) {
      document.getElementById('stage4Content').classList.add('expanded');
    }
  }

  /**
   * Toggle stage expansion
   */
  toggleStage(stageId) {
    const content = document.getElementById(stageId + 'Content');
    content.classList.toggle('expanded');
  }
}
