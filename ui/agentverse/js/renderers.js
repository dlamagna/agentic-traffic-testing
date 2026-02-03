/**
 * UI Rendering Functions
 */

import { escapeHtml, truncate, getStageBadgeClass, getStageColor } from './utils.js';

/**
 * Render expert cards
 */
export function renderExperts(experts) {
  if (!experts || experts.length === 0) return '<p>No experts recruited.</p>';
  
  return `
    <div class="expert-grid">
      ${experts.map(e => `
        <div class="expert-card">
          <div class="expert-role">${escapeHtml(e.role)}</div>
          <div class="expert-responsibilities">${escapeHtml(truncate(e.responsibilities, 100))}</div>
        </div>
      `).join('')}
    </div>
  `;
}

/**
 * Render discussion rounds
 */
export function renderDiscussion(rounds, structure) {
  if (!rounds || rounds.length === 0) return '<p>No discussion recorded.</p>';
  
  let html = `<p><strong>Structure:</strong> ${escapeHtml(structure)}</p>`;
  
  if (structure === 'horizontal') {
    rounds.forEach((round, idx) => {
      const responses = round.responses || [];
      const total = responses.length;
      const consensusCount = responses.filter(r => r.consensus).length;
      const missingConsensus = responses
        .filter(r => !r.consensus)
        .map(r => r.expert || 'Agent');
      
      let consensusSummary;
      if (total === 0) {
        consensusSummary = 'No responses';
      } else if (consensusCount === total) {
        consensusSummary = 'Consensus reached';
      } else {
        consensusSummary = 'Missing consensus from: ' + missingConsensus.map(name => escapeHtml(name)).join(', ');
      }
      
      html += `
        <div class="discussion-round">
          <div class="round-header">
            <span>Round ${round.round || idx + 1}</span>
            <span style="font-size: 12px; color: var(--text-secondary); margin-left: 8px;">
              ${consensusSummary}
            </span>
          </div>
          ${responses.map(r => `
            <div class="round-response">
              <div class="role">
                ${escapeHtml(r.expert || 'Agent')}
                ${r.consensus ? ' ✓' : ''}
              </div>
              <div class="content">${escapeHtml(truncate(r.response, 400))}</div>
            </div>
          `).join('')}
        </div>
      `;
    });
  } else {
    // Vertical structure
    rounds.forEach((round, idx) => {
      const reviewers = round.reviewer_responses || [];
      const total = reviewers.length;
      const approvedCount = reviewers.filter(r => r.approved).length;
      const missingApprovals = reviewers
        .filter(r => !r.approved)
        .map(r => r.reviewer || 'Reviewer');
      
      let approvalSummary;
      if (total === 0) {
        approvalSummary = 'No reviews';
      } else if (approvedCount === total) {
        approvalSummary = 'All reviewers approved';
      } else {
        approvalSummary = 'Waiting approval from: ' + missingApprovals.map(name => escapeHtml(name)).join(', ');
      }
      
      html += `
        <div class="discussion-round">
          <div class="round-header">
            <span>Iteration ${round.iteration || idx + 1}</span>
            <span style="font-size: 12px; color: var(--text-secondary); margin-left: 8px;">
              ${approvalSummary}
            </span>
          </div>
          <div class="round-response">
            <div class="role">Solver Proposal</div>
            <div class="content">${escapeHtml(truncate(round.proposal, 400))}</div>
          </div>
          ${reviewers.map(r => `
            <div class="round-response">
              <div class="role">${escapeHtml(r.reviewer)} ${r.approved ? '✓' : ''}</div>
              <div class="content">${escapeHtml(truncate(r.critique, 300))}</div>
            </div>
          `).join('')}
        </div>
      `;
    });
  }
  
  return html;
}

/**
 * Render execution results
 */
export function renderExecution(outputs, successCount, failureCount) {
  if (!outputs || outputs.length === 0) return '<p>No execution results.</p>';
  
  let html = `<p><strong>Success:</strong> ${successCount} | <strong>Failed:</strong> ${failureCount}</p>`;
  
  outputs.forEach(output => {
    const statusClass = output.success ? 'success' : 'failure';
    html += `
      <div class="execution-result ${statusClass}">
        <div class="result-header">
          <span class="result-expert">${escapeHtml(output.expert)}</span>
          <span class="badge ${output.success ? 'badge-complete' : 'badge-error'}">${output.success ? 'Success' : 'Failed'}</span>
        </div>
        <div class="result-output">${escapeHtml(truncate(output.output, 500))}</div>
      </div>
    `;
  });
  
  return html;
}

/**
 * Render evaluation
 */
export function renderEvaluation(evaluation) {
  if (!evaluation) return '<p>No evaluation results.</p>';
  
  const scoreColor = evaluation.score >= 70 ? 'var(--success)' : evaluation.score >= 40 ? 'var(--warning)' : 'var(--error)';
  
  let html = `
    <div class="evaluation-summary">
      <div class="eval-metric">
        <div class="eval-value" style="color: ${scoreColor}">${evaluation.score}</div>
        <div class="eval-label">Score</div>
      </div>
      <div class="eval-metric">
        <div class="eval-value" style="color: ${evaluation.goal_achieved ? 'var(--success)' : 'var(--warning)'}">${evaluation.goal_achieved ? '✓' : '✗'}</div>
        <div class="eval-label">Goal Achieved</div>
      </div>
    </div>
  `;
  
  // Display criteria breakdown if available
  if (evaluation.criteria) {
    html += `<div style="margin-top: 16px; padding: 12px; background: var(--bg-secondary); border-radius: 6px;">`;
    html += `<div style="font-weight: 600; margin-bottom: 8px; color: var(--text-primary);">Score Breakdown:</div>`;
    html += `<div class="criteria-breakdown">`;
    
    const criteriaLabels = {
      completeness: 'Completeness',
      correctness: 'Correctness',
      clarity: 'Clarity',
      relevance: 'Relevance',
      actionability: 'Actionability'
    };
    
    Object.entries(evaluation.criteria).forEach(([key, value]) => {
      const label = criteriaLabels[key] || key;
      const criteriaColor = value >= 70 ? 'var(--success)' : value >= 40 ? 'var(--warning)' : 'var(--error)';
      html += `
        <div class="criteria-item">
          <div class="criteria-label">${escapeHtml(label)}:</div>
          <div class="criteria-bar">
            <div class="criteria-bar-fill" style="width: ${value}%; background: ${criteriaColor};"></div>
            <span class="criteria-value" style="color: ${criteriaColor}">${value}</span>
          </div>
        </div>
      `;
    });
    
    html += `</div></div>`;
  }
  
  // Display rationale if available
  if (evaluation.rationale) {
    html += `<div style="margin-top: 12px; padding: 12px; background: var(--bg-secondary); border-radius: 6px; border-left: 3px solid var(--accent);">`;
    html += `<div style="font-weight: 600; margin-bottom: 6px; color: var(--text-primary);">Score Rationale:</div>`;
    html += `<div style="color: var(--text-secondary); font-size: 14px; line-height: 1.5;">${escapeHtml(evaluation.rationale)}</div>`;
    html += `</div>`;
  }
  
  if (evaluation.feedback) {
    html += `<p style="margin-top: 12px;"><strong>Feedback:</strong> ${escapeHtml(evaluation.feedback)}</p>`;
  }
  
  if (evaluation.missing_aspects && evaluation.missing_aspects.length > 0) {
    html += `<p><strong>Missing:</strong> ${evaluation.missing_aspects.map(a => escapeHtml(a)).join(', ')}</p>`;
  }
  
  return html;
}

/**
 * Render iteration history with enhanced tracking and comparison
 */
export function renderIterationHistory(history, container) {
  if (!history || history.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">📊</div>
        <p>Run a workflow to see iteration history</p>
      </div>
    `;
    return;
  }
  
  // Score progression chart
  const scores = history.map(h => h.evaluation?.score || 0);
  const maxScore = Math.max(...scores, 100);
  const minScore = Math.min(...scores, 0);
  const scoreRange = maxScore - minScore || 1;
  
  let html = '<div class="iteration-history-container">';
  
  // Score progression visualization
  html += '<div class="score-progression">';
  html += '<div class="score-progression-header">';
  html += '<span class="score-progression-title">Score Progression</span>';
  html += '<span class="score-progression-subtitle">Click an iteration to view details</span>';
  html += '</div>';
  html += '<div class="score-progression-chart">';
  scores.forEach((score, idx) => {
    const prevScore = idx > 0 ? scores[idx - 1] : null;
    const scoreChange = prevScore !== null ? score - prevScore : null;
    const heightPercent = ((score - minScore) / scoreRange) * 100;
    const scoreColor = score >= 70 ? 'var(--success)' : score >= 40 ? 'var(--warning)' : 'var(--error)';
    const changeClass = scoreChange !== null 
      ? (scoreChange > 0 ? 'improved' : scoreChange < 0 ? 'worsened' : 'unchanged')
      : '';
    const changeIcon = scoreChange !== null
      ? (scoreChange > 0 ? '↑' : scoreChange < 0 ? '↓' : '→')
      : '';
    
    html += `
      <div class="score-bar-container" data-iteration="${idx}" onclick="window.agentverse.selectIteration(${idx})">
        <div class="score-bar-wrapper">
          <div class="score-bar" style="height: ${heightPercent}%; background: ${scoreColor};" title="Score: ${score}/100"></div>
          <div class="score-value">${score}</div>
        </div>
        <div class="score-label">Iter ${idx + 1}</div>
        ${scoreChange !== null ? `<div class="score-change ${changeClass}">${changeIcon} ${Math.abs(scoreChange)}</div>` : ''}
      </div>
    `;
  });
  html += '</div>';
  html += '</div>';
  
  // Iteration tabs
  html += '<div class="iteration-tabs">';
  history.forEach((h, idx) => {
    const score = h.evaluation?.score || 0;
    const goalAchieved = h.evaluation?.goal_achieved || false;
    const scoreIcon = score >= 70 ? '✓' : score >= 40 ? '~' : '✗';
    const tabClass = idx === 0 ? 'active' : '';
    const scoreColor = score >= 70 ? 'var(--success)' : score >= 40 ? 'var(--warning)' : 'var(--error)';
    
    html += `<div class="iteration-tab ${tabClass}" data-iteration="${idx}" onclick="window.agentverse.selectIteration(${idx})">
      <span class="iteration-tab-icon">${scoreIcon}</span>
      <span class="iteration-tab-number">Iter ${idx + 1}</span>
      <span class="iteration-tab-score" style="color: ${scoreColor}">${score}/100</span>
      ${goalAchieved ? '<span class="iteration-tab-badge">Goal ✓</span>' : ''}
    </div>`;
  });
  html += '</div>';
  
  // Iteration details panels
  html += '<div class="iteration-details-container">';
  history.forEach((h, idx) => {
    const isActive = idx === 0 ? 'active' : '';
    html += `<div class="iteration-details ${isActive}" data-iteration="${idx}">`;
    html += renderIterationDetails(h, idx, idx > 0 ? history[idx - 1] : null);
    html += '</div>';
  });
  html += '</div>';
  
  html += '</div>';
  
  container.innerHTML = html;
}

/**
 * Render detailed view for a single iteration
 */
function renderIterationDetails(iteration, idx, previousIteration) {
  const evaluation = iteration.evaluation || {};
  const score = evaluation.score || 0;
  const goalAchieved = evaluation.goal_achieved || false;
  const scoreColor = score >= 70 ? 'var(--success)' : score >= 40 ? 'var(--warning)' : 'var(--error)';
  
  let html = '';
  
  // Header with score
  html += `<div class="iteration-header">
    <div class="iteration-header-main">
      <h3 class="iteration-title">Iteration ${idx + 1} Details</h3>
      <div class="iteration-score-large" style="color: ${scoreColor}">${score}/100</div>
    </div>
    <div class="iteration-meta">
      <span>Duration: ${iteration.duration_seconds || 0}s</span>
      ${goalAchieved ? '<span class="goal-badge">Goal Achieved ✓</span>' : '<span class="goal-badge failed">Goal Not Met ✗</span>'}
    </div>
  </div>`;
  
  // Changes from previous iteration
  if (previousIteration) {
    html += '<div class="iteration-changes">';
    html += '<div class="changes-header">Changes from Previous Iteration</div>';
    html += renderIterationChanges(iteration, previousIteration);
    html += '</div>';
  }
  
  // Evaluation breakdown
  html += '<div class="iteration-evaluation">';
  html += '<div class="evaluation-header">Evaluation Results</div>';
  
  // Feedback passed to next iteration's recruitment (only stage that receives it)
  if (evaluation.feedback && evaluation.feedback.trim()) {
    html += `<div class="iteration-feedback-section">
      <div class="iteration-feedback-label">Feedback → Next recruitment</div>
      <div class="iteration-feedback-content">${escapeHtml(evaluation.feedback)}</div>
      <div class="iteration-feedback-hint">This feedback is passed only to the recruitment stage of the next iteration to help select or adjust the expert team.</div>
    </div>`;
  }
  
  if (evaluation.rationale) {
    html += `<div class="evaluation-rationale">
      <div class="evaluation-label">Rationale:</div>
      <div class="evaluation-content">${escapeHtml(evaluation.rationale)}</div>
    </div>`;
  }
  
  if (evaluation.criteria) {
    html += '<div class="evaluation-criteria">';
    html += '<div class="evaluation-label">Score Breakdown:</div>';
    const criteriaLabels = {
      completeness: 'Completeness',
      correctness: 'Correctness',
      clarity: 'Clarity',
      relevance: 'Relevance',
      actionability: 'Actionability'
    };
    
    Object.entries(evaluation.criteria).forEach(([key, value]) => {
      const label = criteriaLabels[key] || key;
      const criteriaColor = value >= 70 ? 'var(--success)' : value >= 40 ? 'var(--warning)' : 'var(--error)';
      const prevValue = previousIteration?.evaluation?.criteria?.[key];
      const change = prevValue !== undefined ? value - prevValue : null;
      const changeIndicator = change !== null
        ? (change > 0 ? `<span class="criteria-change improved">+${change}</span>` 
           : change < 0 ? `<span class="criteria-change worsened">${change}</span>`
           : '<span class="criteria-change unchanged">→</span>')
        : '';
      
      html += `
        <div class="criteria-item-detailed">
          <div class="criteria-label-detailed">${escapeHtml(label)}:</div>
          <div class="criteria-bar-detailed">
            <div class="criteria-bar-fill-detailed" style="width: ${value}%; background: ${criteriaColor};"></div>
            <span class="criteria-value-detailed" style="color: ${criteriaColor}">${value}</span>
          </div>
          ${changeIndicator}
        </div>
      `;
    });
    html += '</div>';
  }
  
  html += '</div>';
  
  // Stage summaries
  html += '<div class="iteration-stages">';
  html += '<div class="stages-header">Stage Summaries</div>';
  
  // Recruitment
  if (iteration.recruitment) {
    html += `<div class="stage-summary">
      <div class="stage-summary-header">
        <span class="stage-summary-title">Expert Recruitment</span>
      </div>
      <div class="stage-summary-content">
        <div><strong>Experts:</strong> ${(iteration.recruitment.experts || []).map(e => escapeHtml(e)).join(', ') || 'N/A'}</div>
        <div><strong>Structure:</strong> ${escapeHtml(iteration.recruitment.structure || 'N/A')}</div>
      </div>
    </div>`;
  }
  
  // Decision
  if (iteration.decision) {
    html += `<div class="stage-summary">
      <div class="stage-summary-header">
        <span class="stage-summary-title">Decision-Making</span>
      </div>
      <div class="stage-summary-content">
        <div><strong>Consensus:</strong> ${iteration.decision.consensus ? '✓ Reached' : '✗ Not reached'}</div>
        <div><strong>Rounds:</strong> ${iteration.decision.rounds || 0}</div>
      </div>
    </div>`;
  }
  
  // Execution
  if (iteration.execution) {
    html += `<div class="stage-summary">
      <div class="stage-summary-header">
        <span class="stage-summary-title">Execution</span>
      </div>
      <div class="stage-summary-content">
        <div><strong>Success:</strong> ${iteration.execution.success || 0}</div>
        <div><strong>Failures:</strong> ${iteration.execution.failures || 0}</div>
      </div>
    </div>`;
  }
  
  html += '</div>';
  
  return html;
}

/**
 * Render changes between two iterations
 */
function renderIterationChanges(current, previous) {
  const changes = [];
  
  // Score change
  const currentScore = current.evaluation?.score || 0;
  const prevScore = previous.evaluation?.score || 0;
  const scoreChange = currentScore - prevScore;
  if (scoreChange !== 0) {
    const changeClass = scoreChange > 0 ? 'improved' : 'worsened';
    changes.push({
      type: 'score',
      label: 'Score',
      change: `${scoreChange > 0 ? '+' : ''}${scoreChange}`,
      class: changeClass,
      from: prevScore,
      to: currentScore
    });
  }
  
  // Experts change
  const currentExperts = (current.recruitment?.experts || []).sort().join(',');
  const prevExperts = (previous.recruitment?.experts || []).sort().join(',');
  if (currentExperts !== prevExperts) {
    changes.push({
      type: 'experts',
      label: 'Expert Team',
      change: 'Changed',
      class: 'changed',
      from: prevExperts || 'None',
      to: currentExperts || 'None'
    });
  }
  
  // Structure change
  const currentStructure = current.recruitment?.structure || '';
  const prevStructure = previous.recruitment?.structure || '';
  if (currentStructure !== prevStructure) {
    changes.push({
      type: 'structure',
      label: 'Communication Structure',
      change: 'Changed',
      class: 'changed',
      from: prevStructure || 'None',
      to: currentStructure || 'None'
    });
  }
  
  // Consensus change
  const currentConsensus = current.decision?.consensus || false;
  const prevConsensus = previous.decision?.consensus || false;
  if (currentConsensus !== prevConsensus) {
    changes.push({
      type: 'consensus',
      label: 'Consensus',
      change: currentConsensus ? 'Achieved' : 'Lost',
      class: currentConsensus ? 'improved' : 'worsened',
      from: prevConsensus ? 'Yes' : 'No',
      to: currentConsensus ? 'Yes' : 'No'
    });
  }
  
  // Execution success change
  const currentSuccess = current.execution?.success || 0;
  const prevSuccess = previous.execution?.success || 0;
  if (currentSuccess !== prevSuccess) {
    changes.push({
      type: 'execution',
      label: 'Execution Success',
      change: `${currentSuccess - prevSuccess > 0 ? '+' : ''}${currentSuccess - prevSuccess}`,
      class: currentSuccess > prevSuccess ? 'improved' : 'worsened',
      from: prevSuccess,
      to: currentSuccess
    });
  }
  
  if (changes.length === 0) {
    return '<div class="no-changes">No significant changes detected</div>';
  }
  
  let html = '<div class="changes-list">';
  changes.forEach(change => {
    html += `
      <div class="change-item ${change.class}">
        <div class="change-label">${escapeHtml(change.label)}</div>
        <div class="change-value">
          <span class="change-from">${escapeHtml(String(change.from))}</span>
          <span class="change-arrow">→</span>
          <span class="change-to">${escapeHtml(String(change.to))}</span>
          <span class="change-indicator">${escapeHtml(change.change)}</span>
        </div>
      </div>
    `;
  });
  html += '</div>';
  
  return html;
}

/**
 * Render LLM requests graph as SVG
 */
export function renderLlmRequestsGraph(requests) {
  if (!requests || requests.length === 0) {
    return '<p style="color: var(--text-secondary);">No LLM requests recorded.</p>';
  }
  
  // Build vertical swimlanes: Agent A (orchestrator) + Agent B roles + LLM Backend
  const orchestratorKey = 'Agent A (orchestrator)';
  const llmBackendKey = 'LLM Backend';
  const laneOrder = [orchestratorKey];
  const laneIndex = { [orchestratorKey]: 0 };
  
  requests.forEach(req => {
    const roleRaw = (req.agent_role || '').trim();
    const roleLower = roleRaw.toLowerCase();
    if (!roleRaw || roleLower.includes('orchestrator')) {
      return;
    }
    const laneName = `Agent B – ${roleRaw}`;
    if (laneIndex[laneName] === undefined) {
      laneIndex[laneName] = laneOrder.length;
      laneOrder.push(laneName);
    }
  });
  
  // Ensure at least one Agent B swimlane for clarity
  if (laneOrder.length === 1) {
    const fallbackLane = 'Agent B – worker';
    laneIndex[fallbackLane] = laneOrder.length;
    laneOrder.push(fallbackLane);
  }
  
  // Add LLM Backend as the last lane
  laneIndex[llmBackendKey] = laneOrder.length;
  laneOrder.push(llmBackendKey);
  
  const laneGapX = 140;
  const paddingX = 80;
  const paddingY = 40;
  const stepY = 80; // Increased to accommodate multiple edges per request
  const width = paddingX * 2 + Math.max(1, laneOrder.length - 1) * laneGapX;
  const height = paddingY * 2 + Math.max(1, requests.length - 1) * stepY;
  
  let svg = `
    <svg class="flow-graph-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet">
      <defs>
        <marker id="arrowhead" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto" markerUnits="strokeWidth">
          <path d="M0,0 L0,6 L6,3 z" fill="currentColor"></path>
        </marker>
      </defs>
  `;
  
  // Lanes (vertical)
  laneOrder.forEach((name, idx) => {
    const x = paddingX + idx * laneGapX;
    svg += `
      <line class="flow-graph-lane" x1="${x}" y1="30" x2="${x}" y2="${height - 20}"></line>
      <text class="flow-graph-lane-label" x="${x}" y="20" text-anchor="middle">${escapeHtml(name)}</text>
    `;
  });
  
  // Edges and nodes (time flows downward)
  // For Agent B calls, we show multiple edges: A→B (message), B→LLM (call), B→A (return)
  requests.forEach((req, idx) => {
    const stage = req.stage || 'unknown';
    const label = req.label || '';
    const roleRaw = (req.agent_role || '').trim();
    const roleLower = roleRaw.toLowerCase();
    const source = (req.source || '').trim();
    
    // Check if this is an Agent B LLM call (source starts with "agent-b-")
    const isAgentBCall = source.toLowerCase().startsWith('agent-b-');
    const isAgentACall = !roleRaw || roleLower.includes('orchestrator') || source === 'Agent A';
    
    const baseY = paddingY + idx * stepY;
    const color = getStageColor(stage);
    const durationStr = req.duration_seconds != null ? ` • ${req.duration_seconds}s` : '';
    const reqIdStr = req.request_id ? ` • id=${req.request_id}` : '';
    const title = `#${req.seq} ${stage} – ${label}${durationStr}${reqIdStr}`;
    const titleAttr = escapeHtml(title);
    
    if (isAgentACall) {
      // Agent A's direct LLM call: Agent A → LLM Backend
      const x1 = paddingX + (laneIndex[orchestratorKey] ?? 0) * laneGapX;
      const x2 = paddingX + (laneIndex[llmBackendKey] ?? 0) * laneGapX;
      const midX = (x1 + x2) / 2;
      // Invisible wide rect for reliable hover (SVG title can be finicky)
      const rectW = Math.abs(x2 - x1) + 20;
      const rectX = Math.min(x1, x2) - 10;
      
      svg += `
        <g class="flow-graph-hoverable" data-tooltip="${titleAttr}">
          <rect x="${rectX}" y="${baseY - 12}" width="${rectW}" height="24" fill="transparent" stroke="none" pointer-events="all"/>
          <path
            class="flow-graph-edge"
            d="M ${x1} ${baseY} L ${x2} ${baseY}"
            stroke="${color}"
            marker-end="url(#arrowhead)"
          ></path>
          <circle
            class="flow-graph-node"
            cx="${midX}"
            cy="${baseY}"
            r="9"
            fill="${color}"
          ></circle>
          <text
            class="flow-graph-node-label"
            x="${midX}"
            y="${baseY + 3}"
            text-anchor="middle"
          >
            ${req.seq}
          </text>
        </g>
      `;
    } else if (isAgentBCall) {
      // Agent B's LLM call: Show complete flow
      // 1. Agent A → Agent B (message sent)
      // 2. Agent B → LLM Backend (LLM call)
      // 3. Agent B → Agent A (results returned)
      const bLane = laneIndex[`Agent B – ${roleRaw}`] !== undefined
        ? `Agent B – ${roleRaw}`
        : laneOrder[1]; // fallback Agent B lane
      
      const xA = paddingX + (laneIndex[orchestratorKey] ?? 0) * laneGapX;
      const xB = paddingX + (laneIndex[bLane] ?? 0) * laneGapX;
      const xLLM = paddingX + (laneIndex[llmBackendKey] ?? 0) * laneGapX;
      
      const y1 = baseY - 15; // Message sent
      const y2 = baseY;      // LLM call
      const y3 = baseY + 15; // Results returned
      const rectW1 = Math.abs(xB - xA) + 20;
      const rectX1 = Math.min(xA, xB) - 10;
      
      // Edge 1: Agent A → Agent B (message sent)
      const edge1Title = `${titleAttr} - Message sent`;
      const edge3Title = `${titleAttr} - Results returned`;
      svg += `
        <g class="flow-graph-hoverable" data-tooltip="${edge1Title}" style="cursor: pointer;">
          <rect x="${rectX1}" y="${y1 - 12}" width="${rectW1}" height="24" fill="transparent" stroke="none" pointer-events="all"/>
          <path
            class="flow-graph-edge"
            d="M ${xA} ${y1} L ${xB} ${y1}"
            stroke="${color}"
            stroke-opacity="0.6"
            stroke-width="2"
            marker-end="url(#arrowhead)"
          ></path>
        </g>
      `;
      
      // Edge 2: Agent B → LLM Backend (LLM call)
      const rectW2 = Math.abs(xLLM - xB) + 20;
      const rectX2 = Math.min(xB, xLLM) - 10;
      svg += `
        <g class="flow-graph-hoverable" data-tooltip="${titleAttr}" style="cursor: pointer;">
          <rect x="${rectX2}" y="${y2 - 12}" width="${rectW2}" height="24" fill="transparent" stroke="none" pointer-events="all"/>
          <path
            class="flow-graph-edge"
            d="M ${xB} ${y2} L ${xLLM} ${y2}"
            stroke="${color}"
            marker-end="url(#arrowhead)"
          ></path>
          <circle
            class="flow-graph-node"
            cx="${(xB + xLLM) / 2}"
            cy="${y2}"
            r="9"
            fill="${color}"
          ></circle>
          <text
            class="flow-graph-node-label"
            x="${(xB + xLLM) / 2}"
            y="${y2 + 3}"
            text-anchor="middle"
          >
            ${req.seq}
          </text>
        </g>
      `;
      
      // Edge 3: Agent B → Agent A (results returned)
      const rectW3 = rectW1;
      const rectX3 = rectX1;
      svg += `
        <g class="flow-graph-hoverable" data-tooltip="${edge3Title}" style="cursor: pointer;">
          <rect x="${rectX3}" y="${y3 - 12}" width="${rectW3}" height="24" fill="transparent" stroke="none" pointer-events="all"/>
          <path
            class="flow-graph-edge"
            d="M ${xB} ${y3} L ${xA} ${y3}"
            stroke="${color}"
            stroke-opacity="0.6"
            stroke-width="2"
            marker-end="url(#arrowhead)"
          ></path>
        </g>
      `;
    } else {
      // Fallback: treat as Agent B call with same multi-edge flow
      const bLane = laneIndex[`Agent B – ${roleRaw}`] !== undefined
        ? `Agent B – ${roleRaw}`
        : laneOrder[1];
      
      const xA = paddingX + (laneIndex[orchestratorKey] ?? 0) * laneGapX;
      const xB = paddingX + (laneIndex[bLane] ?? 0) * laneGapX;
      const xLLM = paddingX + (laneIndex[llmBackendKey] ?? 0) * laneGapX;
      
      const y1 = baseY - 15;
      const y2 = baseY;
      const y3 = baseY + 15;
      
      const fallbackEdge1 = `${titleAttr} - Message sent`;
      const fallbackEdge3 = `${titleAttr} - Results returned`;
      const fallbackRectW = Math.abs(xB - xA) + 20;
      const fallbackRectX = Math.min(xA, xB) - 10;
      const fallbackRectW2 = Math.abs(xLLM - xB) + 20;
      const fallbackRectX2 = Math.min(xB, xLLM) - 10;
      svg += `
        <g class="flow-graph-hoverable" data-tooltip="${fallbackEdge1}" style="cursor: pointer;">
          <rect x="${fallbackRectX}" y="${y1 - 12}" width="${fallbackRectW}" height="24" fill="transparent" stroke="none" pointer-events="all"/>
          <path
            class="flow-graph-edge"
            d="M ${xA} ${y1} L ${xB} ${y1}"
            stroke="${color}"
            stroke-opacity="0.6"
            stroke-width="2"
            marker-end="url(#arrowhead)"
          ></path>
        </g>
        <g class="flow-graph-hoverable" data-tooltip="${titleAttr}" style="cursor: pointer;">
          <rect x="${fallbackRectX2}" y="${y2 - 12}" width="${fallbackRectW2}" height="24" fill="transparent" stroke="none" pointer-events="all"/>
          <path
            class="flow-graph-edge"
            d="M ${xB} ${y2} L ${xLLM} ${y2}"
            stroke="${color}"
            marker-end="url(#arrowhead)"
          ></path>
          <circle
            class="flow-graph-node"
            cx="${(xB + xLLM) / 2}"
            cy="${y2}"
            r="9"
            fill="${color}"
          ></circle>
          <text
            class="flow-graph-node-label"
            x="${(xB + xLLM) / 2}"
            y="${y2 + 3}"
            text-anchor="middle"
          >
            ${req.seq}
          </text>
        </g>
        <g class="flow-graph-hoverable" data-tooltip="${fallbackEdge3}" style="cursor: pointer;">
          <rect x="${fallbackRectX}" y="${y3 - 12}" width="${fallbackRectW}" height="24" fill="transparent" stroke="none" pointer-events="all"/>
          <path
            class="flow-graph-edge"
            d="M ${xB} ${y3} L ${xA} ${y3}"
            stroke="${color}"
            stroke-opacity="0.6"
            stroke-width="2"
            marker-end="url(#arrowhead)"
          ></path>
        </g>
      `;
    }
  });
  
  svg += '</svg>';
  
  // Legend
  const stages = ['recruitment', 'decision', 'execution', 'evaluation', 'synthesis'];
  const legendItems = stages.map(s => `
    <div class="flow-graph-legend-item">
      <span class="flow-graph-legend-swatch" style="background: ${getStageColor(s)};"></span>
      <span>${s}</span>
    </div>
  `).join('');
  
  return `
    <div class="flow-graph-container">
      <div class="flow-graph-legend">
        ${legendItems}
      </div>
      ${svg}
    </div>
  `;
}

/**
 * Render LLM requests table
 */
export function renderLlmRequestsTable(requests) {
  if (!requests || requests.length === 0) {
    return '<p style="color: var(--text-secondary);">No LLM requests recorded.</p>';
  }
  
  const rows = requests.map((req, idx) => {
    const stage = req.stage || 'unknown';
    const label = req.label || '';
    const source = req.source || '—';
    const role = req.agent_role || (req.source === 'Agent A' ? 'orchestrator' : '—');
    const promptPreview = truncate(req.prompt || '', 80);
    const responsePreview = truncate(req.response || '', 80);
    const durationStr = req.duration_seconds != null ? `${req.duration_seconds}s` : '—';
    const reqJson = JSON.stringify(req, null, 2);
    const reqJsonEscaped = escapeHtml(reqJson);
    
    return `
      <tr data-seq="${req.seq}" onclick="window.agentverse.toggleFlowRow(${req.seq})">
        <td class="seq-col">${req.seq}</td>
        <td class="id-col">${req.request_id ? escapeHtml(req.request_id) : '—'}</td>
        <td class="stage-col"><span class="flow-stage-badge ${getStageBadgeClass(stage)}">${escapeHtml(stage)}</span></td>
        <td class="label-col">${escapeHtml(label)}</td>
        <td class="source-col">${escapeHtml(source)}</td>
        <td class="role-col">${escapeHtml(role)}</td>
        <td class="duration-col" title="End-to-end task duration (includes Agent B round-trip where applicable)">${durationStr}</td>
        <td class="preview-col"><span title="${escapeHtml(req.prompt || '')}">${escapeHtml(promptPreview)}</span></td>
        <td class="preview-col"><span title="${escapeHtml(req.response || '')}">${escapeHtml(responsePreview)}</span></td>
      </tr>
      <tr class="flow-detail-row" id="flow-detail-${req.seq}" style="display: none;">
        <td colspan="9" class="flow-detail-cell">
          <div class="flow-detail-section">
            <div class="flow-detail-label">Request (Prompt)</div>
            <div class="flow-detail-content">${escapeHtml(req.prompt || '(empty)')}</div>
          </div>
          <div class="flow-detail-section">
            <div class="flow-detail-label">Response</div>
            <div class="flow-detail-content">${escapeHtml(req.response || '(empty)')}</div>
          </div>
          ${req.request_id ? `<div class="flow-detail-section"><div class="flow-detail-label">LLM request ID</div><div class="flow-detail-content">${escapeHtml(req.request_id)}</div></div>` : ''}
          ${req.endpoint ? `<div class="flow-detail-section"><div class="flow-detail-label">Endpoint</div><div class="flow-detail-content">${escapeHtml(req.endpoint)}</div></div>` : ''}
          <div class="flow-detail-section">
            <div class="flow-detail-label">Full request JSON</div>
            <pre class="flow-detail-json">${reqJsonEscaped}</pre>
          </div>
        </td>
      </tr>
    `;
  }).join('');
  
  const tableHtml = `
    <table class="flow-table">
      <thead>
        <tr>
          <th class="seq-col">#</th>
          <th class="id-col">Req ID</th>
          <th class="stage-col">Stage</th>
          <th class="label-col">Label</th>
          <th class="source-col">Source</th>
          <th class="role-col">Role</th>
          <th class="duration-col" title="End-to-end task duration">Task duration</th>
          <th class="preview-col">Prompt (preview)</th>
          <th class="preview-col">Response (preview)</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>
    </table>
  `;
  
  const graphHtml = renderLlmRequestsGraph(requests);
  
  return `
    <div class="flow-view-toggle">
      <button id="flowViewGraphBtn" class="active" type="button" onclick="window.agentverse.setFlowView('graph')">Graph</button>
      <button id="flowViewTableBtn" type="button" onclick="window.agentverse.setFlowView('table')">Table</button>
    </div>
    <div id="flowGraphView">
      ${graphHtml}
    </div>
    <div id="flowTableView" style="display: none;">
      ${tableHtml}
    </div>
  `;
}
