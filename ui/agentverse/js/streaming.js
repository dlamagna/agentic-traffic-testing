                                                                                                                                                                                                                                                                                                                                                                                                                  /**
 * Server-Sent Events (SSE) Streaming Handler
 */

import { escapeHtml, truncate } from './utils.js';
import { renderExperts, renderLlmRequestsTable } from './renderers.js';

export class StreamingHandler {
  constructor(uiState) {
    this.uiState = uiState;
  }

  /**
   * Handle streaming event
   */
  handleStreamEvent(event, data) {
    console.log('SSE event:', event, data);
    
    if (event === 'iteration_start') {
      this.uiState.elements.statusText.textContent = `Iteration ${data.iteration + 1}/${data.max_iterations}`;
      this.uiState.elements.progressFill.style.width = `${(data.iteration / data.max_iterations) * 100}%`;
    }
    
    else if (event === 'stage_start') {
      const stageNum = data.stage_number;
      this.uiState.updateStage(stageNum, 'running', 'Running...', undefined);
      this.uiState.elements.statusText.textContent = data.message || `Running ${data.stage}...`;
      
      // Update progress based on stage
      const baseProgress = (data.iteration / (this.uiState.currentData?.max_iterations || 3)) * 100;
      const stageProgress = ((stageNum - 1) / 5) * (100 / (this.uiState.currentData?.max_iterations || 3));
      this.uiState.elements.progressFill.style.width = `${Math.min(baseProgress + stageProgress, 95)}%`;
    }
    
    else if (event === 'stage_complete') {
      this._handleStageComplete(data);
    }
    
    else if (event === 'llm_request') {
      this._handleLlmRequest(data);
    }
    
    else if (event === 'discussion_round') {
      this._handleDiscussionRound(data);
    }
    
    else if (event === 'execution_result') {
      this._handleExecutionResult(data);
    }
    
    else if (event === 'vertical_iteration') {
      this._handleVerticalIteration(data);
    }
    
    else if (event === 'error') {
      throw new Error(data.error || 'Unknown error');
    }
  }

  /**
   * Handle stage completion event
   */
  _handleStageComplete(data) {
    const stageNum = data.stage_number;
    
    if (data.stage === 'recruitment') {
      this.uiState.updateStage(1, 'completed', `${data.experts.length} Experts`, `
        <p><strong>Structure:</strong> ${escapeHtml(data.communication_structure || 'horizontal')}</p>
        <p><strong>Reasoning:</strong> ${escapeHtml(data.reasoning || 'N/A')}</p>
        ${renderExperts(data.experts)}
      `);
      // Auto-expand recruitment
      document.getElementById('stage1Content').classList.add('expanded');
    }
    else if (data.stage === 'decision') {
      this.uiState.updateStage(2, 'completed', data.consensus_reached ? 'Consensus' : 'Decided', 
        `<p>Decision complete after ${data.rounds} round(s)</p>`
      );
    }
    else if (data.stage === 'execution') {
      this.uiState.updateStage(3, 'completed', `${data.success_count}/${data.total}`, 
        `<p>${data.success_count} succeeded, ${data.failure_count} failed</p>`
      );
    }
    else if (data.stage === 'evaluation') {
      this.uiState.updateStage(4, 'completed', `${data.score}/100`,
        `<p>Score: ${data.score}/100 | Goal Achieved: ${data.goal_achieved ? '✓' : '✗'}</p>
         ${data.feedback ? `<p><strong>Feedback:</strong> ${escapeHtml(data.feedback)}</p>` : ''}`
      );
      document.getElementById('stage4Content').classList.add('expanded');
    }
    else if (data.stage === 'synthesis') {
      // Show final output
      this.uiState.elements.finalOutputContainer.style.display = 'block';
      this.uiState.elements.finalOutput.textContent = data.final_output || '';
      
      // Stop timer and hide live badge since synthesis is the final stage
      this.uiState.stopTimer(true);
      this.uiState.elements.liveBadge.style.display = 'none';
      this.uiState.elements.statusText.textContent = 'Complete';
      this.uiState.elements.progressFill.style.width = '100%';
      
      // Re-enable the button since workflow is complete
      this.uiState.elements.runBtn.disabled = false;
    }
  }

  /**
   * Handle LLM request event
   */
  _handleLlmRequest(data) {
    // Add to LLM requests list
    if (!this.uiState.currentData) {
      this.uiState.currentData = { llm_requests: [], stages: {} };
    }
    if (!this.uiState.currentData.llm_requests) {
      this.uiState.currentData.llm_requests = [];
    }
    this.uiState.currentData.llm_requests.push(data);
    
    // Update request counter
    const count = this.uiState.currentData.llm_requests.length;
    this.uiState.elements.llmRequestCount.textContent = `${count} LLM request${count !== 1 ? 's' : ''}`;
    
    // Show detailed flow section if we have requests
    const detailedSection = document.getElementById('detailedFlowSection');
    if (detailedSection.style.display === 'none') {
      detailedSection.style.display = 'block';
      // Ensure it's expanded by default when first shown
      document.getElementById('detailedFlowContent').style.display = 'block';
    }
    
    // Update detailed flow table
    const llmTableEl = document.getElementById('llmRequestsTable');
    llmTableEl.innerHTML = renderLlmRequestsTable(this.uiState.currentData.llm_requests);
  }

  /**
   * Handle discussion round event
   */
  _handleDiscussionRound(data) {
    const results2 = document.getElementById('stage2Results');
    const existingContent = results2.innerHTML;
    results2.innerHTML = existingContent + `
      <div class="discussion-round">
        <div class="round-header">Round ${data.round} ${data.consensus ? '✓ Consensus' : ''}</div>
        ${data.responses.map(r => `
          <div class="round-response">
            <div class="role">${escapeHtml(r.expert)} ${r.consensus ? '✓' : ''}</div>
            <div class="content">${escapeHtml(truncate(r.response, 400))}</div>
          </div>
        `).join('')}
      </div>
    `;
    
    // Auto-expand decision stage when discussion starts
    document.getElementById('stage2Content').classList.add('expanded');
  }

  /**
   * Handle execution result event
   */
  _handleExecutionResult(data) {
    const results3 = document.getElementById('stage3Results');
    const existingContent = results3.innerHTML;
    const statusClass = data.success ? 'success' : 'failure';
    results3.innerHTML = existingContent + `
      <div class="execution-result ${statusClass}">
        <div class="result-header">
          <span class="result-expert">${escapeHtml(data.expert)}</span>
          <span class="badge ${data.success ? 'badge-complete' : 'badge-error'}">${data.success ? 'Success' : 'Failed'}</span>
        </div>
        <div class="result-output">${escapeHtml(data.output_preview)}</div>
      </div>
    `;
    
    // Update badge with progress
    this.uiState.updateStage(3, 'running', `${data.completed}/${data.total}`, undefined);
    
    // Auto-expand execution stage when results arrive
    document.getElementById('stage3Content').classList.add('expanded');
  }

  /**
   * Handle vertical iteration event
   */
  _handleVerticalIteration(data) {
    const results2 = document.getElementById('stage2Results');
    const existingContent = results2.innerHTML;
    results2.innerHTML = existingContent + `
      <div class="discussion-round">
        <div class="round-header">Iteration ${data.solver_iteration} ${data.all_approved ? '✓ Approved' : ''}</div>
        <div class="round-response">
          <div class="role">Solver Proposal</div>
          <div class="content">${escapeHtml(data.proposal)}</div>
        </div>
        ${data.reviewer_responses.map(r => `
          <div class="round-response">
            <div class="role">${escapeHtml(r.reviewer)} ${r.approved ? '✓' : ''}</div>
            <div class="content">${escapeHtml(truncate(r.critique, 300))}</div>
          </div>
        `).join('')}
      </div>
    `;
    
    // Auto-expand decision stage when vertical iteration arrives
    document.getElementById('stage2Content').classList.add('expanded');
  }

  /**
   * Run workflow with streaming
   */
  async runWorkflowStreaming(task, endpoint, maxIterations) {
    // Reset and start
    this.uiState.resetUI();
    this.uiState.startTimer();
    this.uiState.elements.runBtn.disabled = true;
    this.uiState.currentData = { max_iterations: maxIterations };
    
    // Show live badge
    this.uiState.elements.liveBadge.style.display = 'inline-flex';
    
    try {
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          task: task,
          max_iterations: maxIterations,
          stream: true
        })
      });
      
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      
      // Ensure streaming is supported
      if (!response.body || !response.body.getReader) {
        throw new Error('Streaming is not supported in this browser/environment.');
      }
      
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let sawComplete = false;
      
      while (true) {
        const { done, value } = await reader.read();
        
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || ''; // Keep incomplete line in buffer
        
        let currentEvent = null;
        let eventDataStr = '';
        
        for (const line of lines) {
          if (line.startsWith('event:')) {
            currentEvent = line.substring(6).trim();
          } else if (line.startsWith('data:')) {
            eventDataStr = line.substring(5).trim();
          } else if (line === '' && currentEvent && eventDataStr) {
            // Complete event received
            try {
              const parsedData = JSON.parse(eventDataStr);
              this.handleStreamEvent(currentEvent, parsedData);
              
              // Handle complete event specially
                if (currentEvent === 'complete') {
                sawComplete = true;
                // Merge complete data with current data
                if (this.uiState.currentData && parsedData) {
                  this.uiState.currentData = { ...this.uiState.currentData, ...parsedData };
                } else {
                  this.uiState.currentData = parsedData;
                }
                this.uiState.updateWorkflowUI(this.uiState.currentData);
                this.uiState.stopTimer(true);
                this.uiState.elements.liveBadge.style.display = 'none';
                this.uiState.elements.statusText.textContent = 'Complete';
                // Re-enable the button
                this.uiState.elements.runBtn.disabled = false;
              }
            } catch (e) {
              console.error('Error parsing SSE data:', e, eventDataStr);
            }
            currentEvent = null;
            eventDataStr = '';
          }
        }
      }
      
      // If the stream ended without a complete event, treat as an error
      if (!sawComplete) {
        throw new Error('Streaming ended before workflow completion.');
      }
      
    } catch (error) {
      console.error('Workflow error:', error);
      this.uiState.stopTimer(false);
      this.uiState.elements.liveBadge.style.display = 'none';
      
      // Check if this is a streaming-specific error
      const isStreamError = error.message && (
        error.message.includes('Streaming is not supported') ||
        error.message.includes('getReader')
      );
      
      if (isStreamError) {
        this.uiState.elements.statusText.textContent = 'Streaming not supported, retrying without streaming...';
        
        // Retry with non-streaming
        try {
          this.uiState.resetUI();
          this.uiState.startTimer();
          this.uiState.updateStage(1, 'running', 'Running...', undefined);
          this.uiState.elements.progressFill.style.width = '10%';
          
          const response = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              task: task,
              max_iterations: maxIterations,
              stream: false
            })
          });
          
          if (!response.ok) {
            const errorText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errorText}`);
          }
          
          const responseData = await response.json();
          this.uiState.updateWorkflowUI(responseData);
          this.uiState.stopTimer(true);
          return; // Success, exit the error handler
        } catch (retryError) {
          console.error('Non-streaming retry failed:', retryError);
          error = retryError; // Continue with error display below
        }
      }
      
      // Display error to user
      this.uiState.elements.statusText.textContent = 'Error';
      
      // Mark all stages as error
      for (let i = 1; i <= 4; i++) {
        this.uiState.updateStage(i, 'error', 'Error', undefined);
      }
      
      this.uiState.elements.rawJson.textContent = `Error: ${error.message}\n\nStack: ${error.stack || 'N/A'}`;
      this.uiState.elements.rawJson.classList.add('visible');
      document.getElementById('rawToggleText').textContent = 'Hide Error Details';
      
    } finally {
      this.uiState.elements.runBtn.disabled = false;
    }
  }
}
