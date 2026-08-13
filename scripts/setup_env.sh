#!/usr/bin/env bash
set -e

# Always run from the project root
cd "$(dirname "$0")/.."

FORCE_REBUILD=0
if [ "$1" == "--force" ]; then
    FORCE_REBUILD=1
fi

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

echo "2. Initializing virtual environment..."
if [ -d ".venv" ] && [ $FORCE_REBUILD -eq 0 ]; then
    echo "Found existing .venv. Using it..."
else
    echo "Removing .venv and initializing a clean one..."
    rm -rf .venv
    python3 -m venv .venv
fi
source .venv/bin/activate

echo "3. Installing requirements..."
pip install -v -r requirements.txt

echo "4. Checking for existing gorgonzola Python package..."
# Get the active python version to determine the site-packages path
PYTHON_VERSION=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
TARGET_DIR=".venv/lib/python${PYTHON_VERSION}/site-packages/gorgonzola"

if [ -d "$TARGET_DIR" ] && [ -n "$(ls "$TARGET_DIR"/_gorgonzola*.so 2>/dev/null)" ] && [ $FORCE_REBUILD -eq 0 ]; then
    echo "Found compiled gorgonzola extension in site-packages. Skipping compilation."
else
    echo "Compiling gorgonzola Python package using cmake (forced lite configuration)..."
    # We retain the build directory to leverage Ninja and ccache for fast incremental builds
    cmake -B build -G Ninja -DCMAKE_UNITY_BUILD=OFF -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache -DENABLE_PCH=ON -DCMAKE_BUILD_TYPE=Release -DBUILD_PYTHON=TRUE -DBUILD_SHELL=FALSE -DGORGONZOLA_LITE=ON -DGORGONZOLA_LITE_ENABLE_GDS=ON -DGORGONZOLA_LITE_ENABLE_EXTENSIONS=ON modules/gorgonzola
    cmake --build build --config Release

    COMPILED_SO=$(ls modules/gorgonzola/modules/api-langs/python_api/build/gorgonzola/_gorgonzola*.so | head -n 1)
    if [ -z "$COMPILED_SO" ]; then
        echo "Error: Failed to find the compiled gorgonzola shared object (.so)."
        exit 1
    fi

    echo "5. Copying gorgonzola Python module to site-packages..."
    mkdir -p "$TARGET_DIR"
    cp "$COMPILED_SO" "$TARGET_DIR/"

    # Copy the python scripts from src_py
    cp -r modules/gorgonzola/modules/api-langs/python_api/src_py/* "$TARGET_DIR/"
fi

echo "6. Registering pecorino-mcp..."
pip install -e .

echo "Environment setup complete!"
echo "Run '.venv/bin/pytest tests/' to verify tests pass."
echo "Run '.venv/bin/pecorino-mcp --help' to verify the CLI tool works."
