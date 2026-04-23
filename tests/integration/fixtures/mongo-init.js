// Bootstrap read-only user + seed data. Runs once on container first start.
// Connects as root (MONGO_INITDB_ROOT_USERNAME/PASSWORD).

db = db.getSiblingDB("dbread_test");

db.createUser({
  user: "ai_ro",
  pwd: "ro_pw",
  roles: [{ role: "read", db: "dbread_test" }],
});

db.users.insertMany([
  { _id: 1, email: "a@x.com", status: "active", created: new Date("2026-01-01"), age: 28 },
  { _id: 2, email: "b@x.com", status: "inactive", created: new Date("2026-02-01"), age: 35 },
  { _id: 3, email: "c@x.com", status: "active", created: new Date("2026-03-01"), age: 42, tags: ["vip", "beta"] },
]);

db.orders.insertMany([
  { _id: 1, user_id: 1, amount: 100, status: "paid" },
  { _id: 2, user_id: 1, amount: 50, status: "refunded" },
  { _id: 3, user_id: 3, amount: 200, status: "paid" },
]);

db.users.createIndex({ email: 1 }, { unique: true });
db.orders.createIndex({ user_id: 1 });
