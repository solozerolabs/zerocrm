const test = require("node:test");
const assert = require("node:assert/strict");
const { toCsv, csvField } = require("../zerocrm/static/csv.js");

const COLUMNS = [
  { key: "name", label: "Name" },
  { key: "email", label: "Email" },
  { key: "company", label: "Company" },
  { key: "phone", label: "Phone" },
];

test("toCsv serializes rows with header in the requested column order", () => {
  const rows = [
    { name: "Jane Doe", email: "jane@acme.com", company: "Acme", phone: "555-1234" },
  ];
  const csv = toCsv(rows, COLUMNS);
  assert.equal(csv, "Name,Email,Company,Phone\r\nJane Doe,jane@acme.com,Acme,555-1234\r\n");
});

test("toCsv quotes fields containing a comma, quote, or newline", () => {
  const rows = [
    { name: 'Doe, "Jane"', email: "jane@acme.com", company: "Line1\nLine2", phone: "" },
  ];
  const csv = toCsv(rows, COLUMNS);
  const dataLine = csv.split("\r\n")[1];
  assert.equal(dataLine, '"Doe, ""Jane"""' + "," + "jane@acme.com" + "," + '"Line1\nLine2"' + ",");
});

test("toCsv treats null/undefined values as empty fields", () => {
  const rows = [{ name: "Ann", email: null, company: undefined, phone: "" }];
  const csv = toCsv(rows, COLUMNS);
  assert.equal(csv, "Name,Email,Company,Phone\r\nAnn,,,\r\n");
});

test("csvField only quotes when needed", () => {
  assert.equal(csvField("plain"), "plain");
  assert.equal(csvField("a,b"), '"a,b"');
});
