import traceback
from kre.shared.db.postgres import PostgresRepository
db = PostgresRepository()
try:
    embedding_str = f'[{",".join(str(x) for x in [0.0]*1024)}]'
    query_sql = '''
        SELECT id
        FROM chunks
        WHERE embedding_full IS NOT NULL
        ORDER BY embedding_full <=> %s::vector ASC LIMIT 10
    '''
    with db._connect() as connection:
        rows = connection.execute(query_sql, [embedding_str]).fetchall()
        print(f'SQL returned {len(rows)} rows')
except Exception as e:
    print("SQL failed")
    traceback.print_exc()
