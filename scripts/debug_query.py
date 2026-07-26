import gorgonzola
import argparse
import sys
import os

def main():
    parser = argparse.ArgumentParser(description="Run a debug query on the Gorgonzola database.")
    parser.add_argument("db_path", help="Path to the Gorgonzola database directory")
    parser.add_argument("query", help="Cypher query to execute")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.db_path):
        print(f"Error: Database path does not exist: {args.db_path}")
        sys.exit(1)
        
    print(f"Connecting to database: {args.db_path}")
    db = gorgonzola.Database(args.db_path, buffer_pool_size=256 * 1024 * 1024)
    conn = gorgonzola.Connection(db)
    
    print(f"Executing query:\n{args.query}\n")
    try:
        res = conn.execute(args.query)
        if res.has_next():
            print("Results:")
            print(res.get_as_df())
        else:
            print("Query executed successfully, no results returned.")
    except Exception as e:
        print(f"Error executing query: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
