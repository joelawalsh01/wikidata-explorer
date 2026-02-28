// --- Wikidata Knowledge Graph Explorer — Frontend ---

let cy = null;          // Cytoscape instance
let selectedNodes = new Set();  // QIDs of shift-selected nodes
const expandedNodes = new Set(); // QIDs already expanded

const DEPTH_COLORS = {
    0: '#f08080',  // lightcoral
    1: '#87ceeb',  // skyblue
    2: '#90ee90',  // lightgreen
};
const DEPTH_COLOR_DEFAULT = '#dda0dd'; // plum for depth 3+

const HUB_SITELINKS_THRESHOLD = 50;

function status(msg) {
    document.getElementById('status-bar').textContent = msg;
}

// --- Cytoscape Setup ---

function initCytoscape() {
    cy = cytoscape({
        container: document.getElementById('cy'),
        style: [
            {
                selector: 'node',
                style: {
                    'label': 'data(label)',
                    'text-wrap': 'wrap',
                    'text-max-width': '100px',
                    'font-size': '11px',
                    'color': '#e0e0e0',
                    'text-outline-color': '#1a1a2e',
                    'text-outline-width': 2,
                    'background-color': function(ele) {
                        var d = ele.data('depth');
                        if (d === 0) return DEPTH_COLORS[0];
                        if (d === 1) return DEPTH_COLORS[1];
                        if (d === 2) return DEPTH_COLORS[2];
                        return DEPTH_COLOR_DEFAULT;
                    },
                    'width': function(ele) {
                        return ele.data('depth') === 0 ? 60 : 40;
                    },
                    'height': function(ele) {
                        return ele.data('depth') === 0 ? 60 : 40;
                    },
                    'border-width': function(ele) {
                        // Hub indicator: thick gold border for high-sitelinks nodes
                        return ele.data('sitelinks') >= HUB_SITELINKS_THRESHOLD ? 4 : 1;
                    },
                    'border-color': function(ele) {
                        return ele.data('sitelinks') >= HUB_SITELINKS_THRESHOLD ? '#ffd700' : '#555';
                    },
                }
            },
            {
                selector: 'node.selected-node',
                style: {
                    'border-width': 4,
                    'border-color': '#e94560',
                }
            },
            {
                selector: 'edge',
                style: {
                    'label': 'data(label)',
                    'font-size': '9px',
                    'color': '#aaa',
                    'text-rotation': 'autorotate',
                    'text-outline-color': '#1a1a2e',
                    'text-outline-width': 1.5,
                    'line-color': '#444',
                    'target-arrow-color': '#444',
                    'target-arrow-shape': 'triangle',
                    'curve-style': 'bezier',
                    'width': 1.5,
                    'arrow-scale': 0.8,
                }
            }
        ],
        layout: { name: 'preset' },
        wheelSensitivity: 0.3,
    });

    // Click to expand, shift+click to select
    cy.on('tap', 'node', function(evt) {
        var node = evt.target;
        var qid = node.data('qid');

        if (evt.originalEvent.shiftKey) {
            // Toggle selection
            if (selectedNodes.has(qid)) {
                selectedNodes.delete(qid);
                node.removeClass('selected-node');
            } else {
                selectedNodes.add(qid);
                node.addClass('selected-node');
            }
            updateTriplesSidebar();
        } else {
            // Expand
            expandNode(qid);
        }
    });

    document.getElementById('graph-hint').style.display = 'none';
}

function runLayout() {
    cy.layout({
        name: 'cose',
        animate: true,
        animationDuration: 600,
        nodeRepulsion: function() { return 8000; },
        idealEdgeLength: function() { return 120; },
        gravity: 0.3,
        padding: 40,
    }).run();
}

// --- Search ---

document.getElementById('search-btn').addEventListener('click', doSearch);
document.getElementById('search-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') doSearch();
});

async function doSearch() {
    var term = document.getElementById('search-input').value.trim();
    if (!term) return;

    status('Searching...');
    var resultsDiv = document.getElementById('search-results');
    resultsDiv.innerHTML = '';
    resultsDiv.classList.remove('visible');

    try {
        var resp = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ term: term }),
        });
        var data = await resp.json();

        if (data.error) {
            status('Error: ' + data.error);
            return;
        }

        if (!data.results || data.results.length === 0) {
            status('No results found.');
            return;
        }

        data.results.forEach(function(item) {
            var div = document.createElement('div');
            div.className = 'search-result-item';
            div.innerHTML =
                '<div><span class="label">' + escapeHtml(item.label) + '</span>' +
                '<span class="qid">' + escapeHtml(item.id) + '</span></div>' +
                '<div class="desc">' + escapeHtml(item.description) + '</div>';
            div.addEventListener('click', function() {
                resultsDiv.classList.remove('visible');
                startTraversal(item.id, item.label);
            });
            resultsDiv.appendChild(div);
        });

        resultsDiv.classList.add('visible');
        status('Found ' + data.results.length + ' results.');
    } catch (err) {
        status('Search failed: ' + err.message);
    }
}

// Close search results when clicking elsewhere
document.addEventListener('click', function(e) {
    var resultsDiv = document.getElementById('search-results');
    if (!e.target.closest('#search-bar') && !e.target.closest('#search-results')) {
        resultsDiv.classList.remove('visible');
    }
});

// --- Traversal ---

async function startTraversal(qid, label) {
    status('Loading graph for ' + label + ' (' + qid + ')...');

    // Reset state
    selectedNodes.clear();
    expandedNodes.clear();
    updateTriplesSidebar();
    document.getElementById('generate-output').innerHTML = '';

    try {
        var resp = await fetch('/api/traverse', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ qid: qid, label: label }),
        });
        var data = await resp.json();

        if (data.error) {
            status('Error: ' + data.error);
            return;
        }

        // Init Cytoscape if needed
        if (!cy) initCytoscape();
        else {
            cy.elements().remove();
            document.getElementById('graph-hint').style.display = 'none';
        }

        cy.add(data.nodes);
        cy.add(data.edges);
        expandedNodes.add(qid);

        runLayout();
        status('Graph loaded: ' + data.nodes.length + ' nodes, ' + data.edges.length + ' edges. Click a node to expand, Shift+click to select.');
    } catch (err) {
        status('Traversal failed: ' + err.message);
    }
}

// --- Expand Node ---

async function expandNode(qid) {
    if (expandedNodes.has(qid)) {
        status(qid + ' already expanded.');
        return;
    }

    var nodeEl = cy.getElementById(qid);
    var label = nodeEl.data('label') || qid;
    status('Expanding ' + label + '...');
    expandedNodes.add(qid);

    try {
        var resp = await fetch('/api/expand', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ qid: qid }),
        });
        var data = await resp.json();

        if (data.error) {
            status('Error: ' + data.error);
            expandedNodes.delete(qid);
            return;
        }

        var addedCount = 0;

        // Assign depth based on parent node
        var parentDepth = nodeEl.data('depth') || 0;

        // Add new nodes (skip if already in graph)
        data.nodes.forEach(function(n) {
            if (cy.getElementById(n.data.id).length === 0) {
                n.data.depth = parentDepth + 1;
                cy.add(n);
                addedCount++;
            }
        });

        // Add new edges (skip duplicates)
        data.edges.forEach(function(e) {
            var edgeId = e.data.source + '-' + e.data.property + '-' + e.data.target;
            if (cy.getElementById(edgeId).length === 0) {
                e.data.id = edgeId;
                // Only add if both endpoints exist
                if (cy.getElementById(e.data.source).length > 0 &&
                    cy.getElementById(e.data.target).length > 0) {
                    cy.add(e);
                }
            }
        });

        runLayout();
        status('Expanded ' + label + ': +' + addedCount + ' nodes.');
    } catch (err) {
        status('Expand failed: ' + err.message);
        expandedNodes.delete(qid);
    }
}

// --- Sidebar: Triples ---

function updateTriplesSidebar() {
    var listDiv = document.getElementById('triples-list');
    var genBtn = document.getElementById('generate-btn');

    if (selectedNodes.size === 0) {
        listDiv.innerHTML = '<p class="hint">Shift+click nodes to select them. Their triples will appear here.</p>';
        genBtn.disabled = true;
        return;
    }

    if (!cy) return;

    var triples = gatherTriples();

    if (triples.length === 0) {
        listDiv.innerHTML = '<p class="hint">No triples found for selected nodes.</p>';
        genBtn.disabled = true;
        return;
    }

    var html = '';
    triples.forEach(function(t) {
        html += '<div class="triple-item">' +
            '<span class="subject">' + escapeHtml(t.subject) + '</span>' +
            ' &mdash; <span class="predicate">' + escapeHtml(t.predicate) + '</span>' +
            ' &mdash; <span class="object">' + escapeHtml(t.object) + '</span>' +
            '</div>';
    });
    listDiv.innerHTML = html;
    genBtn.disabled = false;
}

function gatherTriples() {
    var triples = [];
    var seen = new Set();

    cy.edges().forEach(function(edge) {
        var src = edge.data('source');
        var tgt = edge.data('target');

        if (selectedNodes.has(src) || selectedNodes.has(tgt)) {
            var srcNode = cy.getElementById(src);
            var tgtNode = cy.getElementById(tgt);
            var key = src + '|' + edge.data('label') + '|' + tgt;

            if (!seen.has(key)) {
                seen.add(key);
                triples.push({
                    subject: srcNode.data('label') || src,
                    predicate: edge.data('label'),
                    object: tgtNode.data('label') || tgt,
                });
            }
        }
    });

    return triples;
}

// --- Question Generation ---

document.getElementById('generate-btn').addEventListener('click', doGenerate);

async function doGenerate() {
    var triples = gatherTriples();
    if (triples.length === 0) return;

    var outputDiv = document.getElementById('generate-output');
    outputDiv.innerHTML = '<div class="loading">Generating questions... (this may take a moment)</div>';
    status('Sending triples to Ollama...');

    try {
        var resp = await fetch('/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ triples: triples }),
        });
        var data = await resp.json();

        if (data.error) {
            outputDiv.innerHTML = '<div class="error">' + escapeHtml(data.error) + '</div>';
            status('Generation failed.');
            return;
        }

        outputDiv.innerHTML = renderQuestions(data.response);
        status('Questions generated.');
    } catch (err) {
        outputDiv.innerHTML = '<div class="error">Request failed: ' + escapeHtml(err.message) + '</div>';
        status('Generation failed.');
    }
}

// --- Render Questions ---

function renderQuestions(text) {
    var lines = text.split('\n').filter(function(l) { return l.trim(); });
    var tagPattern = /^\[(RECALL|CONNECT|INFER)\]\s*\d*\.?\s*(.*)/;
    var html = '';
    var parsed = false;

    lines.forEach(function(line) {
        var m = line.match(tagPattern);
        if (m) {
            parsed = true;
            var tag = m[1];
            var question = m[2];
            html += '<div class="question-item">' +
                '<span class="q-tag q-tag-' + tag.toLowerCase() + '">' + tag + '</span> ' +
                escapeHtml(question) +
                '</div>';
        }
    });

    // Fallback: if model didn't follow format, render raw text
    if (!parsed) {
        html = '<div class="question-raw">' +
            escapeHtml(text).replace(/\n/g, '<br>') +
            '</div>';
    }

    return html;
}

// --- Utility ---

function escapeHtml(str) {
    if (!str) return '';
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}
