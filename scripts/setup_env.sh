#!/usr/bin/env bash
set -e

echo "1. Removing broken .venv and initializing a clean one..."
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate

echo "2. Installing requirements..."
pip install -r requirements.txt

echo "3. Searching for a compiled gorgonzola Python package..."
# The .so file would normally be inside the build folder of the gorgonzola python API
COMPILED_SO=$(find modules/gorgonzola/modules/gorgonzola-api-langs/python_api/build -name "_gorgonzola*.so" -print -quit 2>/dev/null || true)

if [ -z "$COMPILED_SO" ]; then
    echo "Compiled package not found. Compiling in lite configuration..."
    make -C modules/gorgonzola python EXTENSION_LIST=""
    COMPILED_SO=$(find modules/gorgonzola/modules/gorgonzola-api-langs/python_api/build -name "_gorgonzola*.so" -print -quit)
else
    echo "Found pre-compiled package at $COMPILED_SO"
fi

echo "4. Copying gorgonzola Python module to site-packages..."
# Get the active python version to determine the site-packages path
PYTHON_VERSION=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
TARGET_DIR=".venv/lib/python${PYTHON_VERSION}/site-packages/gorgonzola"

mkdir -p "$TARGET_DIR"

if [ -n "$COMPILED_SO" ]; then
    cp "$COMPILED_SO" "$TARGET_DIR/"
fi

# Copy the python scripts from src_py
cp -r modules/gorgonzola/modules/gorgonzola-api-langs/python_api/src_py/* "$TARGET_DIR/"

echo "5. Registering pecorino-mcp..."
pip install -e .

echo "Environment setup complete!"
echo "Run '.venv/bin/pytest tests/' to verify tests pass."
echo "Run '.venv/bin/pecorino-mcp --help' to verify the CLI tool works."
