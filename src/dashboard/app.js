// Dashboard Application Logic

document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    initClock();
    initCharts();
    initTypeFilter();
    fetchDashboardData();

    // Refresh data every 30 seconds
    setInterval(fetchDashboardData, 30000);

    // Filter change listener
    document.getElementById('patch-filter').addEventListener('change', fetchDashboardData);
});

function initNavigation() {
    const navItems = document.querySelectorAll('.nav-item');
    const views = document.querySelectorAll('.view-container');

    navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const targetViewId = `${item.dataset.view}-view`;

            // Update Nav State
            navItems.forEach(nav => nav.classList.remove('active'));
            item.classList.add('active');

            // Update View State
            views.forEach(view => {
                if (view.id === targetViewId) {
                    view.classList.add('active');
                } else {
                    view.classList.remove('active');
                }
            });

            // Trigger specific view inits
            if (item.dataset.view === 'developer') {
                initApplicationAccess();
            }
            if (item.dataset.view === 'users') {
                initUserQuilt();
            }
            if (item.dataset.view === 'schema') {
                initSchemaView();
            }
            if (item.dataset.view === 'roi') {
                initCostView();
            }
            if (item.dataset.view === 'system') {
                initSystemView();
            }
            if (item.dataset.view === 'settings') {
                initSettingsView();
            }
        });
    });
}
// ... (rest of initNavigation) ...

// ... (initClock, user quilt, schema logic) ...

// ============================================================
// Patch Type Manager
// ============================================================

async function initSchemaView() {
    await fetchPatchTypes();
    await fetchConnections();
}

async function fetchPatchTypes() {
    try {
        const res = await fetch('/api/dashboard/patch-types');
        const types = await res.json();
        renderPatchTypesTable(types);
    } catch (error) { console.error('Failed to fetch patch types:', error); }
}

function renderPatchTypesTable(types) {
    const tbody = document.getElementById('patch-types-body');
    if (!tbody) return;
    tbody.innerHTML = '';
    types.forEach(t => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td style="font-family:var(--font-mono); color:var(--text-primary);">${t.type_key}</td>
            <td style="color:var(--text-secondary);">${t.display_name}</td>
            <td><select onchange="updatePatchType('${t.type_key}', {persistence: this.value})" style="background:rgba(15,23,42,0.6); border:1px solid rgba(148,163,184,0.1); color:var(--text-primary); padding:4px 8px; border-radius:4px;">
                <option value="sticky" ${t.persistence==='sticky'?'selected':''}>sticky</option>
                <option value="decaying" ${t.persistence==='decaying'?'selected':''}>decaying</option>
                <option value="completable" ${t.persistence==='completable'?'selected':''}>completable</option>
            </select></td>
            <td><input type="number" value="${t.default_ttl_days||''}" placeholder="—" onchange="updatePatchType('${t.type_key}', {default_ttl_days: parseInt(this.value)||null})" style="width:60px; background:rgba(15,23,42,0.6); border:1px solid rgba(148,163,184,0.1); color:var(--text-primary); padding:4px 8px; border-radius:4px;"></td>
            <td style="text-align:center;"><input type="checkbox" ${t.is_completable?'checked':''} onchange="updatePatchType('${t.type_key}', {is_completable: this.checked})"></td>
            <td style="text-align:center;"><input type="checkbox" ${t.project_scoped?'checked':''} onchange="updatePatchType('${t.type_key}', {project_scoped: this.checked})"></td>
            <td style="color:var(--text-muted); font-size:0.8rem;">${t.app_id || 'built-in'}</td>
        `;
        tbody.appendChild(tr);
    });
}

async function updatePatchType(typeKey, data) {
    try {
        await fetch(`/api/dashboard/patch-types/${typeKey}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
        });
    } catch (error) { console.error('Failed to update patch type:', error); }
}

function toggleAddPatchTypeForm() {
    const form = document.getElementById('add-patch-type-form');
    form.style.display = form.style.display === 'none' ? 'block' : 'none';
}

async function addPatchType() {
    const typeKey = document.getElementById('new-type-key').value.trim();
    const displayName = document.getElementById('new-type-name').value.trim();
    const persistence = document.getElementById('new-type-persistence').value;
    if (!typeKey || !displayName) return alert('Type key and display name required');
    try {
        await fetch('/api/dashboard/patch-types', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ type_key: typeKey, display_name: displayName, persistence })
        });
        document.getElementById('new-type-key').value = '';
        document.getElementById('new-type-name').value = '';
        toggleAddPatchTypeForm();
        fetchPatchTypes();
    } catch (error) { alert('Failed to create patch type'); }
}

async function fetchConnections() {
    try {
        const res = await fetch('/api/dashboard/connections');
        const conns = await res.json();
        renderConnectionsTable(conns);
    } catch (error) { console.error('Failed to fetch connections:', error); }
}

function renderConnectionsTable(conns) {
    const tbody = document.getElementById('connections-body');
    if (!tbody) return;
    tbody.innerHTML = '';
    const roleColors = { parent: '#3b82f6', depends_on: '#ef4444', resolves: '#10b981', replaces: '#f59e0b', informs: '#8b5cf6' };
    conns.forEach(c => {
        const color = roleColors[c.role] || '#94a3b8';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td style="font-family:var(--font-mono); color:var(--text-primary);">${c.label}</td>
            <td><span class="badge" style="background:rgba(${color === '#3b82f6' ? '59,130,246' : color === '#ef4444' ? '239,68,68' : color === '#10b981' ? '16,185,129' : color === '#f59e0b' ? '245,158,11' : '139,92,246'},0.15); color:${color};">${c.role}</span></td>
            <td style="font-family:var(--font-mono); font-size:0.8rem; color:var(--text-muted);">${(c.from_types||[]).join(', ')}</td>
            <td style="font-family:var(--font-mono); font-size:0.8rem; color:var(--text-muted);">${(c.to_types||[]).join(', ')}</td>
            <td style="color:var(--text-muted); font-size:0.85rem;">${c.description||''}</td>
        `;
        tbody.appendChild(tr);
    });
}

function toggleAddConnectionForm() {
    const form = document.getElementById('add-connection-form');
    form.style.display = form.style.display === 'none' ? 'block' : 'none';
}

async function addConnection() {
    const label = document.getElementById('new-conn-label').value.trim();
    const role = document.getElementById('new-conn-role').value;
    if (!label) return alert('Label required');
    try {
        await fetch('/api/dashboard/connections', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ label, role, from_types: [], to_types: [] })
        });
        document.getElementById('new-conn-label').value = '';
        toggleAddConnectionForm();
        fetchConnections();
    } catch (error) { alert('Failed to create connection'); }
}

// ============================================================
// Extraction Cost Tracking
// ============================================================

let costOverTimeChart, costByModelChart;

async function initCostView() {
    await fetchCostSummary();
    await fetchCostOverTime();
    await fetchCostByModel();
    await fetchRecentExtractions();
}

async function fetchCostSummary() {
    try {
        const res = await fetch('/api/dashboard/metrics/summary?days=30');
        const data = await res.json();
        document.getElementById('cost-total').textContent = '$' + data.total_cost.toFixed(4);
        document.getElementById('cost-extractions').textContent = data.total_extractions;
        document.getElementById('cost-avg').textContent = '$' + data.avg_cost.toFixed(6);
        document.getElementById('cost-latency').textContent = Math.round(data.avg_latency) + 'ms';
    } catch (error) { console.error('Failed to fetch cost summary:', error); }
}

async function fetchCostOverTime() {
    try {
        const res = await fetch('/api/dashboard/metrics/cost?days=30');
        const data = await res.json();
        const ctx = document.getElementById('costOverTimeChart');
        if (!ctx) return;
        if (costOverTimeChart) costOverTimeChart.destroy();
        costOverTimeChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: data.map(d => d.date),
                datasets: [{
                    label: 'Cost ($)',
                    data: data.map(d => d.cost),
                    borderColor: '#10b981',
                    backgroundColor: 'rgba(16, 185, 129, 0.1)',
                    fill: true, tension: 0.4, pointRadius: 3
                }]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    y: { grid: { color: 'rgba(148,163,184,0.05)' }, ticks: { color: '#64748b' } },
                    x: { grid: { display: false }, ticks: { color: '#64748b' } }
                }
            }
        });
    } catch (error) { console.error('Failed to fetch cost over time:', error); }
}

async function fetchCostByModel() {
    try {
        const res = await fetch('/api/dashboard/metrics/models?days=30');
        const data = await res.json();
        const ctx = document.getElementById('costByModelChart');
        if (!ctx) return;
        if (costByModelChart) costByModelChart.destroy();
        const colors = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ef4444', '#06b6d4'];
        costByModelChart = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: data.map(d => d.model),
                datasets: [{ data: data.map(d => d.cost), backgroundColor: colors.slice(0, data.length) }]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { position: 'bottom', labels: { color: '#94a3b8', padding: 15 } } }
            }
        });
    } catch (error) { console.error('Failed to fetch cost by model:', error); }
}

async function fetchRecentExtractions() {
    try {
        const res = await fetch('/api/dashboard/metrics/recent?limit=50');
        const data = await res.json();
        const tbody = document.getElementById('recent-extractions-body');
        if (!tbody) return;
        tbody.innerHTML = '';
        data.forEach(m => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td style="color:var(--text-muted); font-size:0.85rem; white-space:nowrap;">${new Date(m.created_at).toLocaleString()}</td>
                <td style="color:var(--text-primary);">${m.user_id || '—'}</td>
                <td style="font-family:var(--font-mono); font-size:0.8rem; color:var(--text-muted);">${m.model || '—'}</td>
                <td style="text-align:right; color:var(--text-muted);">${m.input_tokens || 0}</td>
                <td style="text-align:right; color:var(--text-muted);">${m.output_tokens || 0}</td>
                <td style="text-align:right; color:#10b981; font-family:var(--font-mono);">$${(m.cost_usd||0).toFixed(6)}</td>
                <td style="text-align:right; color:var(--text-muted);">${Math.round(m.latency_ms||0)}ms</td>
                <td style="text-align:right; color:var(--text-primary);">${m.patches_extracted || 0}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (error) { console.error('Failed to fetch recent extractions:', error); }
}

// ============================================================
// System Health
// ============================================================

async function initSystemView() {
    try {
        const res = await fetch('/api/dashboard/health-check');
        const data = await res.json();

        // Postgres
        const pgStatus = document.getElementById('health-pg-status');
        const pgDetail = document.getElementById('health-pg-detail');
        const pgCard = document.getElementById('health-pg-card');
        if (data.postgres.status === 'connected') {
            pgStatus.textContent = 'Connected';
            pgDetail.textContent = `${data.postgres.latency_ms}ms · ${data.postgres.patches} patches · ${data.postgres.users} users`;
            pgCard.querySelector('.kpi-icon').className = 'kpi-icon green';
        } else {
            pgStatus.textContent = 'Disconnected';
            pgDetail.textContent = data.postgres.error || '';
            pgCard.querySelector('.kpi-icon').className = 'kpi-icon red';
        }

        // Redis
        const redisStatus = document.getElementById('health-redis-status');
        const redisDetail = document.getElementById('health-redis-detail');
        const redisCard = document.getElementById('health-redis-card');
        if (data.redis.status === 'connected') {
            redisStatus.textContent = 'Connected';
            redisDetail.textContent = `${data.redis.latency_ms}ms · ${data.redis.queue_keys} queues · ${data.redis.entity_keys} indexes`;
            redisCard.querySelector('.kpi-icon').className = 'kpi-icon green';
        } else {
            redisStatus.textContent = 'Disconnected';
            redisDetail.textContent = data.redis.error || '';
            redisCard.querySelector('.kpi-icon').className = 'kpi-icon red';
        }

        // Worker
        document.getElementById('health-worker-pending').textContent = data.worker.pending_events >= 0 ? data.worker.pending_events : '?';

        // LLM
        document.getElementById('health-llm-model').textContent = data.llm.model;
        document.getElementById('health-llm-url').textContent = data.llm.base_url;

        // Config
        document.getElementById('health-config-display').textContent = JSON.stringify(data.config, null, 2);

    } catch (error) {
        console.error('Failed to fetch health:', error);
        document.getElementById('health-pg-status').textContent = 'Error';
    }
}

// ============================================================
// Settings / Configuration
// ============================================================

let allPrompts = [];

async function initSettingsView() {
    await fetchSettingsConfig();
    await fetchPromptList();
}

async function fetchSettingsConfig() {
    try {
        const res = await fetch('/api/dashboard/config');
        const config = await res.json();

        document.getElementById('extraction-settings-display').innerHTML = `
            <div style="display:grid; gap:0.75rem;">
                <div>Max patches per meeting: <strong>${config.extraction.max_patches_per_meeting}</strong></div>
                <div>Max entities per meeting: <strong>${config.extraction.max_entities_per_meeting}</strong></div>
                <div>Max relationships per meeting: <strong>${config.extraction.max_relationships_per_meeting}</strong></div>
            </div>
            <div style="margin-top:1rem; padding-top:1rem; border-top:1px solid rgba(148,163,184,0.1);">
                <div>Queue max wait: <strong>${config.queue.max_wait_minutes} min</strong></div>
                <div>Queue budget threshold: <strong>${(config.queue.budget_threshold * 100).toFixed(0)}%</strong></div>
            </div>
        `;

        document.getElementById('llm-config-display').innerHTML = `
            <div style="display:grid; gap:0.75rem;">
                <div>Model: <strong>${config.llm.model}</strong></div>
                <div>Base URL: <strong style="word-break:break-all;">${config.llm.base_url}</strong></div>
            </div>
        `;
    } catch (error) { console.error('Failed to fetch config:', error); }
}

async function fetchPromptList() {
    try {
        const res = await fetch('/api/dashboard/prompts');
        allPrompts = await res.json();
        const selector = document.getElementById('prompt-selector');
        if (!selector) return;
        selector.innerHTML = '<option value="">Select a prompt...</option>';
        allPrompts.forEach(p => {
            selector.innerHTML += `<option value="${p.prompt_key}">${p.prompt_name} (v${p.version_num})</option>`;
        });
    } catch (error) { console.error('Failed to fetch prompts:', error); }
}

function loadSelectedPrompt() {
    const key = document.getElementById('prompt-selector').value;
    const prompt = allPrompts.find(p => p.prompt_key === key);
    if (prompt) {
        document.getElementById('prompt-editor').value = prompt.prompt_text;
        document.getElementById('prompt-info').textContent =
            `${prompt.description || ''} · Version ${prompt.version_num} · Updated ${new Date(prompt.updated_at).toLocaleString()}`;
    } else {
        document.getElementById('prompt-editor').value = '';
        document.getElementById('prompt-info').textContent = '';
    }
}

async function savePrompt() {
    const key = document.getElementById('prompt-selector').value;
    if (!key) return alert('Select a prompt first');
    const text = document.getElementById('prompt-editor').value;
    if (!text.trim()) return alert('Prompt text cannot be empty');
    try {
        const res = await fetch(`/api/dashboard/prompts/${key}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ prompt_text: text, change_reason: 'Dashboard edit' })
        });
        if (res.ok) {
            const data = await res.json();
            alert(`Saved as version ${data.new_version}`);
            fetchPromptList();
        } else { alert('Failed to save prompt'); }
    } catch (error) { alert('Failed to save prompt: ' + error.message); }
}





// Chart Granularity State
let currentGranularity = { days: 1, start: null, end: null, unit: 'hour' };



function updateChartGranularity(days, unit) {
    currentGranularity = { days, start: null, end: null, unit };

    // Clear Custom Inputs
    const startInput = document.getElementById('start-date');
    const endInput = document.getElementById('end-date');
    if (startInput) startInput.value = '';
    if (endInput) endInput.value = '';

    // Update active button state
    const btns = document.querySelectorAll('.chart-controls .sm-btn');
    btns.forEach(btn => {
        // Check text content matches
        if (btn.textContent.includes(days === 1 ? '24h' : days + 'd')) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });

    // Refresh all dashboard data
    fetchDashboardData();
}

async function applyCustomDateRange() {
    const startInput = document.getElementById('start-date');
    const endInput = document.getElementById('end-date');
    const applyBtn = document.querySelector('.date-range-picker button');

    let start = startInput.value;
    let end = endInput.value;

    if (!start || !end) {
        alert("Please select both start and end dates.");
        return;
    }

    // Enforce end date <= today
    const today = new Date();
    const endDate = new Date(end);

    // Normalize today to YYYY-MM-DD local
    const year = today.getFullYear();
    const month = String(today.getMonth() + 1).padStart(2, '0');
    const day = String(today.getDate()).padStart(2, '0');
    const todayStr = `${year}-${month}-${day}`;
    const todayDate = new Date(todayStr);

    if (endDate > todayDate) {
        end = todayStr;
        endInput.value = end;
    }

    if (new Date(start) > new Date(end)) {
        alert("Start date cannot be after end date.");
        return;
    }

    // Update State
    currentGranularity = { days: null, start, end, unit: 'day' };

    // Update UI
    if (applyBtn) {
        const originalText = applyBtn.textContent;
        applyBtn.textContent = 'Applying...';
        applyBtn.disabled = true;

        // Remove active class from preset buttons
        document.querySelectorAll('.chart-controls .sm-btn').forEach(btn => btn.classList.remove('active'));

        try {
            await fetchDashboardData();
        } catch (e) {
            console.error("Error refreshing dashboard:", e);
        } finally {
            applyBtn.textContent = originalText;
            applyBtn.disabled = false;
        }
    } else {
        fetchDashboardData();
    }
}

function getDateParams() {
    if (currentGranularity.start && currentGranularity.end) {
        return `start_date=${currentGranularity.start}&end_date=${currentGranularity.end}`;
    }
    return `days=${currentGranularity.days}`;
}




async function fetchIngestionHistory() {
    if (!ingestionChart) return;

    try {
        const params = getDateParams();
        const res = await fetch(`/api/dashboard/patches/history?${params}&granularity=${currentGranularity.unit}`);
        const history = await res.json();
        updateIngestionChart(history);

    } catch (error) {
        console.error('Failed to fetch ingestion history:', error);
    }
}




// (Old mock schema/candidate functions removed — replaced by Patch Type Manager above)


function initClock() {
    const updateClock = () => {
        const now = new Date();
        const timeString = now.toLocaleTimeString('en-US', {
            hour12: false,
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        });

        const clock = document.getElementById('clock');
        if (clock) clock.textContent = timeString;

        const devClock = document.getElementById('clock-dev');
        if (devClock) devClock.textContent = timeString;
    };
    updateClock();
    setInterval(updateClock, 1000);
}

// User Quilt Logic
let allUsers = [];

async function initUserQuilt() {
    await fetchUsers();

    // Search listener
    const searchInput = document.getElementById('user-search-input');
    if (searchInput) {
        searchInput.addEventListener('input', (e) => {
            const term = e.target.value.toLowerCase();
            const filtered = allUsers.filter(u => u.toLowerCase().includes(term));
            renderUserList(filtered);
        });
    }
}

async function fetchUsers() {
    try {
        const res = await fetch('/api/dashboard/users');
        allUsers = await res.json();
        renderUserTable(allUsers);
    } catch (error) {
        console.error('Failed to fetch users:', error);
    }
}



// User Sorting Logic
let userSortState = {
    column: 'last_updated',
    direction: 'desc'
};

function sortUsers(column) {
    if (userSortState.column === column) {
        userSortState.direction = userSortState.direction === 'asc' ? 'desc' : 'asc';
    } else {
        userSortState.column = column;
        userSortState.direction = 'asc';
    }

    // Sort logic
    allUsers.sort((a, b) => {
        let valA = a[column] || '';
        let valB = b[column] || '';

        // Handle dates
        if (column === 'last_updated') {
            // Nulls last
            if (!valA) return 1;
            if (!valB) return -1;
            valA = new Date(valA).getTime();
            valB = new Date(valB).getTime();
        }
        // Handle numbers
        else if (column === 'patch_count') {
            valA = Number(valA);
            valB = Number(valB);
        }
        // Handle strings
        else {
            valA = String(valA).toLowerCase();
            valB = String(valB).toLowerCase();
        }

        if (valA < valB) return userSortState.direction === 'asc' ? -1 : 1;
        if (valA > valB) return userSortState.direction === 'asc' ? 1 : -1;
        return 0;
    });

    renderUserTable(allUsers);
    updateUserSortIcons();
}

function updateUserSortIcons() {
    // Reset all
    document.querySelectorAll('#users-view th i').forEach(icon => {
        icon.className = 'fa-solid fa-sort';
    });

    // Set active
    const activeHeader = document.querySelector(`#users-view th[onclick="sortUsers('${userSortState.column}')"] i`);
    if (activeHeader) {
        activeHeader.className = userSortState.direction === 'asc' ? 'fa-solid fa-sort-up' : 'fa-solid fa-sort-down';
    }
}

function renderUserTable(users) {
    const tbody = document.getElementById('users-table-body');
    if (!tbody) return;
    tbody.innerHTML = '';

    // Update count badge
    const countBadge = document.getElementById('directory-user-count');
    if (countBadge) {
        countBadge.textContent = users.length;
    }

    if (users.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-message">No users found.</td></tr>';
        return;
    }

    users.forEach(user => {
        const tr = document.createElement('tr');
        // user object: { user_id, display_name, email, patch_count, last_updated, last_provided }

        let lastUpdatedStr = '-';
        if (user.last_updated) {
            lastUpdatedStr = new Date(user.last_updated).toLocaleString();
        }

        // Show display_name if available, fall back to truncated user_id
        const userLabel = user.display_name || user.user_id.substring(0, 12) + '...';

        tr.innerHTML = `
            <td style="font-weight: 500; color: var(--text-primary);"><i class="fa-solid fa-user-circle" style="color: var(--primary-color); margin-right:8px;"></i> <span title="${user.user_id}">${userLabel}</span></td>
            <td style="color: var(--text-primary);">${user.display_name || '-'}</td>
            <td style="color: var(--text-primary);">${user.email || '-'}</td>
            <td><span class="badge" style="background: rgba(148, 163, 184, 0.1); color: #94a3b8;">${user.patch_count} Patches</span></td>
            <td style="color: var(--text-muted); font-size: 0.9em;">${lastUpdatedStr}</td>
            <td style="color: var(--text-muted); font-size: 0.9em;"><i class="fa-solid fa-clock-rotate-left"></i> ${user.last_provided}</td>
            <td>
                <button class="sm-btn active" onclick="loadUserQuilt('${user.user_id}')">View Quilt</button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

// User Quilt Data State
let currentUserPatches = [];
let currentUserQuilt = null;
let currentUserId = null;

function toggleCard(cardId) {
    const card = document.getElementById(cardId);
    if (!card) return;

    // Find body by convention: 'body-' + suffix of 'card-'
    const suffix = cardId.replace('card-', '');
    const bodyId = 'body-' + suffix;
    const body = document.getElementById(bodyId);

    // Find toggle icon in this card
    const icon = card.querySelector('.toggle-icon');

    if (body) {
        if (body.classList.contains('hidden')) {
            // EXPAND
            body.classList.remove('hidden');
            if (icon) icon.classList.remove('collapsed');
        } else {
            // COLLAPSE
            body.classList.add('hidden');
            if (icon) icon.classList.add('collapsed');
        }
    }
}

// Helper to set specific state
function setCardState(cardId, isOpen) {
    const card = document.getElementById(cardId);
    if (!card) return;
    const suffix = cardId.replace('card-', '');
    const bodyId = 'body-' + suffix;
    const body = document.getElementById(bodyId);
    const icon = card.querySelector('.toggle-icon');

    if (isOpen) {
        if (body) body.classList.remove('hidden');
        if (icon) icon.classList.remove('collapsed');
    } else {
        if (body) body.classList.add('hidden');
        if (icon) icon.classList.add('collapsed');
    }
}

async function loadUserQuilt(userId) {
    console.log("Loading quilt for:", userId);

    // 1. Collapse User Directory automatically
    setCardState('card-users', false);

    // 2. Show and Expand detail cards
    // document.getElementById('quilt-content-area').style.display = 'flex'; // REMOVED as it no longer exists
    document.getElementById('card-history').style.display = 'block'; // Ensure visible structure
    setCardState('card-history', true);

    document.getElementById('card-details').style.display = 'flex'; // Ensure visible
    setCardState('card-details', true);

    // Scroll top
    document.getElementById('users-view').scrollTo({ top: 0, behavior: 'smooth' });

    // Show loading state in details (optional, skipping for now)

    try {
        const res = await fetch(`/api/dashboard/users/${encodeURIComponent(userId)}/quilt`);
        if (!res.ok) throw new Error("Failed to load quilt");

        const quilt = await res.json();
        currentUserQuilt = quilt; // Store globally
        currentUserId = userId; // Store ID globally
        currentUserPatches = quilt.patches; // Update currentUserPatches for timeline

        renderQuiltView(quilt); // Pass the full quilt object

        // Render Timeline
        await updateUserTimelineRange(30);

    } catch (err) {
        console.error(err);
        alert("Failed to load user quilt: " + err.message);
    }
}

function updateUserTimelineRange(days) {
    renderUserTimeline(currentUserPatches, days);

    // Update active button state
    document.querySelectorAll('.timeline-controls button').forEach(btn => btn.classList.remove('active'));
    const btnId = `btn-timeline-${days}`;
    const btn = document.getElementById(btnId);
    if (btn) btn.classList.add('active');
}

function renderQuiltView(data) {
    // Switch views
    document.getElementById('quilt-empty-state').style.display = 'none';

    // Ensure cards are visible (they should be already handled by loadUserQuilt)
    document.getElementById('card-history').style.display = 'block';
    document.getElementById('card-details').style.display = 'flex';

    // Render Timeline
    renderUserTimeline(data.patches);

    // Header — show display_name if available, user_id as fallback
    const displayLabel = data.display_name || data.user_id;
    document.getElementById('quilt-user-id').textContent = displayLabel;
    document.getElementById('quilt-user-initial').textContent = displayLabel.charAt(0).toUpperCase();
    document.getElementById('quilt-patch-count').textContent = data.patches.length;
    // Mock Last Active
    document.getElementById('quilt-last-active').textContent = 'Just now';

    // Communication Profile
    const profileSection = document.getElementById('comm-profile-section');
    const profileBars = document.getElementById('comm-profile-bars');
    const profileSamples = document.getElementById('comm-profile-samples');
    if (data.communication_profile && profileBars) {
        const cp = data.communication_profile;
        const sampleCount = cp._sample_count || 1;
        profileSamples.textContent = `(${sampleCount} meeting${sampleCount !== 1 ? 's' : ''})`;

        const dimensions = [
            { key: 'verbosity', label: 'Verbosity', icon: 'fa-comment', low: 'Terse', high: 'Detailed' },
            { key: 'directness', label: 'Directness', icon: 'fa-bullseye', low: 'Hedging', high: 'Decisive' },
            { key: 'formality', label: 'Formality', icon: 'fa-user-tie', low: 'Casual', high: 'Formal' },
            { key: 'technical_level', label: 'Technical', icon: 'fa-microchip', low: 'Layperson', high: 'Expert' },
            { key: 'warmth', label: 'Warmth', icon: 'fa-heart', low: 'Transactional', high: 'Friendly' },
            { key: 'detail_orientation', label: 'Detail', icon: 'fa-magnifying-glass', low: 'Vague', high: 'Precise' },
        ];

        profileBars.innerHTML = dimensions.map(d => {
            const score = cp[d.key];
            if (score == null) return '';
            const pct = Math.round(score * 100);
            const hue = 200 + (score * 60); // blue to purple gradient
            return `
                <div style="display: flex; align-items: center; gap: 0.5rem;">
                    <i class="fa-solid ${d.icon}" style="color: var(--text-muted); width: 16px; text-align: center; font-size: 0.75rem;"></i>
                    <span style="width: 70px; font-size: 0.8rem; color: var(--text-secondary);">${d.label}</span>
                    <div style="flex: 1; background: rgba(15,23,42,0.6); border-radius: 4px; height: 8px; position: relative;">
                        <div style="width: ${pct}%; height: 100%; background: hsl(${hue}, 60%, 55%); border-radius: 4px; transition: width 0.3s;"></div>
                    </div>
                    <span style="width: 32px; text-align: right; font-size: 0.75rem; font-family: var(--font-mono); color: var(--text-muted);">${score.toFixed(1)}</span>
                </div>
            `;
        }).join('');

        profileSection.style.display = 'block';
    } else if (profileSection) {
        profileSection.style.display = 'none';
    }

    // Render Patches Table
    renderQuiltPatchTable(data.patches);

    // Timeline List (Side)
    const timeline = document.getElementById('quilt-timeline');
    if (timeline) {
        timeline.innerHTML = '';
        data.timeline.forEach(event => {
            const item = document.createElement('div');
            item.className = 'timeline-item';
            item.innerHTML = `
                <div class="timeline-dot"></div>
                <div class="timeline-date">${event.date}</div>
                <div class="timeline-content">${event.description}</div>
            `;
            timeline.appendChild(item);
        });
    }
}

function renderQuiltPatchTable(patches) {
    const tbody = document.getElementById('quilt-patch-table-body');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (patches.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-message">No patches found.</td></tr>';
        return;
    }

    patches.forEach(patch => {
        const tr = document.createElement('tr');

        // Format Time
        const timeStr = new Date(patch.created_at).toLocaleString();

        // Format Value
        let valDisplay = patch.value;
        if (typeof patch.value === 'object' && patch.value !== null) {
            valDisplay = JSON.stringify(patch.value);
        }

        // Type badge style
        let typeColor = '#94a3b8';
        let typeBg = 'rgba(148, 163, 184, 0.1)';

        if (patch.patch_type === 'identity') { typeColor = '#3b82f6'; typeBg = 'rgba(59, 130, 246, 0.1)'; }
        if (patch.patch_type === 'preference') { typeColor = '#10b981'; typeBg = 'rgba(16, 185, 129, 0.1)'; }
        if (patch.patch_type === 'trait') { typeColor = '#8b5cf6'; typeBg = 'rgba(139, 92, 246, 0.1)'; }
        if (patch.patch_type === 'experience') { typeColor = '#f59e0b'; typeBg = 'rgba(245, 158, 11, 0.1)'; }

        // Origin badge style
        let originBadge = `<span class="badge" style="background: rgba(255,255,255,0.05); color: var(--text-muted);">${patch.origin_mode}</span>`;
        if (patch.origin_mode === 'inferred') {
            originBadge = `<span class="badge" style="background: rgba(139, 92, 246, 0.1); color: #a78bfa;">inferred</span>`;
        }

        // Icon mapping
        let icon = 'fa-circle';
        if (patch.patch_type === 'identity') icon = 'fa-id-card';
        if (patch.patch_type === 'preference') icon = 'fa-heart';
        if (patch.patch_type === 'trait') icon = 'fa-wand-magic-sparkles';
        if (patch.patch_type === 'experience') icon = 'fa-clock-rotate-left';

        tr.innerHTML = `
            <td style="color: var(--text-muted); font-size: 0.85rem; white-space: nowrap;">${timeStr}</td>
            <td style="color: var(--text-primary); font-weight: 500;">${patch.user_id}</td>
            <td>${originBadge}</td>
            <td><span class="badge" style="background: ${typeBg}; color: ${typeColor};"><i class="fa-solid ${icon}"></i> ${patch.patch_type}</span></td>
            <td style="font-family: var(--font-mono); color: var(--text-primary);">${patch.patch_name}</td>
            <td style="font-family: var(--font-mono); color: var(--text-muted); font-size: 0.85rem;">${patch.source_prompt}</td>
            <td style="color: var(--text-muted); font-size: 0.85rem;">${patch.confidence}</td>
            <td style="color: var(--text-muted); font-size: 0.85rem;">${patch.sensitivity}</td>
            <td style="font-family: var(--font-mono); font-size: 0.85rem; color: var(--text-secondary); max-width: 400px; overflow-wrap: break-word;">${valDisplay}</td>
            <td style="white-space: nowrap;">
                <button onclick="editPatch('${patch.patch_id}', '${patch.user_id}')" class="btn-icon" title="Edit" style="background: rgba(59, 130, 246, 0.1); color: #3b82f6; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; margin-right: 4px;">
                    <i class="fa-solid fa-pen-to-square"></i>
                </button>
                <button onclick="deletePatch('${patch.patch_id}', '${patch.user_id}')" class="btn-icon" title="Delete" style="background: rgba(239, 68, 68, 0.1); color: #ef4444; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer;">
                    <i class="fa-solid fa-trash"></i>
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

// Patch Management Functions
async function editPatch(patchId, userId) {
    const currentFact = prompt('Edit this patch fact:');
    if (currentFact === null) return; // User cancelled

    try {
        const response = await fetch(`/api/dashboard/patches/${patchId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ fact: currentFact })
        });
        if (!response.ok) throw new Error(`Failed to update patch: ${response.statusText}`);
        // Refresh the quilt view
        if (userId) {
            const quiltResponse = await fetch(`/api/dashboard/users/${userId}/quilt`);
            if (quiltResponse.ok) {
                const data = await quiltResponse.json();
                currentUserPatches = data.patches;
                renderQuiltPatchTable(data.patches);
            }
        }
    } catch (err) {
        alert('Failed to update patch: ' + err.message);
    }
}

async function deletePatch(patchId, userId) {
    if (!confirm('Delete this patch? This cannot be undone.')) return;

    try {
        const response = await fetch(`/api/dashboard/patches/${patchId}`, {
            method: 'DELETE'
        });
        if (!response.ok) throw new Error(`Failed to delete patch: ${response.statusText}`);
        // Refresh the quilt view
        if (userId) {
            const quiltResponse = await fetch(`/api/dashboard/users/${userId}/quilt`);
            if (quiltResponse.ok) {
                const data = await quiltResponse.json();
                currentUserPatches = data.patches;
                renderQuiltPatchTable(data.patches);
            }
        }
    } catch (err) {
        alert('Failed to delete patch: ' + err.message);
    }
}

// Global Sort State for Quilt Table
let currentQuiltSortState = {
    column: 'created_at',
    direction: 'desc'
};

function sortQuiltData(column) {
    if (currentQuiltSortState.column === column) {
        currentQuiltSortState.direction = currentQuiltSortState.direction === 'asc' ? 'desc' : 'asc';
    } else {
        currentQuiltSortState.column = column;
        currentQuiltSortState.direction = 'asc';
    }

    // Sort
    if (!currentUserPatches) return;

    currentUserPatches.sort((a, b) => {
        let valA = a[column];
        let valB = b[column];

        if (column === 'created_at') {
            valA = new Date(valA || 0).getTime();
            valB = new Date(valB || 0).getTime();
        }
        else if (column === 'confidence') {
            valA = Number(valA || 0);
            valB = Number(valB || 0);
        }
        else {
            valA = String(valA || '').toLowerCase();
            valB = String(valB || '').toLowerCase();
        }

        if (valA < valB) return currentQuiltSortState.direction === 'asc' ? -1 : 1;
        if (valA > valB) return currentQuiltSortState.direction === 'asc' ? 1 : -1;
        return 0;
    });

    renderQuiltPatchTable(currentUserPatches);
    updateQuiltSortIcons();
}

function updateQuiltSortIcons() {
    document.querySelectorAll('#card-details th i').forEach(icon => {
        icon.className = 'fa-solid fa-sort';
    });

    const activeHeader = document.querySelector(`#card-details th[onclick="sortQuiltData('${currentQuiltSortState.column}')"] i`);
    if (activeHeader) {
        activeHeader.className = currentQuiltSortState.direction === 'asc' ? 'fa-solid fa-sort-up' : 'fa-solid fa-sort-down';
    }
}


// Application Access Logic
function initApplicationAccess() {
    fetchApplications();
}

async function fetchApplications() {
    try {
        const res = await fetch('/v1/auth/apps');
        const apps = await res.json();
        const tbody = document.getElementById('dev-apps-body');
        if (!tbody) return;
        tbody.innerHTML = '';

        apps.forEach(app => {
            const tr = document.createElement('tr');
            const created = new Date(app.created_at).toLocaleDateString();
            const checked = app.enforce_auth ? 'checked' : '';

            tr.innerHTML = `
                <td style="font-weight: 500; color: var(--text-primary);">${app.app_name}</td>
                <td style="font-family: var(--font-mono); color: var(--text-muted);">
                    ${app.app_id}
                    <button class="icon-btn" onclick="navigator.clipboard.writeText('${app.app_id}')" title="Copy ID" style="margin-left: 0.5rem;">
                        <i class="fa-regular fa-copy"></i>
                    </button>
                </td>
                <td>
                    <label class="switch">
                        <input type="checkbox" ${checked} onchange="toggleEnforcement('${app.app_id}', this.checked)">
                        <span class="slider round"></span>
                    </label>
                </td>
                <td style="color: var(--text-muted); font-size: 0.875rem;">${created}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (error) {
        console.error('Failed to fetch apps:', error);
    }
}

async function registerApplication() {
    const nameInput = document.getElementById('new-app-name');
    const name = nameInput.value.trim();
    if (!name) return alert("Please enter an application name");

    try {
        const res = await fetch('/v1/auth/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ app_name: name })
        });

        if (!res.ok) throw new Error("Registration failed");

        const data = await res.json();

        // Show credentials
        document.getElementById('created-client-id').value = data.app_id;
        document.getElementById('created-client-secret').value = data.client_secret;
        document.getElementById('new-app-credentials').style.display = 'block';

        // Clear input and refresh list
        nameInput.value = '';
        fetchApplications();

    } catch (error) {
        alert("Failed to register application: " + error.message);
    }
}

async function toggleEnforcement(appId, enforce) {
    try {
        const res = await fetch(`/v1/auth/apps/${appId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enforce_auth: enforce })
        });
        if (!res.ok) throw new Error("Update failed");
    } catch (error) {
        alert("Failed to update enforcement: " + error.message);
        fetchApplications(); // Refresh to reset state
    }
}

function copyToClipboard(elementId) {
    const copyText = document.getElementById(elementId);
    copyText.select();
    copyText.setSelectionRange(0, 99999);
    navigator.clipboard.writeText(copyText.value);
}

let ingestionChart;
let categoryChart;
let currentFacts = []; // Store current facts for sorting
let currentDistributionGroup = 'patch_type'; // Track current distribution group
let sortState = {
    column: 'created_at',
    direction: 'desc' // 'asc' or 'desc'
};

function initCharts() {
    // 1. Ingestion Rate Chart (Line)
    const canvas1 = document.getElementById('ingestionChart');
    if (!canvas1) return; // Guard if not in view (though charts are init on load)

    const ctx1 = canvas1.getContext('2d');

    // Gradient for line chart
    const gradient = ctx1.createLinearGradient(0, 0, 0, 400);
    gradient.addColorStop(0, 'rgba(6, 182, 212, 0.5)');
    gradient.addColorStop(1, 'rgba(6, 182, 212, 0.0)');

    ingestionChart = new Chart(ctx1, {
        type: 'bar',
        data: {
            labels: [], // To be populated
            labels: [],
            datasets: [
                {
                    label: 'Identity',
                    data: [],
                    backgroundColor: '#3b82f6', // Blue
                    borderRadius: 2
                },
                {
                    label: 'Preference',
                    data: [],
                    backgroundColor: '#10b981', // Green
                    borderRadius: 2
                },
                {
                    label: 'Trait',
                    data: [],
                    backgroundColor: '#8b5cf6', // Purple
                    borderRadius: 2
                },
                {
                    label: 'Experience',
                    data: [],
                    backgroundColor: '#f59e0b', // Orange
                    borderRadius: 2
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false }, // Custom legend used
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    backgroundColor: 'rgba(15, 23, 42, 0.95)',
                    titleColor: '#f8fafc',
                    bodyColor: '#cbd5e1',
                    borderColor: 'rgba(148, 163, 184, 0.1)',
                    borderWidth: 1,
                    callbacks: {
                        label: function (context) {
                            return ` ${context.dataset.label}: ${context.raw}`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    stacked: true,
                    grid: { display: false },
                    ticks: { color: '#64748b' }
                },
                y: {
                    stacked: true,
                    grid: { color: 'rgba(148, 163, 184, 0.05)' },
                    ticks: { color: '#64748b' },
                    beginAtZero: true,
                    title: {
                        display: true,
                        text: 'Total Patches Added'
                    }
                }
            }
        }
    });

    // 2. Category Chart (Doughnut)
    const canvas2 = document.getElementById('categoryChart');
    if (!canvas2) return;

    const ctx2 = canvas2.getContext('2d');
    categoryChart = new Chart(ctx2, {
        type: 'doughnut',
        data: {
            labels: [],
            datasets: [{
                data: [],
                backgroundColor: [
                    '#3b82f6', // Blue
                    '#10b981', // Green
                    '#8b5cf6', // Purple
                    '#f59e0b', // Amber
                    '#ef4444', // Red
                    '#ec4899', // Pink
                    '#6366f1', // Indigo
                    '#14b8a6', // Teal
                    '#f97316', // Orange
                    '#64748b'  // Slate
                ],
                borderWidth: 0,
                hoverOffset: 4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'right',
                    labels: { color: '#94a3b8', usePointStyle: true, pointStyle: 'circle' }
                }
            },
            cutout: '70%'
        }
    });

    // 3. Time Scale Buttons (Handled via onclick in HTML)


    // 4. Distribution Selector
    const distSelector = document.getElementById('distribution-selector');
    if (distSelector) {
        distSelector.addEventListener('change', (e) => {
            currentDistributionGroup = e.target.value;
            fetchDistribution(currentDistributionGroup);
        });
    }
}

async function initTypeFilter() {
    // Static list of patch types
    const types = ['identity', 'preference', 'trait', 'experience'];
    const select = document.getElementById('patch-filter');
    if (!select) return;

    types.forEach(type => {
        const option = document.createElement('option');
        option.value = type;
        option.textContent = type.charAt(0).toUpperCase() + type.slice(1);
        select.appendChild(option);
    });
}

async function fetchDashboardData() {
    const promises = [];

    // 1. Fetch Stats
    promises.push(fetchStats().catch(e => console.error('Stats failed:', e)));

    // 2. Fetch Recent Patches
    promises.push((async () => {
        const patchFilter = document.getElementById('patch-filter');
        if (patchFilter) {
            const filterVal = patchFilter.value;
            const patchesUrl = filterVal ? `/api/dashboard/patches/recent?patch_type=${encodeURIComponent(filterVal)}` : '/api/dashboard/patches/recent';
            const patchesRes = await fetch(patchesUrl);
            currentFacts = await patchesRes.json();

            // Apply current sort
            sortFacts();
            updateRecentFactsTable(currentFacts);
        }
    })().catch(e => console.error('Recent patches failed:', e)));

    // 3. Fetch History (Chart)
    promises.push(fetchIngestionHistory().catch(e => console.error('Ingestion history failed:', e)));

    // 4. Fetch Distribution (Chart)
    promises.push(fetchDistribution(currentDistributionGroup).catch(e => console.error('Distribution failed:', e)));

    await Promise.allSettled(promises);
}

async function fetchStats() {
    try {
        const params = getDateParams();
        const statsRes = await fetch(`/api/dashboard/stats?${params}`);
        const stats = await statsRes.json();
        const totalUsers = document.getElementById('total-users');
        if (totalUsers) totalUsers.textContent = stats.total_users || 0;

        const totalFacts = document.getElementById('total-facts');
        if (totalFacts) totalFacts.textContent = stats.total_facts || 0;
    } catch (error) {
        console.error('Failed to fetch stats:', error);
    }
}





function updateRecentFactsTable(patches) {
    const tbody = document.getElementById('recent-facts-body');
    if (!tbody) return;
    tbody.innerHTML = '';

    patches.forEach(patch => {
        const tr = document.createElement('tr');
        const date = new Date(patch.created_at).toLocaleTimeString();
        const originClass = patch.origin === 'inferred' ? 'background: rgba(139,92,246,0.3);' : 'background: rgba(16,185,129,0.3);';
        let typeColor = 'rgba(100, 116, 139, 0.2)'; // Default Slate
        let typeIcon = 'fa-circle-question';
        let typeLabel = patch.patch_type || 'unknown';

        const type = (patch.patch_type || '').toLowerCase();

        if (type === 'identity') {
            typeColor = 'rgba(30, 58, 138, 0.3)'; // Deep Blue
            typeIcon = 'fa-id-card';
            typeLabel = 'Identity';
        } else if (type === 'preference') {
            typeColor = 'rgba(16, 185, 129, 0.3)'; // Green
            typeIcon = 'fa-heart';
            typeLabel = 'Preference';
        } else if (type === 'trait') {
            typeColor = 'rgba(139, 92, 246, 0.3)'; // Purple
            typeIcon = 'fa-wand-magic-sparkles';
            typeLabel = 'Trait';
        } else if (type === 'experience') {
            typeColor = 'rgba(245, 158, 11, 0.3)'; // Amber
            typeIcon = 'fa-clock-rotate-left';
            typeLabel = 'Experience';
        }

        let valueStr = typeof patch.value === 'object' ? JSON.stringify(patch.value) : String(patch.value);

        tr.innerHTML = `
            <td style="color: var(--text-muted); font-family: var(--font-mono);">${date}</td>
            <td>${patch.user_id}</td>
            <td><span class="badge" style="${originClass}">${patch.origin || 'unknown'}</span></td>
            <td>
                <span class="badge" style="background: ${typeColor}; display: inline-flex; align-items: center; gap: 0.5rem;">
                    <i class="fa-solid ${typeIcon}"></i> ${typeLabel}
                </span>
            </td>
            <td style="font-family: var(--font-mono); color: var(--text-primary);">${patch.patch_name}</td>
            <td style="max-width: 250px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${valueStr}</td>
        `;
        tbody.appendChild(tr);
    });
}

function updateIngestionChart(history) {
    if (!ingestionChart) return;

    const labels = history.map(h => {
        // Check if date is already in HH:MM format (simple heuristic)
        if (h.date.includes(':') && h.date.length === 5) {
            return h.date;
        }
        const d = new Date(h.date);
        return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    });

    const identityData = history.map(h => h.counts ? (h.counts.identity || 0) : (h.count || 0));
    const preferenceData = history.map(h => h.counts ? (h.counts.preference || 0) : 0);
    const traitData = history.map(h => h.counts ? (h.counts.trait || 0) : 0);
    const experienceData = history.map(h => h.counts ? (h.counts.experience || 0) : 0);

    ingestionChart.data.labels = labels;
    ingestionChart.data.datasets[0].data = identityData;
    ingestionChart.data.datasets[1].data = preferenceData;
    ingestionChart.data.datasets[2].data = traitData;
    ingestionChart.data.datasets[3].data = experienceData;

    ingestionChart.update();
}

function updateCategoryChart(distribution) {
    if (!categoryChart) return;
    // Expected patch type order
    const typeOrder = ['identity', 'preference', 'trait', 'experience'];

    // Sort distribution if keys match the expected types
    distribution.sort((a, b) => {
        const labelA = (a.label || '').toLowerCase();
        const labelB = (b.label || '').toLowerCase();
        const idxA = typeOrder.indexOf(labelA);
        const idxB = typeOrder.indexOf(labelB);

        // If both are in the known list, sort by index
        if (idxA !== -1 && idxB !== -1) return idxA - idxB;
        // If only A is known, it comes first
        if (idxA !== -1) return -1;
        // If only B is known, it comes first
        if (idxB !== -1) return 1;
        // Otherwise sort alphabetically
        return labelA.localeCompare(labelB);
    });

    // Expecting distribution = [{ label: 'preference', count: 10 }, ...]
    const labels = distribution.map(c => {
        const lbl = c.label || 'Unknown';
        // Capitalize if it's one of our known types
        if (typeOrder.includes(lbl.toLowerCase())) {
            return lbl.charAt(0).toUpperCase() + lbl.slice(1);
        }
        return lbl;
    });
    const data = distribution.map(c => c.count);

    // Color Mapping for Consistency
    const colorMap = {
        'identity': '#3b82f6',       // Blue
        'preference': '#10b981',     // Green
        'trait': '#8b5cf6',          // Purple
        'experience': '#f59e0b',     // Amber/Orange
        // Semantic/Origin mappings
        'inferred': '#8b5cf6',
        'declared': '#10b981',
        'auto_generated': '#f59e0b'
    };

    const fallbackColors = [
        '#ef4444', // Red
        '#ec4899', // Pink
        '#6366f1', // Indigo
        '#14b8a6', // Teal
        '#f97316', // Orange
        '#64748b'  // Slate
    ];

    const backgroundColors = labels.map((label, index) => {
        const key = String(label).toLowerCase();
        if (colorMap[key]) return colorMap[key];
        // Use hash or index to pick fallback ?? Index is fine for now
        return fallbackColors[index % fallbackColors.length];
    });

    categoryChart.data.labels = labels;
    categoryChart.data.datasets[0].data = data;
    categoryChart.data.datasets[0].backgroundColor = backgroundColors;
    categoryChart.update();
}

async function fetchDistribution(groupBy) {
    try {
        const params = getDateParams();
        const res = await fetch(`/api/dashboard/patches/distribution?group_by=${groupBy}&${params}`);
        const distribution = await res.json();
        updateCategoryChart(distribution);
    } catch (error) {
        console.error('Failed to fetch distribution:', error);
    }
}


function sortData(column) {
    // Toggle direction if clicking same column
    if (sortState.column === column) {
        sortState.direction = sortState.direction === 'asc' ? 'desc' : 'asc';
    } else {
        sortState.column = column;
        sortState.direction = 'asc'; // Default to asc for new column
    }

    sortFacts();
    updateRecentFactsTable(currentFacts);
    updateSortIcons();
}

function sortFacts() {
    currentFacts.sort((a, b) => {
        let valA = a[sortState.column] || '';
        let valB = b[sortState.column] || '';

        // Handle dates
        if (sortState.column === 'created_at') {
            valA = new Date(valA).getTime();
            valB = new Date(valB).getTime();
        } else {
            // Case insensitive string sort
            valA = String(valA).toLowerCase();
            valB = String(valB).toLowerCase();
        }

        if (valA < valB) return sortState.direction === 'asc' ? -1 : 1;
        if (valA > valB) return sortState.direction === 'asc' ? 1 : -1;
        return 0;
    });
}

function updateSortIcons() {
    // Reset all icons
    document.querySelectorAll('th.sortable i').forEach(icon => {
        icon.className = 'fa-solid fa-sort';
    });

    // Update active icon
    const activeHeader = document.querySelector(`th[onclick = "sortData('${sortState.column}')"] i`);
    if (activeHeader) {
        activeHeader.className = sortState.direction === 'asc' ? 'fa-solid fa-sort-up' : 'fa-solid fa-sort-down';
    }
}

// -- User Quilt Timeline Chart --
let timelineChart = null;

function renderUserTimeline(patches, days = 30) {
    const canvas = document.getElementById('userTimelineChart');
    if (!canvas) return;

    // Destroy previous instance
    if (timelineChart) {
        timelineChart.destroy();
        timelineChart = null;
    }

    // Initialize buckets for last 'days'
    const stats = {};
    const dates = [];

    const today = new Date();
    today.setHours(0, 0, 0, 0);

    for (let i = days - 1; i >= 0; i--) {
        const d = new Date(today);
        d.setDate(d.getDate() - i);
        const label = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        dates.push(label);
        stats[label] = { identity: 0, preference: 0, trait: 0, experience: 0 };
    }

    // Fill Buckets
    // Fill Buckets
    patches.forEach(p => {
        const dateStr = p.created_at || p.updated_at;
        if (!dateStr) return;
        const d = new Date(dateStr);
        const label = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

        // Only count if it falls within our generated buckets
        if (stats[label]) {
            const type = (p.patch_type || 'trait').toLowerCase();
            if (stats[label][type] !== undefined) stats[label][type]++;
        }
    });

    // Extract datasets
    const identityData = dates.map(d => stats[d].identity);
    const prefData = dates.map(d => stats[d].preference);
    const traitData = dates.map(d => stats[d].trait);
    const expData = dates.map(d => stats[d].experience);

    const ctx = canvas.getContext('2d');
    timelineChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: dates,
            datasets: [
                { label: 'Identity', data: identityData, backgroundColor: '#3b82f6', borderRadius: 2, stack: 'Stack 0' },
                { label: 'Preference', data: prefData, backgroundColor: '#10b981', borderRadius: 2, stack: 'Stack 0' },
                { label: 'Trait', data: traitData, backgroundColor: '#8b5cf6', borderRadius: 2, stack: 'Stack 0' },
                { label: 'Experience', data: expData, backgroundColor: '#f59e0b', borderRadius: 2, stack: 'Stack 0' }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false,
            },
            plugins: {
                legend: {
                    display: true,
                    position: 'bottom',
                    labels: { color: '#94a3b8', usePointStyle: true, boxWidth: 8, padding: 20 }
                },
                tooltip: {
                    backgroundColor: 'rgba(15, 23, 42, 0.9)',
                    titleColor: '#f8fafc',
                    bodyColor: '#cbd5e1',
                    borderColor: 'rgba(148, 163, 184, 0.1)',
                    borderWidth: 1
                }
            },
            scales: {
                x: {
                    stacked: true,
                    grid: { display: false },
                    ticks: {
                        color: '#94a3b8',
                        maxRotation: 0,
                        autoSkip: true,
                        maxTicksLimit: 10 // Prevent overcrowding
                    }
                },
                y: {
                    stacked: true,
                    grid: { color: 'rgba(148, 163, 184, 0.1)' },
                    ticks: { color: '#94a3b8', precision: 0 },
                    beginAtZero: true
                }
            }
        }
    });
}

// Pipeline Playground Logic
function loadSampleConversation() {
    const sample = [
        {
            "role": "user",
            "content": "Hi, I'm Sarah. I'm a frontend developer working on a React project."
        },
        {
            "role": "assistant",
            "content": "Hello Sarah! How can I help you with your React project today?"
        },
        {
            "role": "user",
            "content": "I prefer using Tailwind CSS over Bootstrap. Can you help me set up a dark mode toggle?"
        }
    ];
    const input = document.getElementById('playground-input');
    if (input) {
        input.value = JSON.stringify(sample, null, 2);
    }
}

async function runPipelineTest() {
    const input = document.getElementById('playground-input');
    const resultDiv = document.getElementById('playground-results');
    const statusBadge = document.getElementById('pipeline-status');
    const tbody = document.getElementById('playground-patches-body');
    const rawDiv = document.getElementById('playground-raw');

    if (!input || !input.value.trim()) {
        alert("Please enter a conversation.");
        return;
    }

    // 1. Parse Input
    let messages = [];
    try {
        messages = JSON.parse(input.value);
        if (!Array.isArray(messages)) throw new Error("Not an array");
    } catch (e) {
        messages = [{ "role": "user", "content": input.value }];
    }

    // 2. Reset UI
    if (resultDiv) resultDiv.style.display = 'none';

    // Reset steps & metrics
    ['picker', 'stitcher', 'designer', 'cataloger'].forEach(step => {
        const el = document.getElementById(`step-${step}`);
        if (el) {
            el.className = 'step';
            const metrics = el.querySelector('.step-metrics');
            if (metrics) {
                metrics.querySelector('.val-time').textContent = '--';
                metrics.querySelector('.val-tokens').textContent = '--';
            }
        }
    });
    // Reset connectors
    ['1', '2', '3'].forEach(i => {
        const el = document.getElementById(`conn-${i}`);
        if (el) el.className = 'connector';
    });

    if (statusBadge) {
        statusBadge.style.display = 'inline-block';
        statusBadge.textContent = 'Processing...';
        statusBadge.className = 'status-badge warning';
    }

    try {
        // 3. Streaming API Call
        const res = await fetch('/api/dashboard/test-pipeline', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ messages })
        });

        if (!res.ok) throw new Error(res.statusText);

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let finalData = null;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop(); // Keep partial line

            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed || !trimmed.startsWith('data: ')) continue;

                try {
                    const msg = JSON.parse(trimmed.substring(6));

                    if (msg.type === 'step_start') {
                        const el = document.getElementById(`step-${msg.step}`);
                        if (el) el.classList.add('active');
                    }
                    else if (msg.type === 'step_complete') {
                        const el = document.getElementById(`step-${msg.step}`);
                        if (el) {
                            el.classList.remove('active');
                            el.classList.add('done');

                            // Update Metrics
                            const metrics = el.querySelector('.step-metrics');
                            if (metrics) {
                                metrics.querySelector('.val-time').textContent = msg.time_ms + 'ms';
                                metrics.querySelector('.val-tokens').textContent = msg.tokens;
                            }
                        }

                        // Activate Connector to next step
                        let connId = null;
                        if (msg.step === 'picker') connId = '1';
                        if (msg.step === 'stitcher') connId = '2';
                        if (msg.step === 'designer') connId = '3';

                        if (connId) {
                            const conn = document.getElementById(`conn-${connId}`);
                            if (conn) conn.classList.add('active');
                        }
                    }
                    else if (msg.type === 'result') {
                        finalData = msg;
                    }
                    else if (msg.type === 'error') {
                        throw new Error(msg.message);
                    }
                } catch (jsonErr) {
                    console.error("Error parsing stream chunk", jsonErr);
                }
            }
        }

        // 4. Render Final Results
        if (finalData && tbody) {
            tbody.innerHTML = '';
            const patches = finalData.patches || [];

            if (patches.length > 0) {
                patches.forEach(patch => {
                    const tr = document.createElement('tr');

                    let typeColor = '#94a3b8';
                    let typeBg = 'rgba(148, 163, 184, 0.1)';
                    if (patch.patch_type === 'identity') { typeColor = '#3b82f6'; typeBg = 'rgba(59, 130, 246, 0.1)'; }
                    if (patch.patch_type === 'preference') { typeColor = '#10b981'; typeBg = 'rgba(16, 185, 129, 0.1)'; }
                    if (patch.patch_type === 'trait') { typeColor = '#8b5cf6'; typeBg = 'rgba(139, 92, 246, 0.1)'; }
                    if (patch.patch_type === 'experience') { typeColor = '#f59e0b'; typeBg = 'rgba(245, 158, 11, 0.1)'; }

                    const promptName = patch.source_prompt || 'detective';

                    tr.innerHTML = `
                        <td style="color: var(--text-secondary); text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em;">${promptName}</td>
                        <td style="font-weight: 500; color: var(--text-primary);">${patch.patch_name}</td>
                        <td><span class="badge" style="background: ${typeBg}; color: ${typeColor};">${patch.patch_type}</span></td>
                        <td style="font-family: var(--font-mono); color: var(--text-muted); font-size: 0.8rem;">${patch.value}</td>
                    `;
                    tbody.appendChild(tr);
                });
            } else {
                tbody.innerHTML = '<tr><td colspan="4" class="empty-message">No extracted facts found.</td></tr>';
            }

            if (rawDiv) rawDiv.textContent = finalData.raw_response || "No raw response.";
            if (resultDiv) resultDiv.style.display = 'block';

            if (statusBadge) {
                statusBadge.textContent = 'Complete';
                statusBadge.className = 'status-badge success';
            }
        }

    } catch (error) {
        console.error(error);
        alert("Pipeline Test Failed:\n" + error.message);

        document.querySelectorAll('.step.active').forEach(el => {
            el.classList.add('error');
            el.classList.remove('active');
        });

        if (statusBadge) {
            statusBadge.textContent = 'Failed';
            statusBadge.className = 'status-badge error';
        }
    }
}
