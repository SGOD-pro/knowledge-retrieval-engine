#!/bin/bash
set -e
export ENVIRONMENT=dev
export AWS_PROFILE=aws
export PYTHONUNBUFFERED=1
.venv/bin/python src/kre/ingest_benchmark_docs.py
.venv/bin/python src/kre/run_benchmark.py > benchmark_out.txt
cat benchmark_out.txt
