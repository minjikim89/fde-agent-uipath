#!/usr/bin/env bash
# Assemble a self-contained UiPath Coded Agent package root in ./dist.
#
# WHY: `uipath pack` recurses the project ROOT's subdirectories but cannot reach
# UP into a parent directory (verified, uipath 2.10.75). The entry module
# coded_agent_wrapper.py imports the shared diagnosis core (core/ -> tools.py
# -> metrics.* + agents.aggregator) and reads data/*.yaml, all of which live in
# the PARENT scripts/ dir. So packing from scripts/uipath/ would ship an agent
# that ImportErrors at runtime. This script vendors the exact runtime closure
# into ./dist so `uipath init && uipath pack` from dist produce a complete .nupkg.
#
# The heavy RAG corpus (data/chroma, ~143MB) is intentionally excluded — the
# engine degrades to ontology-only (degraded=True) when it is absent. Mount it
# via an Orchestrator Storage Bucket for non-degraded runs.
#
# Usage (uipath CLI lives in scripts/.venv-uipath — system python 3.14 unsupported,
# so activate the 3.12 venv first; run from an EXTERNAL terminal because `uipath
# auth` opens a browser):
#   source scripts/.venv-uipath/bin/activate   # from repo root
#   cd scripts/uipath && ./build_coded_agent.sh && cd dist
#   uipath auth && uipath init && uipath pack --nolock && uipath publish --folder Shared
set -euo pipefail

UIPATH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # scripts/uipath
SCRIPTS_DIR="$(dirname "$UIPATH_DIR")"                        # scripts
DIST="$UIPATH_DIR/dist"

echo "[build] cleaning $DIST"
rm -rf "$DIST"
mkdir -p "$DIST"

echo "[build] vendoring entry + helpers"
cp "$UIPATH_DIR"/agent_entry.py \
   "$UIPATH_DIR"/coded_agent_wrapper.py \
   "$UIPATH_DIR"/uipath_client.py \
   "$UIPATH_DIR"/loop_b_symbolic.py \
   "$UIPATH_DIR"/pyproject.toml \
   "$UIPATH_DIR"/uipath.json \
   "$DIST"/

echo "[build] vendoring shared diagnosis core packages"
# core/tools.py imports metrics.* and agents.aggregator at module load; engine
# reads data/*.yaml + data/sample-workflows/*.md at runtime. parser/heatmap are
# part of the closure. All are pure-python + small.
cp -R "$SCRIPTS_DIR"/core \
      "$SCRIPTS_DIR"/agents \
      "$SCRIPTS_DIR"/metrics \
      "$SCRIPTS_DIR"/parser \
      "$SCRIPTS_DIR"/heatmap \
      "$DIST"/

echo "[build] vendoring data (light yaml/json/md only; heavy corpus excluded)"
rsync -a \
  --exclude 'chroma' \
  --exclude 'mongodump_full_snapshot' \
  --exclude '*.tar.bz2' \
  --exclude '*.pkl' \
  --exclude '*.xlsx' \
  --exclude '*.pdf' \
  "$SCRIPTS_DIR"/data/ "$DIST"/data/

echo "[build] pruning __pycache__"
find "$DIST" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "[build] done -> $DIST ($(du -sh "$DIST" | cut -f1))"
echo "[build] next (uipath CLI = scripts/.venv-uipath; activate it first):"
echo "[build]   cd '$DIST' && uipath auth && uipath init && uipath pack --nolock && uipath publish --folder Shared"
