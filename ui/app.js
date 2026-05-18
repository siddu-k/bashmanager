/* ═══════════════════════════════════════════════════════════
   DevShell — Frontend Logic (CLI + Analysis + Locks + GitHub)
   ═══════════════════════════════════════════════════════════ */

const MAX_NOTIFICATIONS = 5;
const NOTIFICATION_DURATION = 5000;

const API = {
    scripts: '/api/scripts',
    content: '/api/scripts/content',
    run: '/api/scripts/run',
    save: '/api/scripts/save',
    delete: '/api/scripts/delete',
    favorite: '/api/scripts/favorite',
    exec: '/api/exec',
    lock: '/api/scripts/lock',
    import_github: '/api/scripts/import_github',
    pr: '/api/git/pr',
    history: '/api/history',
    history_export: '/api/history/export',
    kill: '/api/scripts/kill',
};

// ─── State ────────────────────────────────────────────────
let state = {
    scripts: {},
    activeScript: null,
    expandedCategories: new Set(),
    searchQuery: '',
    cmdHistory: [],
    cmdHistoryIndex: -1,
    historyQuery: '',
    historyFilter: 'all',
    historyEntries: [],
    historySummary: { total: 0, failed: 0, successful: 0, scripts: 0, commands: 0 },
    unlockedScripts: {}, // stores valid passwords for locked scripts: { "path": "pass" }
    terminals: [1],      // list of terminal IDs
    activeTerminalId: 1,
    nextTerminalId: 2,
    runningScripts: {},   // { termId: { run_id } }
};

const RUN_BUTTON_IDLE_HTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg><span>Run</span>`;

// ─── SVG Icons ─────────────────────────────────────────────
const ICONS = {
    docker: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/></svg>`,
    git: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><path d="M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9"/><path d="M12 12v3"/></svg>`,
    linux: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/></svg>`,
    network: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" x2="22" y1="12" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`,
    database: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/></svg>`,
    deploy: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2-2 4h4z"/><path d="M5 22h14l-2.7-8.1C15.8 12.4 14.1 11 12 11s-3.8 1.4-4.3 3.9z"/><path d="m9 11-2-6h10l-2 6"/></svg>`,
    monitor: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/><path d="M6 10h12"/></svg>`,
    backup: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>`,
    security: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`,
    cloud: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/></svg>`,
    default: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>`,
    script: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m18 16 4-4-4-4"/><path d="m6 8-4 4 4 4"/><path d="m14.5 4-5 16"/></svg>`,
    favorite: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>`,
    lock: `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`,
};

function getCategoryIcon(name) {
    return ICONS[name.toLowerCase()] || ICONS.default;
}

// ─── Init ──────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    loadScripts();
    bindEvents();
    initResizers();

    // Replace the execute icon inside the CLI input bar to a more standard 'Enter' icon
    const runCmdBtn = document.getElementById('btn-run-cmd');
    if (runCmdBtn) {
        runCmdBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>`;
    }
});

// ─── API Calls ─────────────────────────────────────────────

function initResizers() {
    const resizerLeft = document.getElementById('resizer-left');
    const sidebar = document.getElementById('sidebar');
    let isResizingLeft = false;

    if (resizerLeft) {
        resizerLeft.addEventListener('mousedown', (e) => {
            isResizingLeft = true;
            document.body.style.cursor = 'col-resize';
            resizerLeft.classList.add('resizing');
        });
    }

    const resizerRight = document.getElementById('resizer-right');
    const rightPanel = document.getElementById('analysis-panel');
    let isResizingRight = false;

    if (resizerRight) {
        resizerRight.addEventListener('mousedown', (e) => {
            isResizingRight = true;
            document.body.style.cursor = 'col-resize';
            resizerRight.classList.add('resizing');
        });
    }

    document.addEventListener('mousemove', (e) => {
        if (!isResizingLeft && !isResizingRight) return;

        if (isResizingLeft) {
            let newWidth = e.clientX;
            if (newWidth < 150) newWidth = 150;
            if (newWidth > 600) newWidth = 600;
            sidebar.style.width = newWidth + 'px';
            sidebar.style.minWidth = newWidth + 'px';
            document.documentElement.style.setProperty('--sidebar-width', newWidth + 'px');
        }
        else if (isResizingRight) {
            let newWidth = document.body.clientWidth - e.clientX;
            if (newWidth < 250) newWidth = 250;
            if (newWidth > 800) newWidth = 800;
            rightPanel.style.width = newWidth + 'px';
            rightPanel.style.minWidth = newWidth + 'px';
            document.documentElement.style.setProperty('--analysis-width', newWidth + 'px');
        }
    });

    document.addEventListener('mouseup', () => {
        if (isResizingLeft) {
            isResizingLeft = false;
            if (resizerLeft) resizerLeft.classList.remove('resizing');
        }
        if (isResizingRight) {
            isResizingRight = false;
            if (resizerRight) resizerRight.classList.remove('resizing');
        }
        document.body.style.cursor = 'default';
    });
}

async function loadScripts() {
    try {
        const res = await fetch(API.scripts);
        state.scripts = await res.json();
        renderSidebar();
        renderWelcomeStats();
    } catch (err) {
        console.error('Failed to load scripts:', err);
        notify(`Failed to load scripts: ${err.message}`, 'error');
    }
}

async function fetchScriptContent(relPath, password = '') {
    try {
        const res = await fetch(API.content, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: relPath, password: password })
        });
        const data = await res.json();
        if (res.status === 401) {
            return { error: 'Locked', locked: true };
        }
        return data.content || '';
    } catch (err) {
        console.error('Failed to load script content:', err);
        notify(`Failed to load script content: ${err.message}`, 'error');
        return { error: err.message };
    }
}

function getTerminalBody(termId = state.activeTerminalId) {
    return document.getElementById(`terminal-body-${termId}`)
        || (termId === 1 ? document.getElementById('terminal-body') : null);
}

function updateRunButton() {
    const btnRun = document.getElementById('btn-run');
    if (!btnRun) return;

    const running = state.runningScripts[state.activeTerminalId];
    btnRun.classList.remove('running', 'abort', 'aborting');

    if (running) {
        btnRun.classList.add(running.aborting ? 'aborting' : 'abort');
        btnRun.innerHTML = running.aborting
            ? '<span style="margin-right: 6px;">x</span> Aborting...'
            : '<span style="margin-right: 6px;">x</span> Abort';
        btnRun.title = running.aborting ? 'Aborting script' : 'Abort script';
        btnRun.setAttribute('aria-label', running.aborting ? 'Aborting script' : 'Abort script');
    } else {
        btnRun.innerHTML = RUN_BUTTON_IDLE_HTML;
        btnRun.title = 'Run Script';
        btnRun.setAttribute('aria-label', 'Run script');
    }
}

async function abortScriptRun(termId = state.activeTerminalId) {
    const running = state.runningScripts[termId];
    if (!running) return;

    running.abortRequested = true;
    running.aborting = true;
    updateRunButton();

    if (!running.run_id || running.killSent) return;

    running.killSent = true;
    try {
        const res = await fetch(API.kill, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ run_id: running.run_id }),
        });

        if (!res.ok && res.status !== 404) {
            const data = await res.json().catch(() => ({}));
            notify(data.error || 'Failed to abort script.', 'error');
        }
    } catch (e) {
        notify(`Failed to abort script: ${e.message}`, 'error');
    }
}

async function runScript(relPath) {
    const termId = state.activeTerminalId;
    if (state.runningScripts[termId]) return;
    const runStatus = document.getElementById('run-status');
    const resourcePanel = document.getElementById('resource-panel');

    let runId = null;

    state.runningScripts[termId] = {
        run_id: null,
        relPath,
        abortRequested: false,
        aborting: false,
        killSent: false,
    };
    updateRunButton();

    if (termId === state.activeTerminalId) {
        runStatus.textContent = 'Executing...';
        runStatus.className = 'run-status running';
        resourcePanel.style.display = 'none';
    }

    appendToCli(`$ Running script: ${relPath}`, 'system', termId);
    if (typeof DebuggerConsole !== 'undefined') DebuggerConsole.addEntry('info', `▶ Running script: ${relPath}`, 'script');

    try {
        const res = await fetch(API.run, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: relPath, password: state.unlockedScripts[relPath] || '' }),
        });

        if (res.status === 401) {
            appendToCli('Error: Script depends on a lock sequence. Unauthorized.', 'error', termId);
            if (typeof DebuggerConsole !== 'undefined') DebuggerConsole.addEntry('error', 'Script is locked — unauthorized', 'script');
            if (termId === state.activeTerminalId) {
                runStatus.textContent = 'Locked';
                runStatus.className = 'run-status error';
            }
            return;
        }

        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.error || `Script run failed with HTTP ${res.status}`);
        }

        if (!res.body) {
            throw new Error('Script run did not return a stream');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            let eolIndex;
            while ((eolIndex = buffer.indexOf('\n\n')) >= 0) {
                const chunk = buffer.slice(0, eolIndex).trim();
                buffer = buffer.slice(eolIndex + 2);

                if (chunk.startsWith('data: ')) {
                    try {
                        const data = JSON.parse(chunk.substring(6));

                        if (data.type === 'started') {
                            runId = data.run_id;
                            const running = state.runningScripts[termId];
                            if (running) {
                                running.run_id = runId;
                                if (running.abortRequested) abortScriptRun(termId);
                            }
                            updateRunButton();
                            appendToCli(data.content, 'system', termId);
                        } else if (data.type === 'stdout' || data.type === 'error' || data.type === 'system') {
                            let cssClass = data.type === 'stdout' ? 'stdout' : (data.type === 'system' ? 'system' : 'stderr');
                            appendToCli(data.content, cssClass, termId);
                            if (typeof DebuggerConsole !== 'undefined') {
                                const dbgType = data.type === 'error' ? 'error' : 'log';
                                DebuggerConsole.addEntry(dbgType, data.content.trimEnd(), relPath);
                            }
                        } else if (data.type === 'aborted') {
                            appendToCli(data.content, 'error', termId);
                            if (typeof DebuggerConsole !== 'undefined') {
                                DebuggerConsole.addEntry('error', `⏹ Script aborted (ID: ${data.run_id})`, 'script');
                            }
                            if (termId === state.activeTerminalId) {
                                runStatus.textContent = 'Aborted';
                                runStatus.className = 'run-status error';
                            }
                        } else if (data.type === 'metrics') {
                            if (data.success) {
                                appendToCli(`Script completed (Exit code: ${data.exit_code})`, 'success', termId);
                                if (typeof DebuggerConsole !== 'undefined') {
                                    DebuggerConsole.addEntry('info', `✓ Script completed — exit code: ${data.exit_code} | time: ${data.resources?.execution_time_formatted || ''} | cpu: ${data.resources?.cpu_percent || 0}% | mem: ${data.resources?.memory_used_mb || 0}MB`, 'metrics');
                                }
                                if (termId === state.activeTerminalId) {
                                    runStatus.textContent = 'Success';
                                    runStatus.className = 'run-status success';
                                }
                            } else {
                                appendToCli(`Script failed (Exit code: ${data.exit_code})`, 'error', termId);
                                if (typeof DebuggerConsole !== 'undefined') {
                                    DebuggerConsole.addEntry('error', `✗ Script failed — exit code: ${data.exit_code}`, 'metrics');
                                }
                                if (termId === state.activeTerminalId) {
                                    runStatus.textContent = 'Failed';
                                    runStatus.className = 'run-status error';
                                }
                            }

                            if (data.resources && Object.keys(data.resources).length > 0) {
                                if (termId === state.activeTerminalId) {
                                    renderResources(data.resources);
                                    resourcePanel.style.display = '';
                                }
                            }
                        }
                    } catch (e) { }
                }
            }
        }
    } catch (err) {
        const abortRequested = Boolean(state.runningScripts[termId]?.abortRequested);
        if (!abortRequested) {
            appendToCli(`Error executing script: ${err.message}`, 'stderr', termId);
            if (typeof DebuggerConsole !== 'undefined') DebuggerConsole.addEntry('error', `Script error: ${err.message}`, 'script');
            if (termId === state.activeTerminalId) {
                runStatus.textContent = 'Error';
                runStatus.className = 'run-status error';
            }
        }
    } finally {
        refreshExecutionHistoryIfVisible();
        delete state.runningScripts[termId];
        updateRunButton();
    }
}

async function execCommand(cmd) {
    if (!cmd.trim()) return;
    const termId = state.activeTerminalId;

    state.cmdHistory.push(cmd);
    state.cmdHistoryIndex = state.cmdHistory.length;
    appendToCli(`$ ${cmd}`, 'cmd-line', termId);
    // Mirror command to debugger
    if (typeof DebuggerConsole !== 'undefined') DebuggerConsole.addEntry('info', `$ ${cmd}`, 'terminal');

    try {
        const res = await fetch(API.exec, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: cmd }),
        });

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            let eolIndex;
            while ((eolIndex = buffer.indexOf('\n\n')) >= 0) {
                const chunk = buffer.slice(0, eolIndex).trim();
                buffer = buffer.slice(eolIndex + 2);

                if (chunk.startsWith('data: ')) {
                    try {
                        const data = JSON.parse(chunk.substring(6));
                        if (data.type === 'stdout' || data.type === 'error') {
                            appendToCli(data.content, data.type === 'stdout' ? 'stdout' : 'stderr', termId);
                            // Mirror to debugger
                            if (typeof DebuggerConsole !== 'undefined') {
                                DebuggerConsole.addEntry(data.type === 'error' ? 'error' : 'log', data.content.trimEnd(), 'terminal');
                            }
                        } else if (data.type === 'metrics') {
                            if (!data.success) {
                                appendToCli(`Command failed (Exit code: ${data.exit_code})`, 'stderr', termId);
                                if (typeof DebuggerConsole !== 'undefined') DebuggerConsole.addEntry('error', `Command failed — exit code: ${data.exit_code}`, 'terminal');
                            }
                        }
                    } catch (e) { }
                }
            }
        }
    } catch (err) {
        appendToCli(`Error executing command: ${err.message}`, 'stderr', termId);
        if (typeof DebuggerConsole !== 'undefined') DebuggerConsole.addEntry('error', `Command error: ${err.message}`, 'terminal');
    } finally {
        refreshExecutionHistoryIfVisible();
    }
}

async function loadExecutionHistory(query = '', filter = 'all', limit = 200) {
    const params = new URLSearchParams();
    if (query) params.set('q', query);
    if (filter === 'failed') {
        params.set('status', 'failed');
    } else if (filter === 'command' || filter === 'script') {
        params.set('kind', filter);
    }
    params.set('limit', String(limit));

    const res = await fetch(`${API.history}?${params.toString()}`);
    return res.json();
}

function formatHistoryDuration(entry) {
    if (entry.duration) return entry.duration;
    if (typeof entry.duration_seconds !== 'number') return '';
    const seconds = entry.duration_seconds;
    if (seconds < 60) return `${seconds.toFixed(2)}s`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes}m ${(seconds % 60).toFixed(1)}s`;
}

function renderHistorySummary(summary = {}) {
    const summaryEl = document.getElementById('history-summary');
    if (!summaryEl) return;
    summaryEl.innerHTML = [
        `<span class="history-summary-item">Total <strong>${summary.total || 0}</strong></span>`,
        `<span class="history-summary-item">Successful <strong>${summary.successful || 0}</strong></span>`,
        `<span class="history-summary-item failed">Failed <strong>${summary.failed || 0}</strong></span>`,
        `<span class="history-summary-item">Scripts <strong>${summary.scripts || 0}</strong></span>`,
        `<span class="history-summary-item">Commands <strong>${summary.commands || 0}</strong></span>`,
    ].join('');
}

function renderHistoryEntries(entries = []) {
    const list = document.getElementById('history-list');
    if (!list) return;

    if (!entries.length) {
        list.innerHTML = '<div class="history-empty-state">No execution history matches the current search.</div>';
        return;
    }

    list.innerHTML = entries.map(entry => {
        const statusClass = entry.status === 'failed' ? 'failed' : 'success';
        const kindLabel = entry.kind === 'script' ? 'Script' : 'Command';
        const duration = formatHistoryDuration(entry);
        const excerpt = entry.output_excerpt ? escapeHtml(entry.output_excerpt).replace(/\n/g, '<br>') : '<span class="history-excerpt-empty">No output captured.</span>';
        return `
            <article class="history-entry ${statusClass}">
                <div class="history-entry-head">
                    <div class="history-entry-title-row">
                        <span class="history-entry-status ${statusClass}">${entry.status}</span>
                        <span class="history-entry-kind">${kindLabel}</span>
                        <span class="history-entry-time">${escapeHtml(entry.started_at || '')}</span>
                    </div>
                    <div class="history-entry-meta">
                        <span>ID ${escapeHtml(entry.id || '')}</span>
                        ${duration ? `<span>${escapeHtml(duration)}</span>` : ''}
                        ${entry.exit_code !== null && entry.exit_code !== undefined ? `<span>Exit ${escapeHtml(String(entry.exit_code))}</span>` : ''}
                        <button class="btn btn-action history-log-link" data-log-file="${escapeAttr(entry.log_file || '')}">Open log</button>
                    </div>
                </div>
                <div class="history-entry-command">${escapeHtml(entry.command || entry.display_name || '')}</div>
                ${entry.error ? `<div class="history-entry-error">${escapeHtml(entry.error)}</div>` : ''}
                <div class="history-entry-excerpt">${excerpt}</div>
            </article>
        `;
    }).join('');

    list.querySelectorAll('.history-log-link').forEach(button => {
        button.addEventListener('click', () => {
            const fileName = button.dataset.logFile;
            if (!fileName) return;
            window.open(`/logs/executions/${encodeURIComponent(fileName)}`, '_blank', 'noopener,noreferrer');
        });
    });
}

async function refreshExecutionHistoryIfVisible() {
    const overlay = document.getElementById('history-modal-overlay');
    if (!overlay || !overlay.classList.contains('active')) return;
    await refreshExecutionHistory();
}

async function refreshExecutionHistory() {
    const historyInput = document.getElementById('history-search');
    const query = historyInput ? historyInput.value.trim() : state.historyQuery;
    const activeFilterButton = document.querySelector('.history-filter.active');
    const filter = activeFilterButton ? activeFilterButton.dataset.historyFilter : state.historyFilter;

    state.historyQuery = query;
    state.historyFilter = filter;

    const payload = await loadExecutionHistory(query, filter);
    state.historyEntries = payload.entries || [];
    state.historySummary = payload.summary || state.historySummary;
    renderHistorySummary(state.historySummary);
    renderHistoryEntries(state.historyEntries);
}

function openHistoryViewer() {
    const overlay = document.getElementById('history-modal-overlay');
    if (!overlay) return;
    overlay.classList.add('active');
    refreshExecutionHistory();
}

function closeHistoryViewer() {
    const overlay = document.getElementById('history-modal-overlay');
    if (!overlay) return;
    overlay.classList.remove('active');
}

async function exportExecutionHistory(format = 'log') {
    const params = new URLSearchParams();
    if (state.historyQuery) params.set('q', state.historyQuery);
    if (state.historyFilter === 'failed') {
        params.set('status', 'failed');
    } else if (state.historyFilter === 'command' || state.historyFilter === 'script') {
        params.set('kind', state.historyFilter);
    }
    params.set('format', format);

    const res = await fetch(`${API.history_export}?${params.toString()}`);
    const text = await res.text();
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `devshell-history.${format === 'txt' ? 'txt' : 'log'}`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
}

async function saveScript(category, filename, content) {
    try {
        const relPath = `${category}/${filename}`.replace(/\/+/g, '/'); // simple guess
        const res = await fetch(API.save, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ category, filename, content, password: state.unlockedScripts[relPath] || '' }),
        });
        const data = await res.json();

        if (res.status === 401) {
            notify('Cannot save: Script is locked.', 'warning');
            return;
        }

        if (data.success) {
            await loadScripts();
            closeModal();
            selectScript(data.path);
            notify('Script saved successfully.', 'success');
        }
    } catch (err) {
        console.error('Failed to save script:', err);
        notify(`Failed to save script: ${err.message}`, 'error');
    }
}

async function deleteScript(relPath) {
    if (!confirm('Are you sure you want to delete this script permanently?')) return;
    try {
        const res = await fetch(API.delete, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: relPath, password: state.unlockedScripts[relPath] || '' })
        });
        const data = await res.json();
        if (res.status === 401) {
            notify('This script is locked. Unlock it first to delete.', 'warning');
            return;
        }

        state.activeScript = null;
        showWelcome();
        await loadScripts();
        notify('Script deleted successfully.', 'success');
    } catch (err) {
        console.error('Failed to delete script:', err);
        notify(`Failed to delete script: ${err.message}`, 'error');
    }
}

async function toggleFavorite(relPath) {
    try {
        const res = await fetch(API.favorite, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: relPath }),
        });
        const data = await res.json();
        await loadScripts();

        if (state.activeScript === relPath) {
            const btnFav = document.getElementById('btn-fav');
            if (btnFav) btnFav.classList.toggle('active', data.favorite);
        }
        notify(
            data.favorite
                ? 'Added to favorites.'
                : 'Removed from favorites.',
            'success'
        );
    } catch (err) {
        console.error('Failed to toggle favorite:', err);
        notify(`Failed to update favorite: ${err.message}`, 'error');
    }
}

async function importGithubScript(url, category, filename) {
    try {
        const res = await fetch(API.import_github, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, category, filename }),
        });
        const data = await res.json();

        if (res.status === 401) {
            notify('File already exists and is locked.', 'warning');
            return;
        }

        if (data.success) {
            await loadScripts();
            document.getElementById('github-modal-overlay').classList.remove('active');
            selectScript(data.path);
            appendToCli(`✓ Imported script from GitHub: ${data.path}`, 'success');
            notify('Script imported successfully.', 'success');
        } else {
            notify(`Import failed: ${data.error}`, 'error');
        }
    } catch (err) {
        console.error('Import error:', err);
        notify(`Exception during import: ${err.message}`, 'error');
    }
}

// --- NEW FEATURE: Pull Request / Git Push Workflow ---

// 1. Opens the custom PR modal and populates default branch/message values
function raisePRFlow(relPath) {
    const overlay = document.getElementById('pr-modal-overlay');
    if (!overlay) return;

    // Set default values based on script path to speed up workflow
    const defaultBranch = `contrib-${relPath.replace(/\//g, '-').replace('.sh', '')}`;
    const defaultMsg = `Update/Add script: ${relPath}`;

    document.getElementById('pr-branch').value = defaultBranch;
    document.getElementById('pr-message').value = defaultMsg;

    overlay.classList.add('active');
}

// 2. Executes the API call to the backend after the modal is submitted
async function executePR(relPath, branch, message, repoUrl) {
    // Hide the modal immediately
    document.getElementById('pr-modal-overlay').classList.remove('active');

    // Automatically toggle the Debugger Console to show progress logs to the user
    if (typeof DebuggerConsole !== 'undefined') {
        DebuggerConsole.toggle();
        DebuggerConsole.addEntry('info', `🚀 Starting Git PR workflow for: ${relPath}`, 'git');
        if (repoUrl) DebuggerConsole.addEntry('info', `   Target Repo: ${repoUrl}`, 'git');
        DebuggerConsole.addEntry('info', `   Branch: ${branch}`, 'git');
        DebuggerConsole.addEntry('info', `   Message: ${message}`, 'git');
        DebuggerConsole.addEntry('info', `Running git operations in backend...`, 'git');
    }

    try {
        // Call the backend API with the branch, message, and the optional target repo
        const res = await fetch(API.pr, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: relPath, branch, message, target_repo: repoUrl }),
        });
        const data = await res.json();

        if (data.success) {
            if (typeof DebuggerConsole !== 'undefined') {
                DebuggerConsole.addEntry('log', `✨ Git operation successful!`, 'git');
                DebuggerConsole.addEntry('log', `🔗 PR Link: ${data.pr_url}`, 'git');
            }
            appendToCli(`✓ Git PR branch '${data.branch}' created and pushed.`, 'success');

            // Offer to automatically open the GitHub Pull Request page
            if (confirm(`Successfully pushed to branch '${data.branch}'.\n\nWould you like to open the Pull Request page on GitHub?`)) {
                window.open(data.pr_url, '_blank');
            }
        } else {
            if (typeof DebuggerConsole !== 'undefined') DebuggerConsole.addEntry('error', `❌ Git PR failed: ${data.error}`, 'git');
            notify(`PR workflow failed: ${data.error}`, 'error');
        }
    } catch (err) {
        if (typeof DebuggerConsole !== 'undefined') DebuggerConsole.addEntry('error', `❌ Git PR Exception: ${err.message}`, 'git');
        notify(`Exception during PR workflow: ${err.message}`, 'error');
    }
}

async function manageLock(relPath, oldPass, newPass) {
    try {
        const res = await fetch(API.lock, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: relPath, old_password: oldPass, new_password: newPass }),
        });
        const data = await res.json();

        if (!data.success) {
            notify(`Lock operation failed: ${data.error}`, 'error');
            return false;
        }

        if (data.locked) {
            state.unlockedScripts[relPath] = newPass; // update session cache
        } else {
            delete state.unlockedScripts[relPath]; // unlocked completely
        }

        await loadScripts();
        return true;
    } catch (err) {
        console.error(err);
        return false;
    }
}


// ─── CLI Helpers ───

function appendToCli(text, className = '', termId = state.activeTerminalId) {
    const termBody = getTerminalBody(termId);
    if (!termBody) return;

    const welcomeEl = termBody.querySelector('.cli-welcome');
    if (welcomeEl) welcomeEl.remove();

    const line = document.createElement('div');
    line.className = `cli-output-block ${className}`;
    line.textContent = text;
    termBody.appendChild(line);

    termBody.scrollTop = termBody.scrollHeight;
    highlightTerminalSearch();
}

function clearCli() {
    const termBody = getTerminalBody(state.activeTerminalId);
    if (termBody) {
        termBody.innerHTML = '<div class="cli-welcome"><span class="cli-prompt">$</span> <span class="cli-welcome-text">Terminal cleared.</span></div>';
    }
    document.getElementById('run-status').textContent = '';
    document.getElementById('run-status').className = 'run-status';
    document.getElementById('resource-panel').style.display = 'none';
}

// ─── Terminal Tabs ───

function addTerminal() {
    const id = state.nextTerminalId++;
    state.terminals.push(id);

    const tabsContainer = document.getElementById('cli-tabs');
    const tabBtn = document.createElement('div');
    tabBtn.className = 'cli-tab';
    tabBtn.id = `tab-btn-${id}`;
    tabBtn.innerHTML = `
        <span class="cli-dots" style="margin-right: 6px;">
            <span class="dot dot-red"></span>
            <span class="dot dot-yellow"></span>
            <span class="dot dot-green"></span>
        </span>
        <span>Terminal ${id}</span> 
        <button class="cli-tab-close" title="Close" aria-label="Close terminal" onclick="event.stopPropagation(); closeTerminal(${id})"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>`;
    tabBtn.onclick = () => switchTerminal(id);
    tabsContainer.insertBefore(tabBtn, document.getElementById('btn-add-tab'));

    const bodyContainer = document.createElement('div');
    bodyContainer.className = 'cli-body';
    bodyContainer.setAttribute('role', 'log');
    bodyContainer.setAttribute('aria-live', 'polite');
    bodyContainer.id = `terminal-body-${id}`;
    bodyContainer.style.display = 'none';
    bodyContainer.innerHTML = '<div class="cli-welcome"><span class="cli-prompt">$</span> <span class="cli-welcome-text">Terminal ready.</span></div>';

    document.getElementById('cli-area').insertBefore(bodyContainer, document.querySelector('.cli-input-bar'));
    switchTerminal(id);
}

function switchTerminal(id) {
    state.activeTerminalId = id;

    document.querySelectorAll('.cli-tab').forEach(t => t.classList.remove('active'));
    const activeTab = document.getElementById(`tab-btn-${id}`) || document.querySelector(`.cli-tab[data-id="${id}"]`);
    if (activeTab) activeTab.classList.add('active');

    document.querySelectorAll('.cli-body').forEach(b => b.style.display = 'none');
    const activeBody = getTerminalBody(id);
    if (activeBody) activeBody.style.display = 'block';

    const runStatus = document.getElementById('run-status');
    const resourcePanel = document.getElementById('resource-panel');
    const running = state.runningScripts[id];
    if (running) {
        runStatus.textContent = running.aborting ? 'Aborting...' : 'Executing...';
        runStatus.className = 'run-status running';
        resourcePanel.style.display = 'none';
    } else {
        runStatus.textContent = '';
        runStatus.className = 'run-status';
    }
    updateRunButton();
    highlightTerminalSearch();
}

function closeTerminal(id) {
    if (state.terminals.length <= 1) return;

    state.terminals = state.terminals.filter(t => t !== id);
    const tabBtn = document.getElementById(`tab-btn-${id}`) || document.querySelector(`.cli-tab[data-id="${id}"]`);
    if (tabBtn) tabBtn.remove();

    const bodyContainer = getTerminalBody(id);
    if (bodyContainer) bodyContainer.remove();

    if (state.activeTerminalId === id) {
        switchTerminal(state.terminals[state.terminals.length - 1]);
    }
}

// ─── Terminal Search Highlight ───

function highlightTerminalSearch() {
    const searchInput = document.getElementById('cli-search-input');
    if (!searchInput) return;
    const query = searchInput.value;
    const termId = state.activeTerminalId;
    const body = document.getElementById(`terminal-body-${termId}`) || (termId === 1 ? document.getElementById('terminal-body') : null);
    if (!body) return;

    const resultsSpan = document.getElementById('cli-search-results');

    // Remove old highlights
    const marks = body.querySelectorAll('mark.highlight');
    marks.forEach(m => {
        const parent = m.parentNode;
        parent.replaceChild(document.createTextNode(m.textContent), m);
        parent.normalize();
    });

    if (!query) {
        if (resultsSpan) resultsSpan.textContent = '';
        return;
    }

    let matchCount = 0;
    const regex = new RegExp(`(${query.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')})`, 'gi');

    const treeWalker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, null, false);
    const nodesToReplace = [];

    while (treeWalker.nextNode()) {
        const node = treeWalker.currentNode;
        if (node.parentNode.tagName === 'MARK') continue;
        if (regex.test(node.nodeValue)) {
            nodesToReplace.push(node);
        }
    }

    nodesToReplace.forEach(node => {
        const fragment = document.createDocumentFragment();
        const parts = node.nodeValue.split(regex);

        parts.forEach(part => {
            if (part !== '') {
                if (part.toLowerCase() === query.toLowerCase()) {
                    matchCount++;
                    const mark = document.createElement('mark');
                    mark.className = 'highlight';
                    mark.textContent = part;
                    fragment.appendChild(mark);
                } else {
                    fragment.appendChild(document.createTextNode(part));
                }
            }
        });
        node.parentNode.replaceChild(fragment, node);
    });

    if (resultsSpan) resultsSpan.textContent = matchCount > 0 ? `${matchCount} found` : 'No matches';
}


// ─── Rendering ─────────────────────────────────────────────

function renderSidebar() {
    const tree = document.getElementById('category-tree');
    const countEl = document.getElementById('script-count');
    const favsSection = document.getElementById('favorites-section');
    const favsList = document.getElementById('favorites-list');

    let totalScripts = 0;
    let favScripts = [];
    let html = '';

    const query = state.searchQuery.toLowerCase();

    for (const [cat, scripts] of Object.entries(state.scripts)) {
        const filteredScripts = query
            ? scripts.filter(s =>
                s.name.toLowerCase().includes(query) ||
                (s.desc && s.desc.toLowerCase().includes(query)) ||
                (s.tag && s.tag.toLowerCase().includes(query)) ||
                s.file.toLowerCase().includes(query))
            : scripts;

        if (filteredScripts.length === 0) continue;

        totalScripts += filteredScripts.length;
        const isExpanded = state.expandedCategories.has(cat) || !!query;

        html += `
            <div class="category-header" role="button" tabindex="0" aria-expanded="${isExpanded}" onclick="toggleCategory('${cat}')" onkeydown="handleKeyboardAction(event, () => toggleCategory('${cat}'))">
                <div class="category-header" onclick="toggleCategory('${cat}')">
                    <span class="category-arrow ${isExpanded ? 'expanded' : ''}">
                        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
                    </span>
                    <span class="category-icon">${getCategoryIcon(cat)}</span>
                    <span class="category-name">${cat}</span>
                    <span class="category-count">${filteredScripts.length}</span>
                </div>
                <ul class="script-list ${isExpanded ? '' : 'collapsed'}" style="max-height: ${filteredScripts.length * 44}px;">
                    ${filteredScripts.map(s => {
            let lockIcon = s.locked ? `<span class="script-item-icon" style="color: var(--accent-orange); margin-right: 4px;">${ICONS.lock}</span>` : '';

            return `
                        <li class="script-item ${state.activeScript === s.relative_path ? 'active' : ''}" role="button" tabindex="0"
                            onclick="selectScript('${s.relative_path}')"
                            onkeydown="handleKeyboardAction(event, () => selectScript('${s.relative_path}'))"
                            title="${escapeAttr(s.desc)}">
                            ${lockIcon}
                            <span class="script-item-icon" style="${s.locked ? 'display:none;' : ''}">${ICONS.script}</span>
                            <span class="script-item-name">${escapeHtml(s.name)}</span>
                            <span class="script-item-fav ${s.favorite ? 'visible' : ''}"
                                  onclick="event.stopPropagation(); toggleFavorite('${s.relative_path}')">
                                ${ICONS.favorite}
                            </span>
                        </li>
                    `}).join('')}
                </ul>
            </div>
        `;

        // Populate favs
        scripts.forEach(s => { if (s.favorite) favScripts.push(s); });
    }

    tree.innerHTML = html || '<div style="padding: 24px; text-align: center; color: var(--text-muted); font-size: 13px;">No scripts found. Create one to get started.</div>';
    countEl.textContent = totalScripts;

    if (favScripts.length > 0) {
        favsSection.style.display = '';
        favsList.innerHTML = favScripts.map(s => `
            <li class="script-item ${state.activeScript === s.relative_path ? 'active' : ''}" role="button" tabindex="0"
                onclick="selectScript('${s.relative_path}')"
                onkeydown="handleKeyboardAction(event, () => selectScript('${s.relative_path}'))">
                <span class="script-item-icon" style="color: var(--accent-yellow); stroke: var(--accent-yellow);">${ICONS.favorite}</span>
                <span class="script-item-name">${escapeHtml(s.name)}</span>
            </li>
        `).join('');
    } else {
        favsSection.style.display = 'none';
    }
}

function renderWelcomeStats() {
    const statsEl = document.getElementById('welcome-stats');
    if (!statsEl) return;

    let totalScripts = 0;
    let totalCategories = Object.keys(state.scripts).length;
    let totalFavs = 0;

    for (const scripts of Object.values(state.scripts)) {
        totalScripts += scripts.length;
        totalFavs += scripts.filter(s => s.favorite).length;
    }

    statsEl.innerHTML = `
        <div class="stat-card">
            <div class="stat-value">${totalScripts}</div>
            <div class="stat-label">Total Scripts</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${totalCategories}</div>
            <div class="stat-label">Categories</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${totalFavs}</div>
            <div class="stat-label">Favorites</div>
        </div>
    `;
}

async function selectScript(relPath) {
    state.activeScript = relPath;

    let script = null;
    for (const scripts of Object.values(state.scripts)) {
        script = scripts.find(s => s.relative_path === relPath);
        if (script) break;
    }
    if (!script) return;

    state.expandedCategories.add(script.category);
    renderSidebar();

    const welcomePanel = document.getElementById('welcome-state');
    const detailPanel = document.getElementById('script-detail');
    const lockedPanel = document.getElementById('locked-state');

    welcomePanel.style.display = 'none';

    // Handle locked state
    if (script.locked && !state.unlockedScripts[relPath]) {
        detailPanel.style.display = 'none';
        lockedPanel.style.display = '';

        const unlockBtn = document.getElementById('btn-unlock');
        const passInput = document.getElementById('unlock-password');
        passInput.value = '';
        passInput.focus();

        // Remove old event listeners
        const newUnlockBtn = unlockBtn.cloneNode(true);
        unlockBtn.parentNode.replaceChild(newUnlockBtn, unlockBtn);

        const unlockAction = async () => {
            const content = await fetchScriptContent(relPath, passInput.value);
            if (content.locked) {
                notify('Incorrect password.', 'error');
            } else {
                state.unlockedScripts[relPath] = passInput.value;
                selectScript(relPath);
            }
        };

        newUnlockBtn.addEventListener('click', unlockAction);
        passInput.onkeydown = (e) => { if (e.key === 'Enter') unlockAction(); };

        return;
    }

    // Unlocked flow
    lockedPanel.style.display = 'none';
    detailPanel.style.display = '';

    // Fill details
    document.getElementById('detail-category').textContent = script.category;
    document.getElementById('detail-name').textContent = script.name;
    document.getElementById('detail-desc').textContent = script.desc || 'No description provided';
    document.getElementById('detail-path').textContent = script.relative_path;

    // Tags
    const tagsEl = document.getElementById('detail-tags');
    if (script.tag && script.tag.trim() !== '') {
        tagsEl.innerHTML = script.tag.split(',').map(t =>
            `<span class="tag">${escapeHtml(t.trim())}</span>`
        ).join('');
    } else {
        tagsEl.innerHTML = '';
    }

    // Toolbar states
    const btnFav = document.getElementById('btn-fav');
    if (btnFav) btnFav.classList.toggle('active', script.favorite);
    updateRunButton();

    const btnLock = document.getElementById('btn-lock');
    if (btnLock) {
        if (script.locked) {
            btnLock.style.color = "var(--accent-orange)";
            btnLock.style.borderColor = "var(--accent-orange)";
        } else {
            btnLock.style.color = "";
            btnLock.style.borderColor = "";
        }
    }

    // Source code
    const content = await fetchScriptContent(relPath, state.unlockedScripts[relPath] || '');
    if (!content.locked && content !== undefined) {
        document.getElementById('detail-code').textContent = content;
    }

    // Reset resource panel
    document.getElementById('resource-panel').style.display = 'none';

    // Animate in
    detailPanel.classList.add('animate-in');
    setTimeout(() => detailPanel.classList.remove('animate-in'), 300);
}

function showWelcome() {
    document.getElementById('welcome-state').style.display = '';
    if (document.getElementById('script-detail')) document.getElementById('script-detail').style.display = 'none';
    if (document.getElementById('locked-state')) document.getElementById('locked-state').style.display = 'none';
    state.activeScript = null;
    renderSidebar();
}

function renderResources(resources) {
    document.getElementById('res-time').textContent = resources.execution_time_formatted || `${resources.execution_time}s`;

    const exitEl = document.getElementById('res-exit');
    exitEl.textContent = resources.exit_code !== undefined ? resources.exit_code : '—';
    exitEl.style.color = resources.exit_code === 0 ? 'var(--accent)' : 'var(--accent-red)';

    if (resources.cpu_percent !== undefined) {
        document.getElementById('res-cpu').textContent = `${resources.cpu_percent}%`;
        document.getElementById('res-cpu-bar').style.width = `${Math.min(resources.cpu_percent, 100)}%`;

        const cpuBar = document.getElementById('res-cpu-bar');
        if (resources.cpu_percent > 80) cpuBar.style.background = 'var(--accent-red)';
        else if (resources.cpu_percent > 50) cpuBar.style.background = 'var(--accent-orange)';
        else cpuBar.style.background = 'var(--accent)';
    }

    if (resources.memory_used_mb !== undefined) {
        const memUsed = resources.memory_used_mb.toFixed(1);
        const memPercent = resources.memory_percent.toFixed(2);

        // Show actual memory used by process 
        document.getElementById('res-mem').textContent = `${memUsed} MB (${memPercent}%)`;
        document.getElementById('res-mem-bar').style.width = `${Math.min(resources.memory_percent, 100)}%`;

        const memBar = document.getElementById('res-mem-bar');
        if (resources.memory_percent > 85) memBar.style.background = 'var(--accent-red)';
        else if (resources.memory_percent > 60) memBar.style.background = 'var(--accent-orange)';
        else memBar.style.background = 'var(--accent)';
    }
}

function toggleCategory(cat) {
    if (state.expandedCategories.has(cat)) state.expandedCategories.delete(cat);
    else state.expandedCategories.add(cat);
    renderSidebar();
}

function handleKeyboardAction(event, callback) {
    if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        callback();
    }
}

// ─── Modals ─────────────────────────────────────────────

function openModal(mode = 'new') {
    const overlay = document.getElementById('modal-overlay');
    const title = document.getElementById('modal-title');

    if (mode === 'edit' && state.activeScript) {
        title.textContent = 'Edit Script';
        const parts = state.activeScript.split('/');
        document.getElementById('modal-category').value = parts[0] || '';
        document.getElementById('modal-filename').value = parts[1] || '';
        fetchScriptContent(state.activeScript, state.unlockedScripts[state.activeScript] || '').then(content => {
            if (!content.locked) document.getElementById('modal-editor').value = content;
        });
    } else {
        title.textContent = 'New Script';
        document.getElementById('modal-category').value = '';
        document.getElementById('modal-filename').value = '';
        document.getElementById('modal-editor').value = `#!/bin/bash\n# name: \n# desc: \n# tag: \n\n`;
    }

    overlay.classList.add('active');
}

function closeModal() {
    document.getElementById('modal-overlay').classList.remove('active');
}

// ─── Event Bindings ────────────────────────────────────────

function bindEvents() {
    // Terminal Search
    const cliSearchInput = document.getElementById('cli-search-input');
    if (cliSearchInput) {
        cliSearchInput.addEventListener('input', () => highlightTerminalSearch());
    }

    // Terminal Tabs
    const btnAddTab = document.getElementById('btn-add-tab');
    if (btnAddTab) {
        btnAddTab.addEventListener('click', addTerminal);
    }
    const firstTab = document.querySelector('.cli-tab[data-id="1"]');
    if (firstTab) {
        firstTab.addEventListener('click', () => switchTerminal(1));
    }

    // Search
    const searchInput = document.getElementById('search-input');
    searchInput.addEventListener('input', (e) => {
        state.searchQuery = e.target.value;
        renderSidebar();
    });

    // CLI input
    const cliInput = document.getElementById('cli-input');
    cliInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            const cmd = cliInput.value.trim();
            if (cmd) {
                execCommand(cmd);
                cliInput.value = '';
            }
        }
        if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (state.cmdHistoryIndex > 0) {
                state.cmdHistoryIndex--;
                cliInput.value = state.cmdHistory[state.cmdHistoryIndex] || '';
            }
        }
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (state.cmdHistoryIndex < state.cmdHistory.length - 1) {
                state.cmdHistoryIndex++;
                cliInput.value = state.cmdHistory[state.cmdHistoryIndex] || '';
            } else {
                state.cmdHistoryIndex = state.cmdHistory.length;
                cliInput.value = '';
            }
        }
    });

    // Run Command button
    document.getElementById('btn-run-cmd').addEventListener('click', () => {
        const cmd = cliInput.value.trim();
        if (cmd) {
            execCommand(cmd);
            cliInput.value = '';
        }
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
            e.preventDefault();
            searchInput.focus();
        }
        if (e.key === 'Escape') {
            // close any active modal
            document.querySelectorAll('.modal-overlay').forEach(el => el.classList.remove('active'));
            if (document.activeElement === searchInput) {
                searchInput.value = '';
                state.searchQuery = '';
                searchInput.blur();
                renderSidebar();
            }
        }
    });

    // Create / Refresh
    const btnHistory = document.getElementById('btn-history');
    if (btnHistory) btnHistory.addEventListener('click', openHistoryViewer);
    document.getElementById('btn-add-script').addEventListener('click', () => openModal('new'));
    document.getElementById('btn-refresh').addEventListener('click', () => loadScripts());

    // Script Details Actions
    const btnRun = document.getElementById('btn-run');
    if (btnRun) {
        btnRun.addEventListener('click', () => {
            if (state.runningScripts[state.activeTerminalId]) {
                abortScriptRun(state.activeTerminalId);
            } else if (state.activeScript) {
                runScript(state.activeScript);
            }
        });
    }

    const btnEdit = document.getElementById('btn-edit');
    if (btnEdit) btnEdit.addEventListener('click', () => { if (state.activeScript) openModal('edit'); });

    const btnDel = document.getElementById('btn-delete');
    if (btnDel) btnDel.addEventListener('click', () => { if (state.activeScript) deleteScript(state.activeScript); });

    const btnFav = document.getElementById('btn-fav');
    if (btnFav) btnFav.addEventListener('click', () => { if (state.activeScript) toggleFavorite(state.activeScript); });

    // Clear terminal
    document.getElementById('btn-clear').addEventListener('click', clearCli);
    document.getElementById('btn-close-detail').addEventListener('click', showWelcome);

    const historyOverlay = document.getElementById('history-modal-overlay');
    const historyClose = document.getElementById('history-modal-close');
    const historySearch = document.getElementById('history-search');
    const historyFilters = document.querySelectorAll('.history-filter');
    const historyExportTxt = document.getElementById('history-export-txt');
    const historyExportLog = document.getElementById('history-export-log');

    if (historyClose) historyClose.addEventListener('click', closeHistoryViewer);
    if (historyOverlay) {
        historyOverlay.addEventListener('click', (e) => {
            if (e.target === historyOverlay) closeHistoryViewer();
        });
    }
    if (historySearch) {
        let historySearchTimer;
        historySearch.addEventListener('input', () => {
            clearTimeout(historySearchTimer);
            historySearchTimer = setTimeout(() => refreshExecutionHistory(), 180);
        });
    }
    historyFilters.forEach(filterButton => {
        filterButton.addEventListener('click', () => {
            historyFilters.forEach(btn => btn.classList.remove('active'));
            filterButton.classList.add('active');
            refreshExecutionHistory();
        });
    });
    if (historyExportTxt) historyExportTxt.addEventListener('click', () => exportExecutionHistory('txt'));
    if (historyExportLog) historyExportLog.addEventListener('click', () => exportExecutionHistory('log'));

    // Main Modal controls
    document.getElementById('modal-close').addEventListener('click', closeModal);
    document.getElementById('modal-cancel').addEventListener('click', closeModal);
    document.getElementById('modal-overlay').addEventListener('click', (e) => { if (e.target.id === 'modal-overlay') closeModal(); });

    document.getElementById('modal-save').addEventListener('click', () => {
        const category = document.getElementById('modal-category').value.trim();
        const filename = document.getElementById('modal-filename').value.trim();
        const content = document.getElementById('modal-editor').value;

        if (!category) {
            return notify('Please enter a category name.', 'warning');
        }

        if (!filename) {
            return notify('Please enter a filename.', 'warning');
        }

        if (!content.trim()) {
            return notify('Please enter script content.', 'warning');
        }

        saveScript(category, filename, content);
    });

    // GitHub Import Features
    const btnGithub = document.getElementById('btn-github-import');
    const githubOverlay = document.getElementById('github-modal-overlay');
    if (btnGithub && githubOverlay) {
        btnGithub.addEventListener('click', () => {
            document.getElementById('github-url').value = '';
            document.getElementById('github-category').value = '';
            document.getElementById('github-filename').value = '';
            githubOverlay.classList.add('active');
        });

        const closeGithub = () => githubOverlay.classList.remove('active');
        document.getElementById('github-modal-close').addEventListener('click', closeGithub);
        document.getElementById('github-modal-cancel').addEventListener('click', closeGithub);
        githubOverlay.addEventListener('click', (e) => { if (e.target.id === 'github-modal-overlay') closeGithub(); });

        document.getElementById('github-modal-import').addEventListener('click', () => {
            const url = document.getElementById('github-url').value;
            const category = document.getElementById('github-category').value;
            const filename = document.getElementById('github-filename').value;

            if (!url || !category || !filename) {
                return notify('All GitHub import fields are required.', 'warning');
            }
            importGithubScript(url, category, filename);
        });
    }

    // PR Modal Features
    const prOverlay = document.getElementById('pr-modal-overlay');
    if (prOverlay) {
        const closePr = () => prOverlay.classList.remove('active');
        document.getElementById('pr-modal-close').addEventListener('click', closePr);
        document.getElementById('pr-modal-cancel').addEventListener('click', closePr);
        prOverlay.addEventListener('click', (e) => { if (e.target.id === 'pr-modal-overlay') closePr(); });

        document.getElementById('pr-modal-submit').addEventListener('click', () => {
            const repoUrl = document.getElementById('pr-repo').value.trim();
            const branch = document.getElementById('pr-branch').value.trim();
            const message = document.getElementById('pr-message').value.trim();
            if (!branch || !message) {
                notify('Both branch name and commit message are required.', 'warning');
                return;
            }
            if (state.activeScript) {
                executePR(state.activeScript, branch, message, repoUrl);
            }
        });
    }

    // Lock Features
    const btnLock = document.getElementById('btn-lock');
    const lockOverlay = document.getElementById('lock-modal-overlay');
    if (btnLock && lockOverlay) {
        btnLock.addEventListener('click', () => {
            if (!state.activeScript) return;

            // Check if it's already locked from state
            let isLocked = false;
            for (let cat in state.scripts) {
                let sc = state.scripts[cat].find(s => s.relative_path === state.activeScript);
                if (sc && sc.locked) isLocked = true;
            }

            const modalHeader = document.querySelector('#lock-modal h2');
            const currentPassGroup = document.getElementById('lock-current-pass-group');
            const newPassGroup = document.getElementById('lock-new-pass').parentElement;

            if (isLocked) {
                modalHeader.textContent = 'Remove Script Lock';
                currentPassGroup.style.display = 'flex';
                currentPassGroup.querySelector('label').textContent = 'Enter Password to Remove Lock';
                newPassGroup.style.display = 'none';
            } else {
                modalHeader.textContent = 'Lock Script';
                currentPassGroup.style.display = 'none';
                newPassGroup.style.display = 'flex';
                newPassGroup.querySelector('label').textContent = 'Set Password';
            }

            document.getElementById('lock-current-pass').value = '';
            document.getElementById('lock-new-pass').value = '';

            lockOverlay.classList.add('active');
        });

        const closeLock = () => lockOverlay.classList.remove('active');
        document.getElementById('lock-modal-close').addEventListener('click', closeLock);
        document.getElementById('lock-modal-cancel').addEventListener('click', closeLock);
        lockOverlay.addEventListener('click', (e) => { if (e.target.id === 'lock-modal-overlay') closeLock(); });

        document.getElementById('lock-modal-save').addEventListener('click', async () => {
            let isLocked = false;
            for (let cat in state.scripts) {
                let sc = state.scripts[cat].find(s => s.relative_path === state.activeScript);
                if (sc && sc.locked) isLocked = true;
            }

            let oldPass = '', newPass = '';
            if (isLocked) {
                oldPass = document.getElementById('lock-current-pass').value;
                newPass = ''; // meaning remove lock
            } else {
                oldPass = '';
                newPass = document.getElementById('lock-new-pass').value;
                if (!newPass) {
                    return notify('Password cannot be empty when setting a lock.', 'warning');
                }
            }

            const success = await manageLock(state.activeScript, oldPass, newPass);
            if (success) {
                notify(
                    isLocked
                        ? 'Script lock removed successfully.'
                        : 'Script locked successfully.',
                    'success'
                );
                if (!isLocked && newPass) {
                    delete state.unlockedScripts[state.activeScript];
                    selectScript(state.activeScript);
                } else if (isLocked && !newPass) {
                    delete state.unlockedScripts[state.activeScript];
                    selectScript(state.activeScript);
                }
                closeLock();
            }
        });
    }
}

// ─── Helpers ───────────────────────────────────────────────

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function escapeAttr(text) {
    return text.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function removeNotification(notification) {
    if (!notification || notification.classList.contains('removing')) {
        return;
    }

    notification.classList.add('removing');

    notification.addEventListener('animationend', () => {
        notification.remove();
    }, { once: true });
}

function notify(message, type = 'info') {
    const container = document.getElementById('notification-container');

    if (!container) {
        console.warn('Notification container missing');
        return;
    }

    const existingNotifications = container.querySelectorAll('.notification');

    if (existingNotifications.length >= MAX_NOTIFICATIONS) {
        removeNotification(existingNotifications[0]);
    }

    const notification = document.createElement('div');
    notification.className = `notification ${type}`;
    notification.setAttribute('role', 'status');

    notification.setAttribute(
        'aria-live',
        type === 'error' ? 'assertive' : 'polite'
    );

    const content = document.createElement('div');
    content.className = 'notification-content';

    const messageElement = document.createElement('div');
    messageElement.className = 'notification-message';

    // Safe rendering
    messageElement.textContent = message;

    content.appendChild(messageElement);

    const closeButton = document.createElement('button');
    closeButton.className = 'notification-close';
    closeButton.setAttribute('aria-label', 'Dismiss notification');
    closeButton.textContent = '×';

    closeButton.addEventListener('click', () => {
        removeNotification(notification);
    });

    notification.appendChild(content);
    notification.appendChild(closeButton);

    container.appendChild(notification);

    let timeout = setTimeout(() => {
        removeNotification(notification);
    }, NOTIFICATION_DURATION);

    notification.addEventListener('mouseenter', () => {
        clearTimeout(timeout);
    });

    notification.addEventListener('mouseleave', () => {
        clearTimeout(timeout);
        timeout = setTimeout(() => {
            removeNotification(notification);
        }, 2000);
    });
}

// ═══════════════════════════════════════════════════════════
//  Debugger Console with Smart Suggestions
// ═══════════════════════════════════════════════════════════

const DebuggerConsole = (() => {
    let entries = [];
    let activeFilter = 'all';
    let suggestionIndex = -1;
    let debugHistory = [];
    let debugHistoryIdx = -1;
    let isOpen = false;

    const ICONS_DBG = {
        log: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>`,
        warn: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>`,
        error: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/></svg>`,
        info: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>`,
        network: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`,
        result: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>`,
        input: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>`,
    };

    // Smart suggestion database (JS expressions and debug helpers only)
    const SUGGESTIONS = [
        { cmd: 'clear', desc: 'Clear debugger console', icon: 'cmd', category: 'debug' },
        { cmd: 'state', desc: 'Inspect current app state', icon: 'debug', category: 'debug' },
        { cmd: 'state.scripts', desc: 'View loaded scripts object', icon: 'debug', category: 'debug' },
        { cmd: 'state.activeScript', desc: 'Show currently selected script', icon: 'debug', category: 'debug' },
        { cmd: 'state.terminals', desc: 'List active terminal IDs', icon: 'debug', category: 'debug' },
        { cmd: 'state.cmdHistory', desc: 'View command history', icon: 'debug', category: 'debug' },
        { cmd: 'state.expandedCategories', desc: 'View expanded categories', icon: 'debug', category: 'debug' },
        { cmd: 'Object.keys(state.scripts)', desc: 'List script categories', icon: 'debug', category: 'debug' },
        { cmd: 'JSON.stringify(state, null, 2)', desc: 'Pretty print full state', icon: 'debug', category: 'debug' },
        { cmd: 'document.title', desc: 'Get page title', icon: 'cmd', category: 'js' },
        { cmd: 'window.location.href', desc: 'Get current URL', icon: 'cmd', category: 'js' },
        { cmd: 'navigator.userAgent', desc: 'Get browser user agent', icon: 'cmd', category: 'js' },
        { cmd: 'performance.now()', desc: 'Get high-res timestamp', icon: 'cmd', category: 'js' },
        { cmd: 'loadScripts()', desc: 'Reload scripts from server', icon: 'script', category: 'debug' },
    ];

    function getTime() {
        const d = new Date();
        return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }

    function addEntry(type, content, source = '') {
        const entry = { type, content: String(content), source, time: getTime(), id: Date.now() + Math.random() };
        entries.push(entry);
        renderEntry(entry);
        updateCount();
        updateFilterBadges();
    }

    function renderEntry(entry) {
        const body = document.getElementById('debugger-body');
        if (!body) return;
        const welcome = body.querySelector('.debugger-welcome');
        if (welcome) welcome.remove();

        const el = document.createElement('div');
        el.className = `debugger-entry ${entry.type}`;
        el.dataset.type = entry.type;
        if (activeFilter !== 'all' && entry.type !== activeFilter) el.classList.add('hidden');

        el.innerHTML = `
            <span class="debugger-entry-icon">${ICONS_DBG[entry.type] || ICONS_DBG.log}</span>
            <span class="debugger-entry-time">${entry.time}</span>
            <span class="debugger-entry-content">${escapeHtml(entry.content)}</span>
            ${entry.source ? `<span class="debugger-entry-source">${escapeHtml(entry.source)}</span>` : ''}
        `;
        body.appendChild(el);
        body.scrollTop = body.scrollHeight;
    }

    function updateCount() {
        const el = document.getElementById('debugger-log-count');
        if (el) {
            const visible = activeFilter === 'all' ? entries.length : entries.filter(e => e.type === activeFilter).length;
            el.textContent = `${visible} ${visible === 1 ? 'entry' : 'entries'}`;
        }
    }

    function updateFilterBadges() {
        const errorCount = entries.filter(e => e.type === 'error').length;
        const warnCount = entries.filter(e => e.type === 'warn').length;
        const errTab = document.querySelector('.debugger-filter-tab[data-filter="error"]');
        const warnTab = document.querySelector('.debugger-filter-tab[data-filter="warn"]');
        if (errTab) errTab.classList.toggle('has-entries', errorCount > 0);
        if (warnTab) warnTab.classList.toggle('has-entries', warnCount > 0);
        if (errTab && errorCount > 0) errTab.textContent = `Error (${errorCount})`;
        else if (errTab) errTab.textContent = 'Error';
        if (warnTab && warnCount > 0) warnTab.textContent = `Warn (${warnCount})`;
        else if (warnTab) warnTab.textContent = 'Warn';
    }

    function setFilter(filter) {
        activeFilter = filter;
        document.querySelectorAll('.debugger-filter-tab').forEach(t => {
            t.classList.toggle('active', t.dataset.filter === filter);
        });
        document.querySelectorAll('.debugger-entry').forEach(el => {
            if (filter === 'all') el.classList.remove('hidden');
            else el.classList.toggle('hidden', el.dataset.type !== filter);
        });
        updateCount();
    }

    function clearConsole() {
        entries = [];
        const body = document.getElementById('debugger-body');
        if (body) body.innerHTML = `<div class="debugger-welcome"><span class="debugger-welcome-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg></span><span>Console cleared.</span></div>`;
        updateCount();
        updateFilterBadges();
    }

    function toggle() {
        const panel = document.getElementById('debugger-console');
        const btn = document.getElementById('btn-debugger-toggle');
        if (!panel) return;
        isOpen = !isOpen;
        panel.classList.toggle('open', isOpen);
        btn.classList.toggle('active', isOpen);
        document.body.classList.toggle('debugger-open', isOpen);
        if (isOpen) {
            const h = panel.offsetHeight;
            document.documentElement.style.setProperty('--debugger-height', h + 'px');
        }
    }

    function close() {
        isOpen = false;
        const panel = document.getElementById('debugger-console');
        const btn = document.getElementById('btn-debugger-toggle');
        if (panel) panel.classList.remove('open');
        if (btn) btn.classList.remove('active');
        document.body.classList.remove('debugger-open');
    }

    function getSuggestions(query) {
        const q = query.toLowerCase();
        const scriptSugs = [];
        const stateSugs = [];

        // 1. Dynamic property suggestions for 'state' object
        if (q.startsWith('state.')) {
            const parts = q.split('.');
            const prefix = parts.slice(0, -1).join('.');
            const lastPart = parts[parts.length - 1];

            // For now, just handle top-level properties of 'state'
            if (typeof state !== 'undefined') {
                Object.keys(state).forEach(key => {
                    if (key.toLowerCase().startsWith(lastPart)) {
                        stateSugs.push({
                            cmd: `${prefix}.${key}`,
                            desc: `Property: ${typeof state[key]}`,
                            icon: 'debug',
                            category: 'debug'
                        });
                    }
                });
            }
        }

        // 2. Dynamic script suggestions
        if (typeof state !== 'undefined' && state.scripts) {
            for (const [cat, scripts] of Object.entries(state.scripts)) {
                for (const s of scripts) {
                    if (s.name.toLowerCase().includes(q) || s.relative_path.toLowerCase().includes(q)) {
                        scriptSugs.push({ cmd: `runScript('${s.relative_path}')`, desc: `Run: ${s.name}`, icon: 'script', category: 'script' });
                    }
                }
            }
        }

        // 3. Static suggestions
        const matched = SUGGESTIONS.filter(s => s.cmd.toLowerCase().includes(q) || s.desc.toLowerCase().includes(q));

        // Combine all, prioritizing state property completions if they exist
        const all = [...stateSugs, ...scriptSugs, ...matched];

        // Remove duplicates (by cmd)
        const unique = [];
        const seen = new Set();
        for (const item of all) {
            if (!seen.has(item.cmd)) {
                unique.push(item);
                seen.has(item.cmd);
                seen.add(item.cmd);
            }
        }

        return unique.slice(0, 10);
    }

    function renderSuggestions(query) {
        const container = document.getElementById('debugger-suggestions');
        if (!container) return;
        const items = getSuggestions(query);
        if (items.length === 0) { container.classList.remove('open'); return; }

        suggestionIndex = -1;
        container.innerHTML = items.map((s, i) => `
            <div class="debugger-suggestion-item" data-index="${i}" data-cmd="${escapeAttr(s.cmd)}">
                <span class="suggestion-icon ${s.icon}">${s.icon === 'cmd' ? '›' : s.icon === 'bash' ? '$' : s.icon === 'debug' ? '⚙' : '▶'}</span>
                <div class="suggestion-content">
                    <div class="suggestion-title">${escapeHtml(s.cmd)}</div>
                    <div class="suggestion-desc">${escapeHtml(s.desc)}</div>
                </div>
                <span class="suggestion-kbd">Tab</span>
            </div>
        `).join('');
        container.classList.add('open');

        container.querySelectorAll('.debugger-suggestion-item').forEach(el => {
            el.addEventListener('click', () => {
                document.getElementById('debugger-input').value = el.dataset.cmd;
                container.classList.remove('open');
                document.getElementById('debugger-input').focus();
            });
        });
    }

    function navigateSuggestions(dir) {
        const container = document.getElementById('debugger-suggestions');
        const items = container.querySelectorAll('.debugger-suggestion-item');
        if (!items.length) return;
        items.forEach(i => i.classList.remove('selected'));
        suggestionIndex = (suggestionIndex + dir + items.length) % items.length;
        items[suggestionIndex].classList.add('selected');
        items[suggestionIndex].scrollIntoView({ block: 'nearest' });
    }

    function acceptSuggestion() {
        const container = document.getElementById('debugger-suggestions');
        const items = container.querySelectorAll('.debugger-suggestion-item');
        if (suggestionIndex >= 0 && items[suggestionIndex]) {
            document.getElementById('debugger-input').value = items[suggestionIndex].dataset.cmd;
            container.classList.remove('open');
            return true;
        } else if (items.length > 0) {
            document.getElementById('debugger-input').value = items[0].dataset.cmd;
            container.classList.remove('open');
            return true;
        }
        return false;
    }

    function evaluate(expr) {
        if (!expr.trim()) return;
        debugHistory.push(expr);
        debugHistoryIdx = debugHistory.length;
        addEntry('input', `› ${expr}`);

        if (expr.trim() === 'clear') { clearConsole(); return; }

        // JS expression evaluator only
        try {
            const result = eval(expr); // eslint-disable-line no-eval
            const output = (result === null) ? 'null' :
                (result === undefined) ? 'undefined' :
                    typeof result === 'object' ? JSON.stringify(result, null, 2) : String(result);
            addEntry('result', output);
        } catch (e) {
            addEntry('error', e.message, 'eval');
        }
    }

    // Intercept console methods
    function interceptConsole() {
        const orig = { log: console.log, warn: console.warn, error: console.error, info: console.info };
        console.log = (...args) => { orig.log.apply(console, args); addEntry('log', args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' '), 'console'); };
        console.warn = (...args) => { orig.warn.apply(console, args); addEntry('warn', args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' '), 'console'); };
        console.error = (...args) => { orig.error.apply(console, args); addEntry('error', args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' '), 'console'); };
        console.info = (...args) => { orig.info.apply(console, args); addEntry('info', args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' '), 'console'); };

        // Intercept fetch for network logging (only log errors, not 2xx)
        const origFetch = window.fetch;
        window.fetch = async (...args) => {
            const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
            const method = args[1]?.method || 'GET';
            const startTime = performance.now();
            try {
                const res = await origFetch.apply(window, args);
                const elapsed = (performance.now() - startTime).toFixed(1);
                // Only log failed requests (non-2xx status codes)
                if (!res.ok) {
                    addEntry('network', `${method} ${url} → ${res.status} (${elapsed}ms)`, 'fetch');
                }
                return res;
            } catch (e) {
                addEntry('network', `${method} ${url} → FAILED: ${e.message}`, 'fetch');
                throw e;
            }
        };

        // Catch unhandled errors
        window.addEventListener('error', (e) => { addEntry('error', `${e.message} at ${e.filename}:${e.lineno}`, 'window'); });
        window.addEventListener('unhandledrejection', (e) => { addEntry('error', `Unhandled Promise: ${e.reason}`, 'promise'); });
    }

    function initResizer() {
        const handle = document.getElementById('debugger-resize-handle');
        const panel = document.getElementById('debugger-console');
        if (!handle || !panel) return;
        let resizing = false;
        handle.addEventListener('mousedown', (e) => { resizing = true; e.preventDefault(); document.body.style.cursor = 'ns-resize'; });
        document.addEventListener('mousemove', (e) => {
            if (!resizing) return;
            let h = window.innerHeight - e.clientY;
            if (h < 150) h = 150;
            if (h > window.innerHeight * 0.7) h = window.innerHeight * 0.7;
            panel.style.height = h + 'px';
            document.documentElement.style.setProperty('--debugger-height', h + 'px');
        });
        document.addEventListener('mouseup', () => { if (resizing) { resizing = false; document.body.style.cursor = ''; } });
    }

    function init() {
        interceptConsole();
        initResizer();

        const toggleBtn = document.getElementById('btn-debugger-toggle');
        if (toggleBtn) toggleBtn.addEventListener('click', toggle);

        const closeBtn = document.getElementById('debugger-close');
        if (closeBtn) closeBtn.addEventListener('click', close);

        const clearBtn = document.getElementById('debugger-clear');
        if (clearBtn) clearBtn.addEventListener('click', clearConsole);

        // Filter tabs
        document.querySelectorAll('.debugger-filter-tab').forEach(tab => {
            tab.addEventListener('click', () => setFilter(tab.dataset.filter));
        });

        // Input handling
        const input = document.getElementById('debugger-input');
        const sugBox = document.getElementById('debugger-suggestions');
        if (input) {
            input.addEventListener('input', () => renderSuggestions(input.value));
            input.addEventListener('focus', () => renderSuggestions(input.value));
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Tab') {
                    e.preventDefault();
                    if (sugBox && sugBox.classList.contains('open')) acceptSuggestion();
                    else renderSuggestions(input.value);
                } else if (e.key === 'ArrowDown' && sugBox && sugBox.classList.contains('open')) {
                    e.preventDefault(); navigateSuggestions(1);
                } else if (e.key === 'ArrowUp') {
                    if (sugBox && sugBox.classList.contains('open')) { e.preventDefault(); navigateSuggestions(-1); }
                    else if (debugHistory.length) { e.preventDefault(); if (debugHistoryIdx > 0) debugHistoryIdx--; input.value = debugHistory[debugHistoryIdx] || ''; }
                } else if (e.key === 'Enter') {
                    if (sugBox && sugBox.classList.contains('open') && suggestionIndex >= 0) { acceptSuggestion(); }
                    else { evaluate(input.value); input.value = ''; }
                    if (sugBox) sugBox.classList.remove('open');
                } else if (e.key === 'Escape') {
                    if (sugBox) sugBox.classList.remove('open');
                }
            });
            // Close suggestions on outside click
            document.addEventListener('click', (e) => {
                if (sugBox && !sugBox.contains(e.target) && e.target !== input) sugBox.classList.remove('open');
            });
        }

        const evalBtn = document.getElementById('debugger-eval-btn');
        if (evalBtn) evalBtn.addEventListener('click', () => { evaluate(input.value); input.value = ''; if (sugBox) sugBox.classList.remove('open'); });

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            // Ctrl+` to toggle debugger
            if ((e.ctrlKey || e.metaKey) && e.key === '`') {
                e.preventDefault();
                toggle();
            }
            // Ctrl+L to clear debugger (only if open)
            if (isOpen && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'l') {
                e.preventDefault();
                clearConsole();
            }
        });
    }

    return { init, addEntry, toggle, close };
})();

// Initialize debugger when DOM is ready
document.addEventListener('DOMContentLoaded', () => { DebuggerConsole.init(); });
