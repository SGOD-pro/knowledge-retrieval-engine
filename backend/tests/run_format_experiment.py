import json
import time
import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir / "src"))

from services.llm.llm_service import call

kv_context = """Document: survay.csv
Year: 2016. Variable_code: H13. Variable_name: Redundancy and severance. Variable_category: Financial performance. Unit: DOLLARS(millions). Value: 455.
Year: 2016. Variable_code: H14. Variable_name: Salaries and wages. Variable_category: Financial performance. Unit: DOLLARS(millions). Value: 160833.
"""

table_context = """Document: survay.csv
| Year | Variable_code | Variable_name | Variable_category | Unit | Value |
| 2016 | H13 | Redundancy and severance | Financial performance | DOLLARS(millions) | 455 |
| 2016 | H14 | Salaries and wages | Financial performance | DOLLARS(millions) | 160833 |
"""

query = "In survay.csv, what is the recorded value for Redundancy and severance in 2016?"

t0 = time.perf_counter()
res_kv = call(query, kv_context)
lat_kv = (time.perf_counter() - t0) * 1000

t1 = time.perf_counter()
res_tbl = call(query, table_context)
lat_tbl = (time.perf_counter() - t1) * 1000

ans_kv = res_kv.get("answer")
usage_kv = res_kv.get("usage")
ans_tbl = res_tbl.get("answer")
usage_tbl = res_tbl.get("usage")

print(f"Key-Value Format Answer: {ans_kv} (Latency: {lat_kv:.1f}ms, Usage: {usage_kv})")
print(f"Markdown Table Format Answer: {ans_tbl} (Latency: {lat_tbl:.1f}ms, Usage: {usage_tbl})")
