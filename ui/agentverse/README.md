# AgentVerse UI

A modern, modular web interface for the AgentVerse multi-agent collaboration system.

## Features

- **Live Streaming Updates**: Real-time progress updates via Server-Sent Events (SSE)
- **Stage Visualization**: Visual workflow with 4 main stages
- **LLM Request Tracking**: Detailed view of all LLM requests and responses
- **Iteration History**: Track multiple iterations with scoring
- **Graph & Table Views**: Visualize agent communication flow

## Project Structure

```
ui/agentverse/
├── index.html              # Main HTML (monolithic, for compatibility)
├── index-modular.html      # Modular HTML (uses separate JS/CSS files)
├── css/
│   └── styles.css          # All CSS styles
├── js/
│   ├── app.js              # Main application entry point
│   ├── config.js           # Configuration and constants
│   ├── utils.js            # Utility functions
│   ├── renderers.js        # HTML rendering functions
│   ├── ui-state.js         # UI state management
│   └── streaming.js        # SSE streaming handler
└── README.md               # This file
```

## File Descriptions

### HTML Files

- **`index.html`**: Self-contained single-file version with all CSS and JavaScript inline. Use this for maximum compatibility and easier deployment.
- **`index-modular.html`**: Modern modular version that imports separate CSS and JS modules. Better for development and maintainability.

### CSS

- **`css/styles.css`**: All visual styles including layout, components, animations, and responsive design.

### JavaScript Modules

- **`js/app.js`**: Main application class that initializes the UI, binds event listeners, and coordinates other modules.

- **`js/config.js`**: Constants and configuration including:
  - Default settings
  - Example tasks
  - Timeouts and limits

- **`js/utils.js`**: Utility functions including:
  - HTML escaping
  - Text truncation
  - Color/badge mapping
  - Default endpoint generation

- **`js/renderers.js`**: Pure functions for rendering UI components:
  - Expert cards
  - Discussion rounds (horizontal/vertical)
  - Execution results
  - Evaluation metrics
  - LLM request table and graph
  - Iteration history

- **`js/ui-state.js`**: UIState class that manages:
  - UI element references
  - Timer functionality
  - Stage updates
  - Progress tracking
  - Reset/initialization

- **`js/streaming.js`**: StreamingHandler class that handles:
  - SSE connection and parsing
  - Real-time event processing
  - Error handling and fallback to non-streaming
  - Progress updates

## Live Streaming Features

The UI supports Server-Sent Events (SSE) for real-time updates:

### Event Types

- `iteration_start`: New iteration beginning
- `stage_start`: Stage starting (recruitment, decision, execution, evaluation, synthesis)
- `stage_complete`: Stage completed with results
- `llm_request`: Individual LLM request/response logged
- `discussion_round`: Discussion round completed (horizontal mode)
- `vertical_iteration`: Solver/reviewer iteration (vertical mode)
- `execution_result`: Individual agent execution completed
- `complete`: Workflow finished
- `error`: Error occurred

### Visual Indicators

- **LIVE badge**: Pulsing red badge when streaming is active
- **LLM Request Counter**: Shows number of LLM requests in real-time
- **Stage Progress**: Stages update from Pending → Running → Complete
- **Auto-expansion**: Relevant stages automatically expand as they complete
- **Progress Bar**: Smooth progress indication based on current stage

### Fallback Behavior

If streaming is not supported or fails:
1. Attempt streaming first
2. If streaming fails, automatically retry with non-streaming mode
3. Display clear error messages if both modes fail

## Usage

### Development

For development with hot-reload, use the modular version:

```html
<!-- In index-modular.html -->
<script type="module" src="js/app.js"></script>
```

### Production

For production deployment, use the self-contained version:

```html
<!-- index.html has everything inline -->
```

### Switching Between Versions

To use the modular version as default:

```bash
cd ui/agentverse
mv index.html index-monolithic.html
mv index-modular.html index.html
```

## API Integration

The UI communicates with Agent A's `/agentverse` endpoint:

### Request Format

```json
{
  "task": "Your task description",
  "max_iterations": 3,
  "stream": true
}
```

### Non-Streaming Response

```json
{
  "task_id": "...",
  "completed": true,
  "iterations": 1,
  "final_output": "...",
  "stages": { ... },
  "iteration_history": [...],
  "llm_requests": [...]
}
```

### Streaming Events

```
event: stage_start
data: {"stage": "recruitment", "stage_number": 1, "message": "..."}

event: llm_request
data: {"seq": 1, "prompt": "...", "response": "..."}

event: complete
data: {...full response...}
```

## Browser Compatibility

- **Streaming**: Requires modern browser with Fetch API and ReadableStream support
- **Fallback**: Automatically falls back to non-streaming for older browsers
- **Tested**: Chrome 90+, Firefox 88+, Safari 14+, Edge 90+

## Customization

### Adding New Event Types

1. Add handler in `js/streaming.js` → `handleStreamEvent()`
2. Emit event in backend `orchestrator.py` → `_send_progress()`
3. Add visual feedback in `js/ui-state.js` or `js/renderers.js`

### Styling

All styles are in `css/styles.css`. CSS variables in `:root` control the color scheme:

```css
:root {
  --primary: #2563eb;
  --success: #16a34a;
  --error: #dc2626;
  /* ... */
}
```

## Performance

- Minimal overhead per SSE event (~50-200 bytes)
- Events only sent at significant milestones
- Efficient incremental DOM updates
- No polling - server pushes updates

## Troubleshooting

### No response when clicking "Run"

1. Check browser console for errors
2. Verify Agent A endpoint is correct
3. Ensure agent-a service is running
4. Check CORS headers if accessing from different origin

### Streaming not working

1. Check if `stream: true` is sent in request
2. Verify browser supports ReadableStream
3. Check backend logs for SSE errors
4. UI will automatically fall back to non-streaming

### Elements not updating

1. Hard refresh browser (Ctrl+Shift+R)
2. Clear browser cache
3. Check console for JavaScript errors
4. Verify HTML elements have correct IDs
