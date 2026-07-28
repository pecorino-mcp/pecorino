import gorgonzola
import os
db_path = "/home/lechibang/.pecorino/indexes/415ed345bf0504a1c653a175e1776021_gorgonzola"
db = gorgonzola.Database(db_path)
conn = gorgonzola.Connection(db)
res = conn.execute("MATCH (c:CodeNode) WHERE c.kind = 'Function' AND c.name CONTAINS 'download' RETURN c.name, c.file LIMIT 5")
res.rows_as_dict(True)
while res.has_next():
    try:
        print(res.get_next())
    except Exception as e:
        print(f"Error reading row: {e}")
