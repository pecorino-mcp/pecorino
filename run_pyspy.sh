#!/bin/bash
.venv/bin/pip install py-spy
.venv/bin/python -m pytest tests/test_federated_graph.py > test_out.log 2>&1 &
PID=$!
sleep 5
.venv/bin/py-spy dump --pid $PID
kill -9 $PID
