import duckdb

db_path = "data/duckdb/prod.duckdb"
out_path = "ticker_metadata_export.csv"

con = duckdb.connect(db_path, read_only=True)

sql = """
COPY (
    SELECT ticker, company_name, sector, industry
    FROM ticker_master
    WHERE active_flag = true
    ORDER BY ticker
)
TO 'ticker_metadata_export.csv'
WITH (HEADER, DELIMITER ',')
"""

con.execute(sql)
con.close()

print(f"Exported to: {out_path}")
