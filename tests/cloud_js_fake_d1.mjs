// Disposable SQLite implementation of the D1 prepare/batch contract.
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

export class FakeD1 {
  constructor() {
    this.sqlite = new DatabaseSync(':memory:');
    this.sqlite.exec(readFileSync(new URL('../deploy/cloudflare/schema.sql', import.meta.url), 'utf8'));
    this.sqlite.exec('PRAGMA foreign_keys = ON');
  }
  execute(sql, values = []) {
    const before = this.sqlite.prepare('SELECT total_changes() AS n').get().n;
    const results = this.sqlite.prepare(sql).all(...values);
    const after = this.sqlite.prepare('SELECT total_changes() AS n').get().n;
    return { results, meta: { changes: Number(after - before) } };
  }
  prepare(sql) {
    return { run: async () => this.execute(sql),
      first: async () => this.sqlite.prepare(sql).get(),
      all: async () => ({ results: this.sqlite.prepare(sql).all() }),
      bind: (...values) => ({
      run: async () => this.execute(sql, values),
      execute: () => this.execute(sql, values),
      first: async () => this.sqlite.prepare(sql).get(...values),
      all: async () => ({ results: this.sqlite.prepare(sql).all(...values) }),
    }) };
  }
  async batch(statements) {
    this.sqlite.exec('BEGIN');
    try {
      const results = statements.map(statement => statement.execute());
      this.sqlite.exec('COMMIT');
      return results;
    } catch (error) {
      this.sqlite.exec('ROLLBACK');
      throw error;
    }
  }
  rows(table) {
    const safe = new Set(['cloud_update_claims', 'cloud_report_claims', 'cloud_outbox',
      'cloud_accounts', 'cloud_plans', 'cloud_plan_revisions', 'cloud_transactions',
      'cloud_snapshots', 'cloud_snapshot_sources', 'cloud_position_snapshots',
      'cloud_cash_snapshots', 'cloud_theses', 'cloud_thesis_revisions',
      'cloud_mutation_checks', 'cloud_market_cache', 'cloud_manual_input_revisions',
      'cloud_policy', 'cloud_policy_revisions']);
    if (!safe.has(table)) throw new TypeError('unsupported fixture table');
    return this.sqlite.prepare(`SELECT * FROM ${table}`).all();
  }
}
