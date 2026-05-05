#!/usr/bin/env bash
set -e

echo "=== Arb Bot Setup ==="

# Check Rust
if ! command -v cargo &>/dev/null; then
  echo "ERROR: Rust not found. Install: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
  exit 1
fi
echo "✓ Rust $(cargo --version)"

# Check Python 3.11+
PYTHON=$(command -v python3.11 || command -v python3 || true)
if [ -z "$PYTHON" ]; then
  echo "ERROR: Python 3.11+ not found. Install: brew install python@3.11"
  exit 1
fi
PY_VERSION=$($PYTHON --version 2>&1 | awk '{print $2}')
echo "✓ Python $PY_VERSION"

# Check uv
if ! command -v uv &>/dev/null; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.cargo/bin:$PATH"
fi
echo "✓ uv $(uv --version)"

# Python dependencies
echo ""
echo "Installing Python dependencies..."
cd python-core
uv sync --extra dev
cd ..
echo "✓ Python dependencies installed"

# Build Rust
echo ""
echo "Building Rust core (release)..."
cd rust-core
cargo build --release
cd ..
echo "✓ Rust binary: rust-core/target/release/arb"

# Copy .env if not exists
if [ ! -f ".env" ]; then
  cp config/.env.example .env
  echo "✓ Created .env from .env.example — add your API keys before going live"
else
  echo "✓ .env already exists"
fi

# Create data dir
mkdir -p data
echo "✓ data/ directory ready"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your API keys"
echo "  2. Seed market pairs: python scripts/backfill_matches.py"
echo "  3. Start the bot: python python-core/main.py"
echo ""
echo "The bot starts in DRY_RUN=true mode — no real money until you change the flag."
