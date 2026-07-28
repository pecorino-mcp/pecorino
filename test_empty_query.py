import gorgonzola
import shutil
shutil.rmtree('test_db', ignore_errors=True)
db = gorgonzola.Database('test_db')
conn = gorgonzola.Connection(db)
conn.execute("CREATE NODE TABLE CodeNode (id STRING, PRIMARY KEY(id))")
res = conn.execute("MATCH (a:CodeNode) RETURN a.id")
print("has_next:", res.has_next())
if res.has_next():
    print("get_next:", res.get_next())
