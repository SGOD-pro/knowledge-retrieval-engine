import pytest

from ingestion.header_detector import detect_header_region
from schemas.structured_table import HeaderTopology


def test_standard_row0_header():
    rows = [
        ["District", "Households", "State"],
        ["Nicobars", 882, "Andaman"],
        ["South Andaman", 1200, "Andaman"],
    ]
    schema, data_start = detect_header_region(rows)
    assert schema.header_topology == HeaderTopology.SINGLE_ROW
    assert schema.header_rows == (0,)
    assert data_start == 1
    assert [c.name for c in schema.columns] == ["District", "Households", "State"]
    assert schema.confidence >= 0.70


def test_numeric_year_headers():
    rows = [
        ["Country", 2019, 2020, 2021],
        ["India", 100, 110, 125],
        ["USA", 500, 520, 550],
    ]
    schema, data_start = detect_header_region(rows)
    assert schema.header_topology == HeaderTopology.SINGLE_ROW
    assert schema.header_rows == (0,)
    assert data_start == 1
    assert [c.name for c in schema.columns] == ["Country", "2019", "2020", "2021"]
    assert schema.confidence >= 0.65


def test_leading_notes_before_header():
    rows = [
        ["CONFIDENTIAL FINANCIAL REPORT - FOR INTERNAL USE ONLY", None, None],
        ["Source: Central Treasury Department", None, None],
        [None, None, None],
        ["Quarter", "Revenue (INR Cr)", "Operating Profit"],
        ["Q1", 450.5, 92.0],
        ["Q2", 510.0, 105.2],
    ]
    schema, data_start = detect_header_region(rows, max_scan_rows=10)
    assert schema.header_topology == HeaderTopology.SINGLE_ROW
    assert schema.header_rows == (3,)
    assert data_start == 4
    assert [c.name for c in schema.columns] == ["Quarter", "Revenue (INR Cr)", "Operating Profit"]
    assert schema.confidence >= 0.65


def test_multi_row_hierarchical_headers():
    rows = [
        ["Region", "FY2021", "FY2022"],
        ["Region", "Sales", "Sales"],
        ["North", 1200, 1450],
        ["South", 980, 1100],
    ]
    schema, data_start = detect_header_region(rows)
    assert schema.header_topology == HeaderTopology.MULTI_ROW
    assert schema.header_rows == (0, 1)
    assert data_start == 2
    col_names = [c.name for c in schema.columns]
    assert col_names[0] == "Region"
    assert "FY2021_Sales" in col_names[1]
    assert "FY2022_Sales" in col_names[2]


def test_safe_fallback_preserves_row_0_as_data():
    # Pure numeric matrix with no header
    rows = [
        [10.5, 20.2, 30.1],
        [40.0, 50.8, 60.3],
        [70.2, 80.9, 90.0],
    ]
    schema, data_start = detect_header_region(rows, confidence_threshold=0.60)
    # Must assign HeaderTopology.NONE
    assert schema.header_topology == HeaderTopology.NONE
    assert schema.header_rows == ()
    assert schema.confidence == 0.0
    # Must preserve Row 0 as regular data!
    assert data_start == 0
    assert [c.name for c in schema.columns] == ["Col_1", "Col_2", "Col_3"]
