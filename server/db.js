import { DatabaseSync } from 'node:sqlite';

export function createDatabase() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE users (user_id INTEGER PRIMARY KEY, name TEXT, region TEXT, city TEXT, registered_at TEXT);
    CREATE TABLE products (product_id INTEGER PRIMARY KEY, name TEXT, category TEXT, unit_price REAL);
    CREATE TABLE orders (order_id INTEGER PRIMARY KEY, order_date TEXT, user_id INTEGER, product_id INTEGER, quantity INTEGER, amount REAL, status TEXT);
    INSERT INTO users VALUES (1,'王芳','华东','上海','2024-01-12'),(2,'李明','华东','杭州','2024-02-03'),(3,'张伟','华南','深圳','2024-02-18'),(4,'陈静','华北','北京','2024-03-01');
    INSERT INTO products VALUES (101,'无线耳机','数码',499),(102,'保温杯','家居',129),(103,'跑鞋','运动',699),(104,'键盘','数码',399);
    INSERT INTO orders VALUES (1001,'2026-07-03',1,101,1,499,'PAID'),(1002,'2026-07-09',2,102,2,258,'PAID'),(1003,'2026-07-16',1,104,1,399,'PAID'),(1004,'2026-07-21',3,103,1,699,'PAID'),(1005,'2026-07-27',2,101,1,499,'PAID'),(1006,'2026-06-26',4,102,1,129,'PAID'),(1007,'2026-07-30',1,103,1,699,'CANCELLED');`);
  return db;
}
