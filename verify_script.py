import requests
import json
import sys

BASE_URL = "http://127.0.0.1:8001/api/v1"

results = []

def record(behavior: str, req_line: str, resp: requests.Response, passed: bool, notes: str = ""):
    entry = {
        "behavior": behavior,
        "request": req_line,
        "status": resp.status_code,
        "passed": passed,
        "body_excerpt": resp.text[:160],
        "notes": notes,
    }
    results.append(entry)
    print(f"[{'PASS' if passed else 'FAIL'}] {behavior} -> {resp.status_code} ({req_line})")

# Behavior 1: Reject unauthenticated workspace creation (AC-2)
r1 = requests.post(f"{BASE_URL}/workspaces", json={"name": "Attacker WS"})
record("Unauthenticated workspace creation rejected", "POST /api/v1/workspaces (no auth)", r1, r1.status_code == 401)

# Behavior 2: Reject invalid bearer token (AC-2)
r2 = requests.post(f"{BASE_URL}/workspaces", json={"name": "Attacker WS"}, headers={"Authorization": "Bearer bad_token"})
record("Invalid bearer token rejected", "POST /api/v1/workspaces (Bearer bad_token)", r2, r2.status_code == 401)

# Behavior 3: Authenticated caller (Alice) creates workspace (AC-1, AC-5)
r3 = requests.post(f"{BASE_URL}/workspaces", json={"name": "Alice Live WS"}, headers={"Authorization": "Bearer test_token_alice"})
record("Alice creates private workspace", "POST /api/v1/workspaces (Bearer test_token_alice)", r3, r3.status_code == 201)
alice_ws_id = r3.json().get("workspace_id") if r3.status_code == 201 else "ws_alpha"

# Behavior 4: Bob receives 404 attempting to access Alice workspace (AC-3, AC-15)
r4 = requests.get(f"{BASE_URL}/workspaces/{alice_ws_id}", headers={"Authorization": "Bearer test_token_bob"})
record("Cross workspace lookup masked with 404", f"GET /api/v1/workspaces/{alice_ws_id} (Bob auth)", r4, r4.status_code == 404)

# Behavior 5: Alice registers new source in authorized workspace
r5 = requests.post(f"{BASE_URL}/workspaces/{alice_ws_id}/sources", json={"title": "Q3 Revenue Report"}, headers={"Authorization": "Bearer test_token_alice"})
record("Alice registers source in workspace", f"POST /api/v1/workspaces/{alice_ws_id}/sources", r5, r5.status_code == 201)
alice_src_id = r5.json().get("source_id") if r5.status_code == 201 else "src_dummy"

# Behavior 6: Bob receives 404 attempting to access Alice source (AC-3)
r6 = requests.get(f"{BASE_URL}/workspaces/{alice_ws_id}/sources/{alice_src_id}", headers={"Authorization": "Bearer test_token_bob"})
record("Cross workspace source access masked with 404", f"GET /api/v1/workspaces/{alice_ws_id}/sources/{alice_src_id} (Bob auth)", r6, r6.status_code == 404)

# Behavior 7: Alice queries workspace alpha successfully (AC-1, AC-10)
r7 = requests.post(f"{BASE_URL}/workspaces/{alice_ws_id}/query", json={"query": "Summary of Q3 results"}, headers={"Authorization": "Bearer test_token_alice"})
record("Alice executes isolated query", f"POST /api/v1/workspaces/{alice_ws_id}/query", r7, r7.status_code == 200)

# Behavior 8: Alice deletes source, creating atomic tombstone and incrementing generation (AC-8, AC-9)
r8 = requests.delete(f"{BASE_URL}/workspaces/{alice_ws_id}/sources/{alice_src_id}", headers={"Authorization": "Bearer test_token_alice"})
record("Alice tombstones source atomically", f"DELETE /api/v1/workspaces/{alice_ws_id}/sources/{alice_src_id}", r8, r8.status_code == 200 and r8.json().get("status") == "tombstoned")

# Behavior 9: Direct access to deleted source immediately returns 404 (AC-8)
r9 = requests.get(f"{BASE_URL}/workspaces/{alice_ws_id}/sources/{alice_src_id}", headers={"Authorization": "Bearer test_token_alice"})
record("Tombstoned source returns 404 on direct lookup", f"GET /api/v1/workspaces/{alice_ws_id}/sources/{alice_src_id}", r9, r9.status_code == 404)

# Behavior 10: Reject empty or whitespace payload (400 Bad Request)
r10 = requests.post(f"{BASE_URL}/workspaces", json={"name": "   "}, headers={"Authorization": "Bearer test_token_alice"})
record("Whitespace workspace name rejected with 400", "POST /api/v1/workspaces (whitespace name)", r10, r10.status_code == 400)

all_passed = all(item["passed"] for item in results)
print(f"\nSummary: {sum(1 for r in results if r['passed'])} / {len(results)} behaviors verified successfully.")
if not all_passed:
    sys.exit(1)
