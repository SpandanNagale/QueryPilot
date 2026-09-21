export function ResultsTable({ columns, rows, truncated }: { columns: string[]; rows: Record<string, unknown>[]; truncated?: boolean }) {
  if (rows.length === 0) {
    return <p className="text-xs font-telemetry text-ink-2">No rows returned.</p>;
  }
  const cols = columns.length > 0 ? columns : Object.keys(rows[0]);
  return (
    <div className="overflow-x-auto rounded-lg border border-border-0">
      <table className="w-full text-xs font-telemetry">
        <thead>
          <tr className="bg-surface-2 text-ink-1">
            {cols.map((c) => (
              <th key={c} className="px-3 py-2 text-left font-medium whitespace-nowrap">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-t border-border-0 odd:bg-surface-1 even:bg-surface-0">
              {cols.map((c) => (
                <td key={c} className="px-3 py-1.5 text-ink-0 whitespace-nowrap">
                  {String(row[c] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {truncated && (
        <p className="px-3 py-1.5 text-[11px] text-ink-2 border-t border-border-0">
          Results truncated to the first {rows.length} rows.
        </p>
      )}
    </div>
  );
}
