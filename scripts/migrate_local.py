"""One-shot copy from the initial local SQLite store into an EMPTY project PostgreSQL."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from sqlalchemy import create_engine,select,text
from dotenv import dotenv_values
from backend.db import Base,ROOT

def main():
    destination=dotenv_values(ROOT/'data/postgres.env')['DATABASE_URL']
    target=create_engine(destination)
    source=create_engine('sqlite:///'+(ROOT/'data/venture-lens.db').as_posix())
    Base.metadata.create_all(target)
    with target.begin() as tx,source.connect() as rx:
        if tx.execute(text('SELECT count(*) FROM runs')).scalar():raise RuntimeError('Destination is not empty; no data changed')
        for table in Base.metadata.sorted_tables:
            rows=[dict(x) for x in rx.execute(select(table)).mappings()]
            if rows:tx.execute(table.insert(),rows)
            print(table.name,len(rows))
        tx.execute(text("SELECT setval(pg_get_serial_sequence('events','id'), COALESCE((SELECT MAX(id) FROM events),1), EXISTS(SELECT 1 FROM events))"))
    local=ROOT/'.env.local';local.write_text(local.read_text(encoding='utf-8')+'DATABASE_URL='+destination+'\n',encoding='utf-8')
    print('Migration completed. SQLite retained as a local backup.')

if __name__=='__main__':main()
