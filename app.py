import sqlite3

def main():
    conn = sqlite3.connect('db.sqlite')
    cur = conn.cursor()
    cur.execute('create table if not exists data (name text)')
    cur.execute('insert into data (name) values (\'hello\')')
    conn.commit()
    cur.execute('select * from data')
    rows = cur.fetchall()
    for row in rows:
        print(row[0])
    conn.close()

if __name__ == '__main__':
    main()
