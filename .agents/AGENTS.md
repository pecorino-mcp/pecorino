# Pecorino Project Rules

- Always use `.venv/bin/python` and `.venv/bin/pytest` (not the system Python) when running Python code or tests in this project. The project depends on custom native packages (`gorgonzola`) and an editable install that are only available in the virtualenv.
