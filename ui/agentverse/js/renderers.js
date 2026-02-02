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
 * Render iteration history
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
  
  let html = '<div class="iteration-tabs">';
  history.forEach((h, idx) => {
    const score = h.evaluation?.score || 0;
    const scoreIcon = score >= 70 ? '✓' : score >= 40 ? '~' : '✗';
    html += `<div class="iteration-tab">${scoreIcon} Iter ${idx + 1} (${score}/100)</div>`;
  });
  html += '</div>';
  
  html += '<div style="font-size: 13px; color: var(--text-secondary);">';
  history.forEach((h, idx) => {
    html += `<p><strong>Iteration ${idx + 1}:</strong> ${h.recruitment?.experts?.join(', ') || 'N/A'} | Duration: ${h.duration_seconds || 0}s</p>`;
  });
  html += '</div>';
  
  container.innerHTML = html;
}

/**
 * Render LLM requests graph as SVG
 */
export function renderLlmRequestsGraph(requests) {
  if (!requests || requests.length === 0) {
    return '<p style="color: var(--text-secondary);">No LLM requests recorded.</p>';
  }
  
  // Build vertical swimlanes: Agent A (orchestrator) + Agent B roles
  const orchestratorKey = 'Agent A (orchestrator)';
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
  
  const laneGapX = 140;
  const paddingX = 80;
  const paddingY = 40;
  const stepY = 60;
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
  requests.forEach((req, idx) => {
    const stage = req.stage || 'unknown';
    const label = req.label || '';
    const roleRaw = (req.agent_role || '').trim();
    const roleLower = roleRaw.toLowerCase();
    
    let fromKey;
    let toKey;
    if (!roleRaw || roleLower.includes('orchestrator')) {
      // Orchestrator self-directed LLM call
      fromKey = orchestratorKey;
      toKey = orchestratorKey;
    } else {
      const bLane = laneIndex[`Agent B – ${roleRaw}`] !== undefined
        ? `Agent B – ${roleRaw}`
        : laneOrder[1]; // fallback Agent B lane
      // Model the dependency as Agent B returning results to Agent A
      fromKey = bLane;
      toKey = orchestratorKey;
    }
    
    const x1 = paddingX + (laneIndex[fromKey] ?? 0) * laneGapX;
    const x2 = paddingX + (laneIndex[toKey] ?? 0) * laneGapX;
    const y = paddingY + idx * stepY;
    const midX = (x1 + x2) / 2;
    const color = getStageColor(stage);
    const title = `#${req.seq} ${stage} – ${label}`;
    
    svg += `
      <g>
        <title>${escapeHtml(title)}</title>
        <path
          class="flow-graph-edge"
          d="M ${x1} ${y} L ${x2} ${y}"
          stroke="${color}"
          marker-end="url(#arrowhead)"
        ></path>
        <circle
          class="flow-graph-node"
          cx="${midX}"
          cy="${y}"
          r="9"
          fill="${color}"
        ></circle>
        <text
          class="flow-graph-node-label"
          x="${midX}"
          y="${y + 3}"
          text-anchor="middle"
        >
          ${req.seq}
        </text>
      </g>
    `;
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
    
    return `
      <tr data-seq="${req.seq}" onclick="window.agentverse.toggleFlowRow(${req.seq})">
        <td class="seq-col">${req.seq}</td>
        <td class="stage-col"><span class="flow-stage-badge ${getStageBadgeClass(stage)}">${escapeHtml(stage)}</span></td>
        <td class="label-col">${escapeHtml(label)}</td>
        <td class="source-col">${escapeHtml(source)}</td>
        <td class="role-col">${escapeHtml(role)}</td>
        <td class="preview-col"><span title="${escapeHtml(req.prompt || '')}">${escapeHtml(promptPreview)}</span></td>
        <td class="preview-col"><span title="${escapeHtml(req.response || '')}">${escapeHtml(responsePreview)}</span></td>
      </tr>
      <tr class="flow-detail-row" id="flow-detail-${req.seq}" style="display: none;">
        <td colspan="7" class="flow-detail-cell">
          <div class="flow-detail-section">
            <div class="flow-detail-label">Request (Prompt)</div>
            <div class="flow-detail-content">${escapeHtml(req.prompt || '(empty)')}</div>
          </div>
          <div class="flow-detail-section">
            <div class="flow-detail-label">Response</div>
            <div class="flow-detail-content">${escapeHtml(req.response || '(empty)')}</div>
          </div>
          ${req.endpoint ? `<div class="flow-detail-section"><div class="flow-detail-label">Endpoint</div><div class="flow-detail-content">${escapeHtml(req.endpoint)}</div></div>` : ''}
        </td>
      </tr>
    `;
  }).join('');
  
  const tableHtml = `
    <table class="flow-table">
      <thead>
        <tr>
          <th class="seq-col">#</th>
          <th class="stage-col">Stage</th>
          <th class="label-col">Label</th>
          <th class="source-col">Source</th>
          <th class="role-col">Role</th>
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
