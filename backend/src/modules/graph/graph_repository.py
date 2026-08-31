import logging
from config import settings

logger = logging.getLogger(__name__)


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
                concept_id = item.get("concept_id") or item.get("PK", "").replace(
                    "ENTITY#", ""
                )
                label = item.get("canonical_name") or concept_id
                c_type = item.get("concept_type", "Concept")
                nodes.append(
                    {
                        "id": concept_id,
                        "label": label,
                        "type": c_type,
                        "properties": {
                            "frequency": int(item.get("frequency", 1)),
                            "doc_count": int(item.get("doc_count", 1)),
                        },
                    }
                )

            rel_resp = self.okf_relations_table.scan(Limit=50)
            for item in rel_resp.get("Items", []):
                edges.append(
                    {
                        "source": item.get("from_concept_id", ""),
                        "target": item.get("to_concept_id", ""),
                        "label": item.get("relation_type", "RELATED_TO"),
                        "weight": float(item.get("relation_weight", 1.0)),
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
                resp = self.okf_properties_table.query(
                    KeyConditionExpression=Key("PK").eq(f"ENTITY#{entity.lower()}")
                )
                results.extend(resp.get("Items", []))
            except Exception as e:
                logger.debug("OKF property lookup failed for %s: %s", entity, e)
        return results

