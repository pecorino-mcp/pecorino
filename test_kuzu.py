import gorgonzola
db_path = "/home/lechibang/.pecorino/indexes/415ed345bf0504a1c653a175e1776021_gorgonzola"
db = gorgonzola.Database(db_path)
conn = gorgonzola.Connection(db)
res = conn.execute("MATCH (n:CodeNode) WHERE n.kind IN ['Function', 'Method', 'function', 'method'] AND (n.name CONTAINS 'download' OR n.name CONTAINS 'Download' OR n.name CONTAINS 'model' OR n.name CONTAINS 'Model') RETURN n.name, n.file, n.kind LIMIT 50")
while res.has_next():
    print(res.get_next())
