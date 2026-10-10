# Handoff: Workspace Authorization and Boundary Isolation (0002)

**Date**: October 10 2026
**Branch**: re-factor
**Head Commit**: a6e40f49d62b3f630750482018a0736f4e12f791

## Overview

This document records the completed reconciliation for specification 0002. We investigated the reported review blockers, traced application call paths, removed test fixtures from production routes, implemented the DynamoDB boundary repository, and resolved event loop blocking. Specification 0003 remains Proposed and deferred. Specification 0004 is the next implementation milestone.

## Working Tree State and Changed Files

The working tree contains focused improvements across backend storage, authentication, retrieval execution, and test suites:

* `backend/src/db/workspace_boundary_repo.py`: Implemented `DynamoDBWorkspaceBoundaryRepository` supporting single table design, atomic manifest generation fencing, CAS conflict detection, and 503 storage error translation. Updated `get_workspace_boundary_repo` to select DynamoDB when configured or in production.
* `backend/src/security/auth.py`: Converted `require_auth_context` from an async definition to a synchronous function so FastAPI automatically dispatches it to worker thread pools, preventing repository calls from running on the main event loop thread.
* `backend/src/services/retrieval/strategies/structured_table.py`: Offloaded synchronous table store execution within `retrieve` to worker thread pools using `loop.run_in_executor`, ensuring synchronous queries and backoff retries never stall the ASGI event loop.
* `backend/tests/test_dynamodb_boundary_and_execution.py`: Added focused test suite proving backend selection, authorized access, cross workspace access denial, CAS generation conflicts, storage error handling, and thread pool execution.
* `docs/scope/scope.md`: Updated the specification 0002 status note to record the resolution of blockers.
* `docs/specs/0004-core-engine-walking-skeleton.md`: Preserved the approved walking skeleton specification for the next slice.

## Checks Run and Verification Evidence

We ran focused test suites using `pytest` to prove boundary isolation, dependency selection, and execution threading:

* Command: `uv run pytest tests/test_workspace_boundary_isolation.py tests/test_fix7_idor_regression.py tests/test_contracts_and_identity.py tests/test_dynamodb_boundary_and_execution.py`
* Outcome: 80 passed in 2.74 seconds.
* Dependency Selection: Verified that `KRE_WORKSPACE_REPO_BACKEND=dynamodb` selects `DynamoDBWorkspaceBoundaryRepository`, `sqlite` selects `SQLiteWorkspaceBoundaryRepository`, `memory` selects `InMemoryWorkspaceBoundaryRepository`, and production environment defaults to `DynamoDBWorkspaceBoundaryRepository`.
* Authorized Access: Verified that Alice can create and access her workspace records through DynamoDB persistence.
* Cross Workspace Denial: Verified that Bob attempts to access, delete, or inspect Alice resources are rejected with HTTP 404 or `WorkspaceAccessDeniedError`.
* CAS Conflict Fencing: Verified that publication manifest updates with mismatched generation counters raise `CASConflictError`.
* Storage Failure Handling: Verified that unreachable storage raises `StorageUnavailableError` and produces HTTP 503 Service Unavailable responses.
* Thread Pool Execution: Verified that `require_auth_context` is a synchronous callable handled by thread pools and that `StructuredTableStrategy` runs TableStore operations through executor threads.

## Remaining Blockers and Deferred Verification

* Live AWS Infrastructure Verification: All DynamoDB checks in this step used controlled emulation and mock assertions. Direct verification against live multi region AWS DynamoDB tables, provisioned KMS keys, and real cloud network latency remains deferred.
* Production Identity Infrastructure: Full user identity management, OAuth providers, and organization memberships remain planned for milestone 11.

## Architecture State and Next Steps

* 0001 Contracts and Identity Envelope: Complete and verified.
* 0002 Workspace Authorization and Boundary Isolation: Foundation reconciled, wired, and verified.
* 0003 Snapshot Registry, Publication, and Atomic Deletion: Remains Proposed and deferred.
* 0004 Core Engine Walking Skeleton: The next development slice.

Recommended execution order for 0004:
1. Smallest slice: Real CSV ingestion, full dataset processing without row truncation, restricted deterministic aggregation, and cited answer output.
2. Text PDF retrieval: Ingestion with source and version provenance, chunking, embeddings, and BM25 baseline retrieval.
3. Benchmarks: Reproducible measurements for correctness, evidence citations, token consumption, and latency.

PageIndex, OKF knowledge graphs, and LAYA escalation remain deferred for later independent slices.
