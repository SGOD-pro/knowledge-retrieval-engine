"""Output Renderer enforcing strict output contracts without data fabrication.

Contracts supported:
- json: validates JSON syntax and schema. In strict mode, rejects invalid responses with
  explicit 'output_contract_violation' instead of fabricating data or returning raw text.
- number: normalizes numbers and decimals cleanly (e.g. 882.0 -> 882).
- boolean: resolves to strict boolean strings ("True" / "False").
- list: formats clean items or JSON lists.
- table: formats markdown tables.
- date: formats ISO 8601 dates (YYYY-MM-DD).
- text: default string rendering.
"""

from decimal import Decimal
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class OutputContractViolation(Exception):
    """Raised when an output cannot satisfy the requested contract in strict mode."""
    pass


def render_output(
    value: Any,
    contract: str = "text",
    strict_contract: bool = False,
    required_json_keys: list[str] | None = None,
) -> str:
    """Render answer according to contract without inventing data."""
    if value is None:
        return "NOT_FOUND"

    c_lower = contract.strip().lower()

    # 1. JSON Contract
    if c_lower == "json":
        # If already a dict or list, dump to JSON
        if isinstance(value, (dict, list)):
            return json.dumps(value, indent=2)

        s_val = str(value).strip()
        # Clean markdown code block wraps if present
        clean_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", s_val, flags=re.MULTILINE).strip()
        try:
            parsed = json.loads(clean_json)
            # Check required schema keys if specified
            if required_json_keys and isinstance(parsed, dict):
                missing = [k for k in required_json_keys if k not in parsed]
                if missing:
                    if strict_contract:
                        return json.dumps({
                            "status": "output_contract_violation",
                            "error": f"Missing required fields: {missing}",
                            "raw_content": s_val,
                        }, indent=2)
            return json.dumps(parsed, indent=2)
        except Exception as exc:
            if strict_contract:
                return json.dumps({
                    "status": "output_contract_violation",
                    "error": f"Invalid JSON response: {exc}",
                    "raw_content": s_val,
                }, indent=2)
            return s_val

    # 2. Number Contract
    if c_lower == "number":
        if isinstance(value, (int, Decimal)):
            # Format cleanly
            if isinstance(value, Decimal) and value == value.to_integral_value():
                return str(int(value))
            return str(value)
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return str(value)

        s_val = str(value).strip()
        # Extract leading or primary numeric token
        num_m = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?", s_val)
        if num_m:
            num_clean = num_m.group(0).replace(",", "")
            if "." in num_clean and num_clean.endswith(".0"):
                return num_clean[:-2]
            return num_clean
        if strict_contract:
            raise OutputContractViolation(f"Value '{s_val}' cannot be parsed as a number")
        return s_val

    # 3. Boolean Contract
    if c_lower == "boolean":
        if isinstance(value, bool):
            return "True" if value else "False"
        s_val = str(value).strip().lower()
        if s_val in ("true", "yes", "1", "correct"):
            return "True"
        if s_val in ("false", "no", "0", "incorrect"):
            return "False"
        if strict_contract:
            raise OutputContractViolation(f"Value '{value}' cannot be resolved to boolean")
        return str(value)

    # 4. List Contract
    if c_lower == "list":
        if isinstance(value, (list, tuple)):
            return "\n".join(f"- {item}" for item in value)
        return str(value)

    # 5. Date Contract
    if c_lower == "date":
        # Check ISO date
        iso_m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", str(value))
        if iso_m:
            return iso_m.group(1)
        return str(value)

    # Default text rendering
    return str(value)
