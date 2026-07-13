#!/usr/bin/env bash
set -e

echo "1. Removing broken .venv and initializing a clean one..."
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate

echo "2. Installing requirements..."
pip install -r requirements.txt

echo "3. Compiling gorgonzola Python package using cmake (forced lite configuration)..."
cmake -B modules/gorgonzola/build/release -DCMAKE_BUILD_TYPE=Release -DBUILD_PYTHON=TRUE -DBUILD_SHELL=FALSE -DGORGONZOLA_LITE=ON -DGORGONZOLA_LITE_ENABLE_GDS=ON modules/gorgonzola
cmake --build modules/gorgonzola/build/release --config Release
COMPILED_SO=$(find modules/gorgonzola/modules/gorgonzola-api-langs/python_api/build -name "_gorgonzola*.so" -print -quit)

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
