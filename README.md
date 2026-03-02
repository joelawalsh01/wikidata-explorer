# Wikidata Traverse REST

Interactive knowledge graph explorer for Wikidata. Search for any entity, traverse its relationships, and visualize the result as an interactive graph. Optionally generate quiz questions from the graph using a local LLM.

## Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com/) (optional, for quiz generation)

## Install uv

If you don't have `uv` installed:

```sh
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

After installing, restart your terminal or follow the instructions printed by the installer to add `uv` to your PATH.

## Setup & Run

```sh
# Clone the repo
git clone https://github.com/your-username/wikidata-traverse-rest.git
cd wikidata-traverse-rest

# Install dependencies and run the web UI
uv run python app.py
```

`uv run` automatically creates a virtual environment and installs dependencies from `pyproject.toml` on first use — no separate install step needed.

The app will be available at **http://localhost:5001**.

### CLI mode

To run the command-line traversal instead:

```sh
uv run python traverse.py
```

This uses settings from `config.yaml` (search term, depth, traversal mode) and outputs a graph image and triples file.

## Configuration

Edit `config.yaml` to change defaults:

| Key | Default | Description |
|-----|---------|-------------|
| `term` | `null` | Pre-set search term (skips interactive prompt in CLI) |
| `depth` | `null` | Traversal depth 1-3 (skips prompt in CLI) |
| `mode` | `rest` | `rest`, `sparql`, or `hybrid` |
| `limit_relations` | `20` | Max relations for the root entity |
| `limit_relations_deep` | `5` | Max relations per entity at deeper levels |
| `max_entity_sitelinks` | `0` | Hub filter threshold (0 = disabled) |
| `ollama_model` | `qwen3:8b` | Ollama model for quiz generation |
| `expand_limit` | `50` | Max edges when expanding a node in the web UI |
