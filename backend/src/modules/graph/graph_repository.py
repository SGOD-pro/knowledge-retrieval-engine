import logging
from config import settings
from modules.graph.okf_key import canon_key

logger = logging.getLogger(__name__)


def _clean_id(raw_id: str) -> str:
    """Strip common partition key prefixes (ENTITY#, CHUNK#, PAGE#, HEADING#)."""
    for prefix in ("ENTITY#", "CHUNK#", "PAGE#", "HEADING#"):
        if raw_id.startswith(prefix):
            return raw_id[len(prefix):]
    return raw_id


class GraphRepository:
    """Domain repository for Open Knowledge Framework (OKF) graph entities and relations."""

    def __init__(self):
        from aws.infra import get_resource

        self.dynamodb = get_resource("dynamodb")
        self.okf_entities_table = self.dynamodb.Table("okf_entities")
        self.okf_properties_table = self.dynamodb.Table("okf_properties")
        self.okf_relations_table = self.dynamodb.Table("okf_relations")

    def get_workspace_graph(self, workspace_id: str | None = None) -> dict:
        nodes = []
        edges = []
        try:
            scan_resp = self.okf_entities_table.scan(Limit=50)
            items = scan_resp.get("Items", [])
            for item in items:
                raw_pk = item.get("PK", "")
                concept_id = item.get("concept_id") or _clean_id(raw_pk)
                label = item.get("name") or item.get("canonical_name") or concept_id
                c_type = item.get("concept_type", "Concept")
                doc_ids = item.get("document_ids", [])
                doc_count = (
                    len(doc_ids)
                    if isinstance(doc_ids, (list, set))
                    else int(item.get("doc_count", 1))
                )
                prop_count = int(item.get("property_count", 0))
                frequency = prop_count if prop_count > 0 else int(item.get("frequency", 1))
                nodes.append(
                    {
                        "id": concept_id,
                        "label": label,
                        "type": c_type,
                        "properties": {
                            "frequency": frequency,
                            "doc_count": doc_count,
                        },
                    }
                )

            rel_resp = self.okf_relations_table.scan(Limit=50)
            for item in rel_resp.get("Items", []):
                raw_from = item.get("PK", "")
                source = _clean_id(raw_from) if raw_from else item.get("from_concept_id", "")

                raw_to = item.get("to_id") or item.get("to_concept_id") or ""
                target = _clean_id(raw_to) if raw_to else ""

                rel_label = item.get("rel_type") or item.get("relation_type") or "RELATED_TO"

                # Weight: read "score" when present (SEMANTICALLY_RELATED edges), fallback to relation_weight or 1.0
                score_val = item.get("score")
                if score_val is not None:
                    weight = float(score_val)
                elif item.get("relation_weight") is not None:
                    weight = float(item["relation_weight"])
                else:
                    weight = 1.0

                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "label": rel_label,
                        "weight": weight,
                    }
                )
        except Exception as e:
            logger.info("Live DynamoDB graph scan fallback: %s", e)

        return {"nodes": nodes, "edges": edges}

    def get_okf_properties(self, entities: list[str]) -> list[dict]:
        if not entities:
            return []
        from modules.documents.documents_repository import _is_test_env

        if _is_test_env():
            return []

        from boto3.dynamodb.conditions import Key

        results = []
        for entity in entities:
            try:
                pk = f"ENTITY#{canon_key(entity)}"
                resp = self.okf_properties_table.query(
                    KeyConditionExpression=Key("PK").eq(pk)
                )
                results.extend(resp.get("Items", []))
            except Exception as e:
                logger.debug("OKF property lookup failed for %s: %s", entity, e)
        return results

    def expand_graph(
        self, start_entities: list[str], max_hops: int = 2
    ) -> list[dict[str, Any]]:
        """Multi-hop graph traversal over okf_relations table.
        Rule 13: MAX_NODES=40, MAX_HOPS=2 default.
        """
        if not start_entities:
            return []
        from modules.documents.documents_repository import _is_test_env

        if _is_test_env():
            return []

        from boto3.dynamodb.conditions import Key

        visited_nodes: set[str] = set()
        frontier = list(start_entities)
        results: list[dict[str, Any]] = []

        for hop in range(max_hops):
            next_frontier: list[str] = []
            for entity in frontier:
                cleaned = canon_key(entity)
                if cleaned in visited_nodes:
                    continue
                visited_nodes.add(cleaned)

                try:
                    resp = self.okf_relations_table.query(
                        KeyConditionExpression=Key("PK").eq(f"ENTITY#{cleaned}")
                    )
                    for item in resp.get("Items", []):
                        raw_to = item.get("to_id") or item.get("to_concept_id") or ""
                        target = _clean_id(raw_to) if raw_to else ""
                        rel_type = item.get("rel_type") or item.get("relation_type") or "RELATED_TO"
                        weight = float(item.get("score") or item.get("relation_weight") or 1.0)
                        edge = {
                            "from": cleaned,
                            "to": target,
                            "relation": rel_type,
                            "weight": weight,
                            "hop": hop + 1,
                        }
                        results.append(edge)
                        if target and target.lower() not in visited_nodes and len(visited_nodes) < 40:
                            next_frontier.append(target)
                        if len(visited_nodes) >= 40:
                            break
                except Exception as e:
                    logger.debug("Graph expansion failed for %s: %s", entity, e)

                if len(visited_nodes) >= 40:
                    break
            frontier = next_frontier
            if not frontier or len(visited_nodes) >= 40:
                break

        return results


