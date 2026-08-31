// Client-side CSV serializer for the contacts export button (see /contacts
// in serve.py). Plain functions, no build step: loaded inline in the browser
// and required directly from the Node test in tests/contacts_csv.test.js.

function csvField(value) {
  const s = String(value == null ? "" : value);
  if (/[",\n]/.test(s)) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function toCsv(rows, columns) {
  const lines = [columns.map((c) => csvField(c.label)).join(",")];
  for (const row of rows) {
    lines.push(columns.map((c) => csvField(row[c.key])).join(","));
  }
  return lines.join("\r\n") + "\r\n";
}

function triggerDownload(filename, csvText) {
  const blob = new Blob([csvText], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { csvField, toCsv, triggerDownload };
}
