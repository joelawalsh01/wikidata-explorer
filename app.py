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

    # Forward edges: qid → targets
    fwd_edges, fwd_labels, fwd_targets, fwd_sitelinks = traverse.sparql_fetch_level(
        {qid}, limit, config
    )

    # Reverse edges: sources → qid
    rev_edges, rev_labels, rev_sources, rev_sitelinks = traverse.sparql_fetch_reverse(
        {qid}, limit, config
    )

    # Merge label maps
    label_map = {**fwd_labels, **rev_labels}

    nodes = []
    edges = []
    seen_nodes = set()

    # Forward: new nodes are targets
    for source_qid, prop_id, target_qid in fwd_edges:
        if target_qid not in seen_nodes:
            nodes.append({
                "data": {
                    "id": target_qid,
                    "label": label_map.get(target_qid, target_qid),
                    "qid": target_qid,
                    "depth": -1,
                    "sitelinks": fwd_sitelinks.get(target_qid, 0),
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

    # Reverse: new nodes are sources
    for source_qid, prop_id, target_qid in rev_edges:
        if source_qid not in seen_nodes:
            nodes.append({
                "data": {
                    "id": source_qid,
                    "label": label_map.get(source_qid, source_qid),
                    "qid": source_qid,
                    "depth": -1,
                    "sitelinks": rev_sitelinks.get(source_qid, 0),
                }
            })
            seen_nodes.add(source_qid)
        edges.append({
            "data": {
                "source": source_qid,
                "target": target_qid,
                "label": label_map.get(prop_id, prop_id),
                "property": prop_id,
            }
        })

    return jsonify({"nodes": nodes, "edges": edges})


@app.route("/api/models", methods=["GET"])
def api_models():
    """Return list of locally available Ollama models."""
    ollama_url = config.get("ollama_endpoint", "http://localhost:11434")
    try:
        resp = http_requests.get(f"{ollama_url}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        names = [m["name"] for m in data.get("models", [])]
        return jsonify({"models": names})
    except http_requests.exceptions.ConnectionError:
        return jsonify({
            "models": [],
            "error": "Could not connect to Ollama. Is it running?"
        })
    except Exception as e:
        return jsonify({"models": [], "error": str(e)})


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """Send triples to Ollama for question generation."""
    data = request.get_json()
    triples = data.get("triples", [])
    fmt = data.get("format", "open")
    graph_entities = data.get("graphEntities", [])
    if not triples:
        return jsonify({"error": "No triples provided"}), 400

    triples_text = "\n".join(
        f"{t['subject']} -- {t['predicate']} -- {t['object']}"
        for t in triples
    )

    if fmt == "mcq":
        entities_list = ", ".join(graph_entities) if graph_entities else ""
        system_prompt = f"""You generate multiple choice quiz questions from knowledge graph triples.
Each triple is: Subject -- Predicate -- Object.

Output exactly 5 multiple choice questions. Each question has 4 options (A, B, C, D).
Mark the correct answer by placing * after it.

Use these entities from the knowledge graph as plausible distractors (wrong answers) where appropriate:
{entities_list}

Format:

[RECALL] 1. Question about a fact directly stated in the triples
A) Wrong answer
B) Correct answer *
C) Wrong answer
D) Wrong answer

[RECALL] 2. Question about a fact directly stated in the triples
A) Wrong answer
B) Wrong answer
C) Correct answer *
D) Wrong answer

[CONNECT] 3. Question about how two entities relate
A) Correct answer *
B) Wrong answer
C) Wrong answer
D) Wrong answer

[CONNECT] 4. Question about how two entities relate
A) Wrong answer
B) Wrong answer
C) Wrong answer
D) Correct answer *

[INFER] 5. Question requiring reasoning beyond what is explicitly stated
A) Wrong answer
B) Correct answer *
C) Wrong answer
D) Wrong answer

Rules:
- Output ONLY the 5 questions with their options, nothing else
- Each question starts with a tag: [RECALL], [CONNECT], or [INFER]
- Each question has exactly 4 options: A), B), C), D)
- Mark the correct answer with * at the end of the line
- Vary which letter is correct across questions
- Use entities from the provided list as plausible wrong answers where possible
- No explanations, no preamble"""
    else:
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
    model = data.get("model") or config.get("ollama_model", "qwen3:8b")

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
