import requests
import networkx as nx
import matplotlib.pyplot as plt
import sys
import os
import yaml

# --- CONFIGURATION ---
USER_AGENT = "Comp395_Student_Bot/1.0 (joel.walsh@example.edu)"  # CHANGE THIS!
HEADERS = {"User-Agent": USER_AGENT}
LIMIT_RELATIONS = 20  # Limit to avoid 'hairball' graphs for popular items like 'Earth'
LIMIT_RELATIONS_DEEP = 5  # Tighter limit for deeper traversal levels

def load_config():
    """
    Loads config.yaml from the same directory as this script.
    Missing keys use built-in defaults. No file → all defaults (REST mode).
    """
    defaults = {
        "term": None,
        "depth": None,
        "mode": "rest",
        "limit_relations": 20,
        "limit_relations_deep": 5,
        "user_agent": "Comp395_Student_Bot/1.0 (joel.walsh@example.edu)",
        "sparql_endpoint": "https://query.wikidata.org/sparql",
        "sparql_timeout": 55,
        "max_entity_sitelinks": 0,
        "ollama_endpoint": "http://localhost:11434",
        "ollama_model": "qwen3:8b",
        "expand_limit": 50,
    }

    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"
    )

    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                user_config = yaml.safe_load(f) or {}
            for key, value in user_config.items():
                if key in defaults:
                    defaults[key] = value
                else:
                    print(f"Warning: Unknown config key '{key}' — ignored.")
            print(f"[CONFIG] Loaded config.yaml (mode={defaults['mode']})")
        except Exception as e:
            print(f"Warning: Could not parse config.yaml: {e}")
    else:
        print("[CONFIG] No config.yaml found — using defaults (interactive, REST mode)")

    return defaults


def search_entity(term):
    """
    Step 1: The 'Retrieval Cue'.
    Uses the Action API to find QIDs matching a search string.
    """
    url = "https://www.wikidata.org/w/api.php"
    params = {
        "action": "wbsearchentities",
        "search": term,
        "language": "en",
        "format": "json"
    }
    
    try:
        response = requests.get(url, headers=HEADERS, params=params)
        data = response.json()
        return data.get("search", [])
    except Exception as e:
        print(f"Error searching: {e}")
        return []

def get_entity_rest(qid):
    """
    Step 2: The 'Schema Lookup'.
    Uses the REST API (cleaner JSON) to get the specific item's data.
    """
    url = f"https://www.wikidata.org/w/rest.php/wikibase/v1/entities/items/{qid}"
    try:
        response = requests.get(url, headers=HEADERS)
        return response.json()
    except Exception as e:
        print(f"Error retrieving REST data: {e}")
        return None

def resolve_labels(qids):
    """
    Helper: Batch resolves QIDs to human-readable labels using Action API.
    Essential for making the graph readable (Cognitive Load management).
    Handles more than 50 QIDs by batching automatically.
    """
    if not qids: return {}

    url = "https://www.wikidata.org/w/api.php"
    mapping = {}
    qid_list = list(qids)

    # Action API allows up to 50 IDs per request — loop in batches
    for i in range(0, len(qid_list), 50):
        batch = qid_list[i:i+50]
        params = {
            "action": "wbgetentities",
            "ids": "|".join(batch),
            "props": "labels",
            "languages": "en",
            "format": "json"
        }
        try:
            data = requests.get(url, headers=HEADERS, params=params).json()
            entities = data.get("entities", {})
            for qid, info in entities.items():
                label = info.get("labels", {}).get("en", {}).get("value", qid)
                mapping[qid] = label
        except Exception as e:
            print(f"Warning: Could not resolve labels for batch: {e}")

    return mapping

def parse_entity_relations(data, limit):
    """
    Extracts (property_id, target_qid) pairs from REST API entity data.
    Returns (raw_relations, ids_to_resolve).
    """
    ids_to_resolve = set()
    raw_relations = []

    statements = data.get('statements', {})

    count = 0
    for prop_id, claim_group in statements.items():
        if count >= limit:
            break

        claim = claim_group[0]
        data_value = claim.get('value', {}).get('content', {})

        target_id = None
        if isinstance(data_value, str) and data_value.startswith('Q'):
            target_id = data_value
        elif isinstance(data_value, dict):
            target_id = data_value.get('id')

        if target_id:
            raw_relations.append((prop_id, target_id))
            ids_to_resolve.add(prop_id)
            ids_to_resolve.add(target_id)
            count += 1

    return raw_relations, ids_to_resolve


def sparql_query(query, config):
    """
    Sends a SPARQL query to the Wikidata Query Service.
    Returns parsed JSON results or None on failure.
    """
    params = {"query": query, "format": "json"}
    headers = {
        "User-Agent": config["user_agent"],
        "Accept": "application/sparql-results+json",
    }
    try:
        response = requests.get(
            config["sparql_endpoint"],
            headers=headers,
            params=params,
            timeout=config["sparql_timeout"],
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        print(f"  [SPARQL] Query timed out after {config['sparql_timeout']}s. "
              "Try reducing depth or limits.")
        return None
    except Exception as e:
        print(f"  [SPARQL] Query failed: {e}")
        return None


def sparql_fetch_level(source_qids, limit, config):
    """
    Fetches all item-valued properties for a batch of entities in one SPARQL query.
    Returns (edges, label_map, new_target_qids, sitelinks_map).
    """
    if not source_qids:
        return [], {}, set(), {}

    values = " ".join(f"wd:{qid}" for qid in source_qids)

    # Generous LIMIT so prolific entities (countries, etc.) can't starve
    # smaller ones under SPARQL's arbitrary row ordering.
    # Client-side per_source_count enforces the real per-entity cap.
    total_limit = 100 * len(source_qids)

    query = f"""
SELECT ?source ?prop ?target ?sourceLabel ?propLabel ?targetLabel ?targetSitelinks
WHERE {{
  VALUES ?source {{ {values} }}
  ?source ?wdt ?target .
  ?prop wikibase:directClaim ?wdt .
  OPTIONAL {{ ?target wikibase:sitelinks ?targetSitelinks . }}
  FILTER(ISIRI(?target))
  FILTER(STRSTARTS(STR(?target), STR(wd:)))
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT {total_limit}
"""

    result = sparql_query(query, config)
    if not result:
        return [], {}, set(), {}

    edges = []
    label_map = {}
    new_targets = set()
    sitelinks_map = {}
    per_source_count = {}

    for binding in result.get("results", {}).get("bindings", []):
        source_uri = binding["source"]["value"]
        prop_uri = binding["prop"]["value"]
        target_uri = binding["target"]["value"]

        source_qid = source_uri.rsplit("/", 1)[-1]
        prop_id = prop_uri.rsplit("/", 1)[-1]
        target_qid = target_uri.rsplit("/", 1)[-1]

        # Enforce per-source limit client-side
        per_source_count.setdefault(source_qid, 0)
        if per_source_count[source_qid] >= limit:
            continue
        per_source_count[source_qid] += 1

        edges.append((source_qid, prop_id, target_qid))
        new_targets.add(target_qid)

        # Collect sitelinks count
        try:
            sitelinks_map[target_qid] = int(binding.get("targetSitelinks", {}).get("value", 0))
        except (ValueError, TypeError):
            sitelinks_map[target_qid] = 0

        # Collect labels from SERVICE wikibase:label
        source_label = binding.get("sourceLabel", {}).get("value", source_qid)
        prop_label = binding.get("propLabel", {}).get("value", prop_id)
        target_label = binding.get("targetLabel", {}).get("value", target_qid)

        label_map[source_qid] = source_label
        label_map[prop_id] = prop_label
        label_map[target_qid] = target_label

    return edges, label_map, new_targets, sitelinks_map


def traverse_sparql(start_qid, start_label, max_depth, config):
    """
    Pure SPARQL BFS — one sparql_fetch_level() call per depth level.
    Returns (edges, all_ids, depth_map, label_map).
    """
    hub_threshold = config["max_entity_sitelinks"]

    edges = []
    all_ids = {start_qid}
    depth_map = {start_qid: 0}
    label_map = {start_qid: start_label}
    visited = {start_qid}
    frontier = {start_qid}

    for depth in range(max_depth):
        if not frontier:
            break

        limit = config["limit_relations"] if depth == 0 else config["limit_relations_deep"]
        print(f"  [SPARQL] Fetching depth {depth} ({len(frontier)} entities)...")

        level_edges, level_labels, new_targets, sitelinks_map = sparql_fetch_level(
            frontier, limit, config
        )

        edges.extend(level_edges)
        label_map.update(level_labels)

        for _, prop_id, target_qid in level_edges:
            all_ids.add(prop_id)
            all_ids.add(target_qid)

        # Next frontier = new targets not yet visited, filtered by hub threshold
        next_frontier = set()
        for t in new_targets:
            if t not in visited:
                visited.add(t)
                depth_map[t] = depth + 1
                if hub_threshold > 0 and sitelinks_map.get(t, 0) >= hub_threshold:
                    print(f"  [HUB] Skipping expansion of {t} ({label_map.get(t, t)}) "
                          f"— {sitelinks_map[t]} sitelinks >= {hub_threshold}")
                else:
                    next_frontier.add(t)

        frontier = next_frontier

    return edges, all_ids, depth_map, label_map


def traverse_hybrid(start_qid, start_label, max_depth, config):
    """
    Hybrid REST+SPARQL traversal.
    Depth 0: REST (pedagogical, shows raw JSON structure).
    Depth 1+: SPARQL (efficient batch queries).
    Returns (edges, all_ids, depth_map, label_map).
    """
    hub_threshold = config["max_entity_sitelinks"]

    edges = []
    all_ids = {start_qid}
    depth_map = {start_qid: 0}
    label_map = {start_qid: start_label}
    visited = {start_qid}

    # --- Depth 0: REST (root entity — never filtered) ---
    print(f"  [REST] Fetching root entity {start_qid}...")
    data = get_entity_rest(start_qid)
    if not data:
        return edges, all_ids, depth_map, label_map

    raw_relations, ids_to_resolve = parse_entity_relations(data, config["limit_relations"])
    all_ids.update(ids_to_resolve)

    frontier = set()
    for prop_id, target_qid in raw_relations:
        edges.append((start_qid, prop_id, target_qid))
        if target_qid not in visited:
            visited.add(target_qid)
            depth_map[target_qid] = 1
            frontier.add(target_qid)

    # Resolve labels from REST depth (property IDs + target QIDs)
    print(f"  [REST] Resolving {len(ids_to_resolve)} labels from root...")
    rest_labels = resolve_labels(ids_to_resolve)
    label_map.update(rest_labels)

    # --- Depth 1+: SPARQL ---
    for depth in range(1, max_depth):
        if not frontier:
            break

        print(f"  [SPARQL] Fetching depth {depth} ({len(frontier)} entities)...")
        level_edges, level_labels, new_targets, sitelinks_map = sparql_fetch_level(
            frontier, config["limit_relations_deep"], config
        )

        edges.extend(level_edges)
        label_map.update(level_labels)

        for _, prop_id, target_qid in level_edges:
            all_ids.add(prop_id)
            all_ids.add(target_qid)

        next_frontier = set()
        for t in new_targets:
            if t not in visited:
                visited.add(t)
                depth_map[t] = depth + 1
                if hub_threshold > 0 and sitelinks_map.get(t, 0) >= hub_threshold:
                    print(f"  [HUB] Skipping expansion of {t} ({label_map.get(t, t)}) "
                          f"— {sitelinks_map[t]} sitelinks >= {hub_threshold}")
                else:
                    next_frontier.add(t)

        frontier = next_frontier

    return edges, all_ids, depth_map, label_map


def traverse(start_qid, start_label, max_depth, config):
    """
    BFS traversal of Wikidata graph up to max_depth levels.
    Returns (edges, all_ids, depth_map).
      edges: list of (source_qid, property_id, target_qid)
      all_ids: set of all QIDs and property IDs to resolve
      depth_map: dict mapping qid -> depth level
    """
    from collections import deque

    hub_threshold = config["max_entity_sitelinks"]

    visited = set()
    depth_map = {start_qid: 0}
    edges = []
    all_ids = {start_qid}
    queue = deque([(start_qid, 0)])
    visited.add(start_qid)

    while queue:
        current_qid, current_depth = queue.popleft()

        limit = LIMIT_RELATIONS if current_depth == 0 else LIMIT_RELATIONS_DEEP

        print(f"  Fetching {current_qid} (depth {current_depth})...")
        data = get_entity_rest(current_qid)
        if not data:
            continue

        # Hub check: skip expansion for non-root entities above sitelinks threshold
        if hub_threshold > 0 and current_depth > 0:
            sitelink_count = len(data.get("sitelinks", {}))
            if sitelink_count >= hub_threshold:
                print(f"  [HUB] Skipping expansion of {current_qid} "
                      f"({sitelink_count} sitelinks >= {hub_threshold})")
                continue

        raw_relations, ids = parse_entity_relations(data, limit)
        all_ids.update(ids)

        for prop_id, target_id in raw_relations:
            edges.append((current_qid, prop_id, target_id))

            if target_id not in visited:
                visited.add(target_id)
                depth_map[target_id] = current_depth + 1

                # Only enqueue for further expansion if within depth limit
                if current_depth + 1 < max_depth:
                    queue.append((target_id, current_depth + 1))

    return edges, all_ids, depth_map


def export_triples(center_label, edges, label_map, max_depth):
    """
    Exports all triples to a .txt file, one triple per line with a blank line between them.
    Each triple is: subject — predicate — object
    """
    safe_name = center_label.replace(' ', '_')
    filename = f"{safe_name}_depth{max_depth}_triples.txt"

    with open(filename, "w") as f:
        for i, (source_qid, prop_id, target_qid) in enumerate(edges):
            source_label = label_map.get(source_qid, source_qid)
            prop_label = label_map.get(prop_id, prop_id)
            target_label = label_map.get(target_qid, target_qid)
            if i > 0:
                f.write("\n")
            f.write(f"{source_label} — {prop_label} — {target_label}\n")

    print(f"[SUCCESS] Triples exported to: {filename}")


def visualize_schema(center_label, edges, depth_map, label_map, max_depth):
    """
    Step 3: 'Externalizing the Mind'.
    Draws the node-link diagram with depth-based coloring.
    """
    DEPTH_COLORS = ['lightcoral', 'skyblue', 'lightgreen', 'plum']
    DEPTH_SIZES = [3000, 2000, 1500, 1200]

    G = nx.DiGraph()

    print(f"\nBuilding Graph for {center_label}...")

    for source_qid, prop_id, target_qid in edges:
        source_label = label_map.get(source_qid, source_qid)
        target_label = label_map.get(target_qid, target_qid)
        prop_label = label_map.get(prop_id, prop_id)
        G.add_edge(source_label, target_label, label=prop_label)

    # Adaptive layout based on graph size
    num_nodes = G.number_of_nodes()
    fig_size = max(12, min(24, num_nodes // 5 + 12))
    k_value = max(0.3, 0.8 - (num_nodes / 200))

    pos = nx.spring_layout(G, k=k_value, seed=42)

    plt.figure(figsize=(fig_size, fig_size))

    # Draw nodes by depth level
    for depth in range(max_depth + 1):
        # Find QIDs at this depth, map to labels
        nodes_at_depth = [
            label_map.get(qid, qid) for qid, d in depth_map.items()
            if d == depth and label_map.get(qid, qid) in G.nodes()
        ]
        if nodes_at_depth:
            color = DEPTH_COLORS[min(depth, len(DEPTH_COLORS) - 1)]
            size = DEPTH_SIZES[min(depth, len(DEPTH_SIZES) - 1)]
            nx.draw_networkx_nodes(
                G, pos, nodelist=nodes_at_depth,
                node_color=color, node_size=size, alpha=0.8
            )

    # Draw Labels
    nx.draw_networkx_labels(G, pos, font_size=10, font_family="sans-serif")

    # Draw Edges
    nx.draw_networkx_edges(G, pos, width=1.0, alpha=0.5, arrows=True)

    # Draw Edge Labels (the Predicates)
    edge_labels = nx.get_edge_attributes(G, 'label')
    nx.draw_networkx_edge_labels(G, pos, edge_labels=edge_labels, font_size=8)

    depth_str = f" (depth={max_depth})" if max_depth > 1 else ""
    plt.title(f"Cognitive Schema: {center_label}{depth_str}", fontsize=15)
    plt.axis('off')

    safe_name = center_label.replace(' ', '_')
    filename = f"{safe_name}_depth{max_depth}_schema.png"
    plt.savefig(filename)
    print(f"\n[SUCCESS] Graph saved to: {filename}")
    plt.show()

def main():
    global USER_AGENT, HEADERS, LIMIT_RELATIONS, LIMIT_RELATIONS_DEEP

    # 1. LOAD CONFIG
    config = load_config()
    USER_AGENT = config["user_agent"]
    HEADERS = {"User-Agent": USER_AGENT}
    LIMIT_RELATIONS = config["limit_relations"]
    LIMIT_RELATIONS_DEEP = config["limit_relations_deep"]
    mode = config["mode"]

    hub_threshold = config["max_entity_sitelinks"]
    if hub_threshold > 0:
        print(f"[CONFIG] Hub filtering enabled: entities with >= {hub_threshold} sitelinks will not be expanded")
    else:
        print("[CONFIG] Hub filtering disabled (max_entity_sitelinks = 0)")

    if mode not in ("rest", "sparql", "hybrid"):
        print(f"Error: Unknown mode '{mode}'. Use 'rest', 'sparql', or 'hybrid'.")
        return

    # 2. INPUT — use config term or prompt interactively
    if config["term"]:
        term = config["term"]
        print(f"[CONFIG] Using search term: {term}")
    else:
        term = input("Enter a concept to map (e.g., 'Learning'): ")

    candidates = search_entity(term)

    if not candidates:
        print("No results found.")
        return

    # 3. VERIFICATION (The "Is right URL?" step) — always interactive
    print(f"\nFound {len(candidates)} candidates. Which one did you mean?")
    for i, item in enumerate(candidates):
        label = item.get('label', 'No Label')
        desc = item.get('description', 'No Description')
        qid = item['id']
        url = item['url']
        print(f"[{i+1}] {label} ({qid})")
        print(f"    Desc: {desc}")
        print(f"    URL:  {url}")
        print("-" * 40)

    selection = input(f"Select a number (1-{len(candidates)}): ")
    try:
        index = int(selection) - 1
        selected_item = candidates[index]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return

    selected_qid = selected_item['id']
    selected_label = selected_item['label']

    # 4. DEPTH SELECTION — use config depth or prompt interactively
    if config["depth"] is not None:
        max_depth = config["depth"]
        print(f"[CONFIG] Using depth: {max_depth}")
    else:
        depth_input = input("Enter traversal depth (1-3, default=1): ").strip()
        try:
            max_depth = int(depth_input)
        except ValueError:
            max_depth = 1
    max_depth = max(1, min(3, max_depth))  # Clamp to 1-3

    # 5. TRAVERSAL — dispatch by mode
    print(f"\nTraversing graph for {selected_label} ({selected_qid}), "
          f"depth={max_depth}, mode={mode}...")

    if mode == "rest":
        edges, all_ids, depth_map = traverse(selected_qid, selected_label, max_depth, config)

        if not edges:
            print("No relations found.")
            return

        print(f"Resolving labels for {len(all_ids)} IDs...")
        label_map = resolve_labels(all_ids)
        label_map[selected_qid] = selected_label

    elif mode == "sparql":
        edges, all_ids, depth_map, label_map = traverse_sparql(
            selected_qid, selected_label, max_depth, config
        )

        if not edges:
            print("No relations found.")
            return

    elif mode == "hybrid":
        edges, all_ids, depth_map, label_map = traverse_hybrid(
            selected_qid, selected_label, max_depth, config
        )

        if not edges:
            print("No relations found.")
            return

    # 6. OUTPUT
    export_triples(selected_label, edges, label_map, max_depth)
    visualize_schema(selected_label, edges, depth_map, label_map, max_depth)

if __name__ == "__main__":
    main()