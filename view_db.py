from app import app, db
from sqlalchemy import inspect, text

with app.app_context():
    inspector = inspect(db.engine)
    for table in inspector.get_table_names():
        print(f"\n{'='*40}\nTABLE: {table}\n{'='*40}")
        result = db.session.execute(text(f"SELECT * FROM {table}"))
        print(" | ".join(result.keys()))
        print("-" * 40)
        for row in result.fetchall():
            print(" | ".join(str(val) for val in row))
