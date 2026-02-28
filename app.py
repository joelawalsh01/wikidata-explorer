"""
Flask web UI for Wikidata Knowledge Graph Explorer.
Provides an interactive graph interface backed by traverse.py functions.
"""

from flask import Flask, render_template, request, jsonify
import re
import requests as http_requests

import traverse

app = Flask(__name__)

# --- Startup: load config and set traverse module globals ---
config = traverse.load_config()
traverse.USER_AGENT = config["user_agent"]
traverse.HEADERS = {"User-Agent": config["user_agent"]}
traverse.LIMIT_RELATIONS = config["limit_relations"]
traverse.LIMIT_RELATIONS_DEEP = config["limit_relations_deep"]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search", methods=["POST"])
def api_search():
    """Search Wikidata for entities matching a term."""
    data = request.get_json()
    term = data.get("term", "").strip()
    if not term:
        return jsonify({"error": "No search term provided"}), 400

    candidates = traverse.search_entity(term)
    results = []
    for item in candidates:
        results.append({
            "id": item.get("id"),
            "label": item.get("label", "No Label"),
            "description": item.get("description", ""),
        })
    return jsonify({"results": results})


@app.route("/api/traverse", methods=["POST"])
def api_traverse():
    """Initial depth-1 traversal for a selected entity."""
    data = request.get_json()
    qid = data.get("qid", "").strip()
    label = data.get("label", qid)
    if not qid:
        return jsonify({"error": "No QID provided"}), 400

    # Fetch root entity via REST API
    entity_data = traverse.get_entity_rest(qid)
    if not entity_data:
        return jsonify({"error": f"Could not retrieve entity {qid}"}), 500

    raw_relations, ids_to_resolve = traverse.parse_entity_relations(
        entity_data, config["limit_relations"]
    )
    ids_to_resolve.add(qid)
    label_map = traverse.resolve_labels(ids_to_resolve)
    label_map.setdefault(qid, label)

    # Get sitelinks count for root
    root_sitelinks = len(entity_data.get("sitelinks", {}))

    # Build Cytoscape-format nodes and edges
    nodes = [{
        "data": {
            "id": qid,
            "label": label_map.get(qid, label),
            "qid": qid,
            "depth": 0,
            "sitelinks": root_sitelinks,
        }
    }]
    edges = []
    seen_nodes = {qid}

    for prop_id, target_qid in raw_relations:
        if target_qid not in seen_nodes:
            nodes.append({
                "data": {
                    "id": target_qid,
                    "label": label_map.get(target_qid, target_qid),
                    "qid": target_qid,
                    "depth": 1,
                    "sitelinks": 0,
                }
            })
            seen_nodes.add(target_qid)
        edges.append({
            "data": {
                "source": qid,
                "target": target_qid,
                "label": label_map.get(prop_id, prop_id),
                "property": prop_id,
            }
        })

    return jsonify({"nodes": nodes, "edges": edges})


@app.route("/api/expand", methods=["POST"])
def api_expand():
    """Expand a single node using SPARQL."""
    data = request.get_json()
    qid = data.get("qid", "").strip()
    if not qid:
        return jsonify({"error": "No QID provided"}), 400

    limit = config.get("expand_limit", 15)
    level_edges, label_map, new_targets, sitelinks_map = traverse.sparql_fetch_level(
        {qid}, limit, config
    )

    nodes = []
    edges = []
    seen_nodes = set()

    for source_qid, prop_id, target_qid in level_edges:
        if target_qid not in seen_nodes:
            nodes.append({
                "data": {
                    "id": target_qid,
                    "label": label_map.get(target_qid, target_qid),
                    "qid": target_qid,
                    "depth": -1,  # frontend will assign real depth
                    "sitelinks": sitelinks_map.get(target_qid, 0),
                }
            })
            seen_nodes.add(target_qid)
        edges.append({
            "data": {
                "source": source_qid,
                "target": target_qid,
                "label": label_map.get(prop_id, prop_id),
                "property": prop_id,
            }
        })

    return jsonify({"nodes": nodes, "edges": edges})


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """Send triples to Ollama for question generation."""
    data = request.get_json()
    triples = data.get("triples", [])
    if not triples:
        return jsonify({"error": "No triples provided"}), 400

    triples_text = "\n".join(
        f"{t['subject']} -- {t['predicate']} -- {t['object']}"
        for t in triples
    )

    system_prompt = """You generate quiz questions from knowledge graph triples.
Each triple is: Subject -- Predicate -- Object.

Output exactly 5 questions using this format:

[RECALL] 1. Question about a fact directly stated in the triples
[RECALL] 2. Question about a fact directly stated in the triples
[CONNECT] 3. Question about how two entities relate to each other
[CONNECT] 4. Question about how two entities relate to each other
[INFER] 5. Question requiring reasoning beyond what is explicitly stated

Example input:
Marie Curie -- place of birth -- Warsaw
Marie Curie -- field of work -- physics
Marie Curie -- award received -- Nobel Prize in Physics

Example output:
[RECALL] 1. Where was Marie Curie born?
[RECALL] 2. What award did Marie Curie receive?
[CONNECT] 3. What is the connection between Marie Curie's field of work and the award she received?
[CONNECT] 4. How does Marie Curie's place of birth relate to her nationality?
[INFER] 5. Based on her receiving the Nobel Prize in Physics, what can you infer about the significance of her contributions to science?

Rules:
- Output ONLY the 5 numbered questions, nothing else
- Each line starts with a tag: [RECALL], [CONNECT], or [INFER]
- No answers, no explanations, no preamble"""

    user_prompt = f"Triples:\n{triples_text}"

    ollama_url = config.get("ollama_endpoint", "http://localhost:11434")
    model = config.get("ollama_model", "qwen3:8b")

    try:
        resp = http_requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": model,
                "system": system_prompt,
                "prompt": user_prompt,
                "stream": False,
            },
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json()
        response_text = result.get("response", "")
        # Strip <think>...</think> blocks from models like qwen3
        response_text = re.sub(
            r"<think>.*?</think>\s*", "", response_text, flags=re.DOTALL
        )
        return jsonify({"response": response_text.strip()})
    except http_requests.exceptions.ConnectionError:
        return jsonify({
            "error": "Could not connect to Ollama. Is it running? "
                     f"(Expected at {ollama_url})"
        }), 503
    except http_requests.exceptions.Timeout:
        return jsonify({"error": "Ollama request timed out."}), 504
    except Exception as e:
        return jsonify({"error": f"Ollama error: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
