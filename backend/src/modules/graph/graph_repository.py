import logging
from config import settings

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
                resp = self.okf_properties_table.query(
                    KeyConditionExpression=Key("PK").eq(f"ENTITY#{entity.lower()}")
                )
                results.extend(resp.get("Items", []))
            except Exception as e:
                logger.debug("OKF property lookup failed for %s: %s", entity, e)
        return results

