#!/usr/bin/env bash
# Bootstrap TS2 Redux Linux Edition — clone or refresh, then run ts2redux install.
set -euo pipefail

REPO="${TS2REDUX_REPO:-https://github.com/DataKnifeAI/TS2Redux.git}"
DIR="${TS2REDUX_SRC:-$HOME/.local/share/ts2redux/src}"
BIN_DIR="${TS2REDUX_BIN:-$HOME/.local/bin}"
BRANCH="${TS2REDUX_BRANCH:-linux-edition}"

if [[ -d "$DIR/.git" ]]; then
  git -C "$DIR" fetch origin "$BRANCH"
  git -C "$DIR" checkout "$BRANCH"
  git -C "$DIR" pull --ff-only origin "$BRANCH"
else
  mkdir -p "$(dirname "$DIR")"
  git clone --branch "$BRANCH" "$REPO" "$DIR"
fi

mkdir -p "$BIN_DIR"
ln -sfn "$DIR/bin/ts2redux" "$BIN_DIR/ts2redux"

exec "$DIR/bin/ts2redux" install "$@"
