// Bootstrap read-only user + seed data. Runs once on container first start.
// Connects as root (MONGO_INITDB_ROOT_USERNAME/PASSWORD).

db = db.getSiblingDB("dbread_test");

db.createUser({
  user: "ai_ro",
  pwd: "ro_pw",
  roles: [
    { role: "read", db: "dbread_test" },
    // Second database for the `mongo.databases` allowlist tests.
    { role: "read", db: "dbread_extra" },
  ],
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

// --- inspection fixtures (separate so the data above stays as it was) ---

// Collection with a validator; `phone` holds both strings and numbers.
db.createCollection("customers", {
  validator: { $jsonSchema: { bsonType: "object", required: ["name"] } },
});
db.customers.insertMany([
  { _id: 1, name: "A", phone: "0123" },
  { _id: 2, name: "B", phone: NumberInt(12345) },
  { _id: 3, name: "C", phone: "0999" },
]);

db.createView("active_users", "users", [{ $match: { status: "active" } }]);

// user_id 42 matches no user (orphan); user_id 1 appears twice (duplicate);
// `source` never varies (constant); `legacy` is always null (null column).
db.reviews.insertMany([
  { _id: 1, user_id: 1, stars: 5, source: "web", legacy: null },
  { _id: 2, user_id: 42, stars: 1, source: "web", legacy: null },
  { _id: 3, user_id: 1, stars: 4, source: "web", legacy: null },
]);

db.getSiblingDB("dbread_extra").events.insertMany([
  { _id: 1, kind: "signup" },
  { _id: 2, kind: "login" },
]);
