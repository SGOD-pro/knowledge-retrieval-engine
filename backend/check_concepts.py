import os
from kre.shared.db.postgres import PostgresRepository

def check_concepts():
    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = "postgresql://postgres:postgres@localhost:5432/kre"
    repo = PostgresRepository()
    repo.initialize()
    
    valid_types = {"PRODUCT", "PERSON", "ORGANIZATION", "METRIC", "POLICY", "PROCESS", "DATE_PERIOD", "LOCATION", "ISSUE", "REGULATION", "TERM"}
    try:
        with repo._connect() as connection:
            rows = connection.execute("SELECT type, COUNT(*) FROM concepts GROUP BY type").fetchall()
            print("Existing concept types:")
            non_conforming = 0
            for row in rows:
                print(f"- {row[0]}: {row[1]}")
                if row[0] not in valid_types:
                    non_conforming += row[1]
            print(f"\nNon-conforming concept rows found: {non_conforming}")
            if non_conforming > 0:
                print("ACTION REQUIRED: Migrate or flag non-conforming types.")
    except Exception as e:
        print(f"Error querying concepts (table might not exist): {e}")

if __name__ == "__main__":
    check_concepts()
