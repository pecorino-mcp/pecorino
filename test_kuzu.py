import gorgonzola
db_path = "/home/lechibang/.pecorino/indexes/415ed345bf0504a1c653a175e1776021_gorgonzola"
db = gorgonzola.Database(db_path)
conn = gorgonzola.Connection(db)
conn.execute("CHECKPOINT;")
