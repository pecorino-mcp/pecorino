#!/usr/bin/env bash
set -e

# Always run from the project root
cd "$(dirname "$0")/.."

echo "0. Checking system prerequisites..."
for cmd in cmake ninja ccache c++; do
    if ! command -v $cmd &> /dev/null; then
        echo "Error: Required command '$cmd' not found in PATH."
        echo "Please install the system prerequisites:"
        echo "  Fedora/RHEL: sudo dnf install cmake ninja-build gcc gcc-c++ ccache python3-devel"
        echo "  Ubuntu/Debian: sudo apt install cmake ninja-build build-essential ccache python3-dev"
        echo "  macOS: brew install cmake ninja ccache"
        exit 1
    fi
done

if ! python3 -c "import sysconfig, os; exit(0 if os.path.exists(os.path.join(sysconfig.get_path('include'), 'Python.h')) else 1)" &> /dev/null; then
    echo "Error: Python development headers (Python.h) not found."
    echo "Please install the python development package for your system:"
    echo "  Fedora/RHEL: sudo dnf install python3-devel"
    echo "  Ubuntu/Debian: sudo apt install python3-dev"
    exit 1
fi

echo "1. Initializing git submodules..."
git submodule update --init --recursive

echo "2. Removing broken .venv and initializing a clean one..."
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate

echo "3. Installing requirements..."
pip install -v -r requirements.txt

echo "4. Compiling gorgonzola Python package using cmake (forced lite configuration)..."
# We retain the build directory to leverage Ninja and ccache for fast incremental builds
cmake -B modules/gorgonzola/build/release -G Ninja -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache -DENABLE_PCH=ON -DCMAKE_BUILD_TYPE=Release -DBUILD_PYTHON=TRUE -DBUILD_SHELL=FALSE -DGORGONZOLA_LITE=ON -DGORGONZOLA_LITE_ENABLE_GDS=ON -DGORGONZOLA_LITE_ENABLE_EXTENSIONS=ON modules/gorgonzola
cmake --build modules/gorgonzola/build/release --config Release

# Symlink compile_commands.json for IDE support
if [ -f "modules/gorgonzola/build/release/compile_commands.json" ]; then
    ln -sf "$(pwd)/modules/gorgonzola/build/release/compile_commands.json" modules/gorgonzola/modules/api-langs/python_api/compile_commands.json
    ln -sf "$(pwd)/modules/gorgonzola/build/release/compile_commands.json" compile_commands.json
fi
COMPILED_SO=$(find modules/gorgonzola/modules/api-langs/python_api -name "_gorgonzola*.so" -print -quit)
if [ -z "$COMPILED_SO" ]; then
    echo "Error: Failed to find the compiled gorgonzola shared object (.so)."
    exit 1
fi

echo "5. Copying gorgonzola Python module to site-packages..."
# Get the active python version to determine the site-packages path
PYTHON_VERSION=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
TARGET_DIR=".venv/lib/python${PYTHON_VERSION}/site-packages/gorgonzola"

mkdir -p "$TARGET_DIR"
cp "$COMPILED_SO" "$TARGET_DIR/"

# Copy the python scripts from src_py
cp -r modules/gorgonzola/modules/api-langs/python_api/src_py/* "$TARGET_DIR/"

echo "6. Registering pecorino-mcp..."
pip install -e .

echo "Environment setup complete!"
echo "Run '.venv/bin/pytest tests/' to verify tests pass."
echo "Run '.venv/bin/pecorino-mcp --help' to verify the CLI tool works."
