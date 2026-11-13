"""Stdlib-only SQLite fixture derived from all current declarative model fields.

Preserves column affinity/nullability, keys, FK/delete policy, checks and indexes.
Does not emulate SQLAlchemy Python-side defaults or its ORM; seed supplies values.
"""
import ast
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-10-29T00:00:00Z"
FINGERPRINT = "fixture_" + "a" * 64


def schema_sql():
    tree = ast.parse((ROOT / "src/account_bot/storage/models.py").read_text())
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    mixin = next(n for n in classes if n.name == "TimestampMixin")
    statements = []
    indexes = []
    for cls in classes:
        name = next((ast.literal_eval(n.value) for n in cls.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in n.targets)), None)
        if name is None:
            continue
        nodes = list(cls.body)
        if any(isinstance(b, ast.Name) and b.id == "TimestampMixin" for b in cls.bases):
            nodes += mixin.body
        columns = []
        for node in nodes:
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.value, ast.Call):
                continue
            field = node.target.id
            call = node.value
            kw = {k.arg: k.value for k in call.keywords}
            args = call.args
            type_name = next((a.id if isinstance(a, ast.Name) else a.func.id for a in args if isinstance(a, ast.Name) or isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id != "ForeignKey"), None)
            annotation = ast.unparse(node.annotation)
            affinity = "INTEGER" if type_name in ("Integer", "Boolean") or type_name is None and ("int" in annotation or "bool" in annotation) else "NUMERIC" if type_name in ("Numeric", "MONEY", "RATIO") else "TEXT"
            primary = ast.literal_eval(kw.get("primary_key", ast.Constant(False)))
            nullable = ast.literal_eval(kw["nullable"]) if "nullable" in kw else "None" in annotation
            column = f'"{field}" {affinity}' + (" PRIMARY KEY" if primary else "") + (" NOT NULL" if not nullable and not primary else "")
            if "unique" in kw and ast.literal_eval(kw["unique"]):
                column += " UNIQUE"
            for arg in args:
                if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "ForeignKey":
                    table, key = ast.literal_eval(arg.args[0]).split(".")
                    column += f' REFERENCES "{table}"("{key}")'
                    delete = next((ast.literal_eval(k.value) for k in arg.keywords if k.arg == "ondelete"), None)
                    if delete:
                        column += " ON DELETE " + delete
            columns.append(column)
            if "index" in kw and ast.literal_eval(kw["index"]):
                indexes.append(f'CREATE INDEX "ix_{name}_{field}" ON "{name}"("{field}");')
        table_args = next((n.value for n in cls.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__table_args__" for t in n.targets)), None)
        if isinstance(table_args, ast.Tuple):
            for c in table_args.elts:
                if not isinstance(c, ast.Call):
                    continue
                args = [ast.literal_eval(a) for a in c.args]
                if c.func.id == "UniqueConstraint":
                    columns.append('UNIQUE (' + ','.join('"'+a+'"' for a in args) + ')')
                elif c.func.id == "CheckConstraint":
                    columns.append('CHECK ('+args[0]+')')
                elif c.func.id == "Index":
                    indexes.append(f'CREATE INDEX "{args[0]}" ON "{name}"('+','.join('"'+a+'"' for a in args[1:])+');')
                else:
                    raise AssertionError(c.func.id)
        statements.append(f'CREATE TABLE "{name}"('+','.join(columns)+');')
    return '\n'.join(statements + indexes)


def seed(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(schema_sql())
        db.execute("PRAGMA foreign_keys=ON")
        def put(table, **values):
            cols = list(values)
            db.execute(f'INSERT INTO "{table}" ({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})',tuple(values[k] for k in cols))
        put("portfolios", id=1,key="fixture",name="fixture",base_currency="KRW",timezone_name="Asia/Seoul",enabled=1,created_at=STAMP,updated_at=STAMP)
        put("broker_accounts",id=1,portfolio_id=1,broker="kiwoom_accum",account_fingerprint=FINGERPRINT,masked_label="fixture alias",base_currency="KRW",enabled=1,created_at=STAMP,updated_at=STAMP)
        put("instruments",id=1,market="US",ticker="TEST",asset_type="ETF",currency="USD",company_name="synthetic",created_at=STAMP,updated_at=STAMP)
        common=dict(instrument_id=1,account_id=1,source="TELEGRAM",enabled=1,daily_amount=2000,currency="KRW",target_weight=None,timezone_name="Asia/Seoul",started_at=STAMP,memo="synthetic")
        put("recurring_buy_plans",id=1,portfolio_id=1,plan_key="fixture:US:TEST",version=1,created_at=STAMP,updated_at=STAMP,**common)
        put("recurring_buy_revisions",id=1,plan_id=1,revision_no=1,change_hash="b"*64,effective_from=STAMP,created_at=STAMP,**common)
        put("transactions",id=1,account_id=1,source_key="fixture-trade",instrument_id=1,transaction_type="BUY",status="FILLED",traded_at=STAMP,currency="USD",quantity=1,unit_price=None,gross_amount=None,fee=None,tax=None,net_amount=None,created_at=STAMP)
        put("investment_theses",id=1,portfolio_id=1,instrument_id=1,status="ACTIVE",current_revision_no=1,created_at=STAMP,updated_at=STAMP)
        put("thesis_revisions",id=1,thesis_id=1,revision_no=1,revision_key="fixture-thesis",thesis_text="Synthetic thesis",content_hash="c"*64,effective_at=STAMP,created_at=STAMP)
        put("reports",id=1,portfolio_id=1,idempotency_key="fixture-report",report_type="DAILY",period_key="2026-10-29",audience_fingerprint=FINGERPRINT,timezone_name="Asia/Seoul",status="READY",claimed_at=STAMP)
        put("report_deliveries",id=1,report_id=1,channel="telegram",destination_fingerprint=FINGERPRINT,status="UNKNOWN",attempt_count=1)
        put("schema_migrations",version=1,name="fixture-v1",checksum="d"*64,applied_at=STAMP)
        put("schema_migrations",version=2,name="fixture-v2",checksum="e"*64,applied_at=STAMP)
        put("saved_investment_scenarios",id=1,portfolio_id=1,name="unchanged private scenario",created_at=STAMP,updated_at=STAMP)
        put("saved_scenario_plans",id=1,scenario_id=1,market="US",ticker="TEST",asset_type="ETF",instrument_currency="USD",configured_amount_krw="2000.000000000000000001",enabled=1)
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 29
