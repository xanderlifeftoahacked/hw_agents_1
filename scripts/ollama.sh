#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/.ollama"
VERSION="${OLLAMA_VERSION:-v0.40.4}"
MODEL="${OPENAI_MODEL:-qwen3:4b}"

export OLLAMA_MODELS="$DIR/models"
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-16384}"
export HOME="$DIR/home"

install() {
    mkdir -p "$DIR" "$OLLAMA_MODELS" "$HOME"
    if [ -x "$DIR/bin/ollama" ]; then
        echo "ollama already installed: $DIR/bin/ollama"
        return
    fi
    local url="https://github.com/ollama/ollama/releases/download/$VERSION/ollama-linux-amd64.tar.zst"
    curl -fL -C - -o "$DIR/ollama.tar.zst" "$url"
    tar --zstd -xf "$DIR/ollama.tar.zst" -C "$DIR"
    rm "$DIR/ollama.tar.zst"
    echo "installed: $DIR/bin/ollama"
}

serve() {
    exec "$DIR/bin/ollama" serve
}

pull() {
    "$DIR/bin/ollama" pull "$MODEL"
}

case "${1:-}" in
    install) install ;;
    serve) serve ;;
    pull) pull ;;
    *) echo "usage: $0 install|serve|pull" >&2; exit 2 ;;
esac
