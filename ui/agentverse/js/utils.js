/**
 * Utility Functions
 */

import { CONFIG } from './config.js';

/**
 * Escape HTML to prevent XSS
 */
export function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Truncate text to a maximum length
 */
export function truncate(text, maxLen = CONFIG.TRUNCATE_LENGTH) {
  if (!text || text.length <= maxLen) return text;
  return text.substring(0, maxLen) + '...';
}

/**
 * Get stage badge CSS class
 */
export function getStageBadgeClass(stage) {
  const map = {
    recruitment: 'flow-stage-recruitment',
    decision: 'flow-stage-decision',
    execution: 'flow-stage-execution',
    evaluation: 'flow-stage-evaluation',
    synthesis: 'flow-stage-synthesis'
  };
  return map[stage] || 'flow-stage-recruitment';
}

/**
 * Get stage color for graph edges
 */
export function getStageColor(stage) {
  const map = {
    recruitment: '#1e40af',
    decision: '#4c1d95',
    execution: '#166534',
    evaluation: '#9a3412',
    synthesis: '#0e7490',
    unknown: '#6366f1'
  };
  return map[stage] || map.unknown;
}

/**
 * Initialize endpoint URL based on current location
 */
export function getDefaultEndpoint() {
  const protocol = window.location.protocol === 'file:' ? 'http:' : window.location.protocol;
  const host = window.location.hostname || 'localhost';
  return `${protocol}//${host}:8101/agentverse`;
}
