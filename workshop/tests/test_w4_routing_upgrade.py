"""Compare deployed W4 schemas with the signed W2 routing projection."""
import hashlib
import json
import os
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from test_installation_ownership import isolated_database


def route_view(conn):
    return conn.execute("""
        SELECT oid, relacl::text, pg_get_viewdef(oid)
        FROM pg_class WHERE relnamespace = current_schema()::regnamespace
          AND relname = 'aw_current_message_edges' AND relkind = 'v'
    """).fetchone()


def catalog(conn):
    """Compare the full logical schema created by both histories; omit physical OIDs."""
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    definitions = conn.execute("""
        SELECT 'relation', relname, relkind::text
          FROM pg_class WHERE relnamespace=current_schema()::regnamespace
        UNION ALL
        SELECT 'column', c.relname || '.' || a.attname,
               concat_ws('|',format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,
                         a.attgenerated,pg_get_expr(d.adbin,d.adrelid))
          FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
          LEFT JOIN pg_attrdef d ON (d.adrelid,d.adnum)=(c.oid,a.attnum)
         WHERE c.relnamespace=current_schema()::regnamespace AND c.relkind IN ('r','v')
           AND a.attnum>0 AND NOT a.attisdropped
        UNION ALL
        SELECT 'constraint', conrelid::regclass::text || '.' || conname, pg_get_constraintdef(oid)
          FROM pg_constraint WHERE connamespace=current_schema()::regnamespace
        UNION ALL
        SELECT 'index', indexname, indexdef FROM pg_indexes WHERE schemaname=current_schema()
        UNION ALL
        SELECT 'view', viewname, definition FROM pg_views WHERE schemaname=current_schema()
        ORDER BY 1,2,3
    """).fetchall()
    return [(kind, name, definition.replace(schema + '.', '')) for kind, name, definition in definitions]


def rows(conn, fields=None):
    if fields is None:
        fields = {}
        for table, name in conn.execute("""
            SELECT c.table_name,c.column_name FROM information_schema.columns c
            JOIN information_schema.tables t USING (table_schema,table_name)
            WHERE c.table_schema=current_schema() AND t.table_type='BASE TABLE'
              AND c.table_name <> 'aw_alembic_version' ORDER BY 1,c.ordinal_position
        """):
            fields.setdefault(table, []).append(name)
    result = {}
    for table, names in fields.items():
        query = sql.SQL("SELECT to_jsonb(r)::text FROM (SELECT {} FROM {}) r ORDER BY 1").format(
            sql.SQL(',').join(map(sql.Identifier, names)), sql.Identifier(table))
        values = [row[0] for row in conn.execute(query)]
        result[table] = (len(values), hashlib.sha256(json.dumps(values).encode()).hexdigest())
    return fields, result


def verify_signed_catalog(dsn):
    base = make_url(dsn).difference_update_query(["options"]).render_as_string(hide_password=False)
    with psycopg.connect(dsn) as upgraded, psycopg.connect(base) as fresh:
        actual, expected = catalog(upgraded), catalog(fresh)
        assert actual == expected
        assert route_view(upgraded)[2] == route_view(fresh)[2]
        return hashlib.sha256(json.dumps(actual).encode()).hexdigest()


def save_receipt(name, details):
    if folder := os.environ.get("AW_W4_ROUTING_UPGRADE_EVIDENCE_DIR"):
        dest = Path(folder)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / (name + '.json')).write_text(json.dumps(details, indent=2) + '\n')


def test_signed_route_view_keeps_identity_and_grants(isolated_database):
    dsn, config = isolated_database
    command.upgrade(config, 'w004b')
    with psycopg.connect(dsn) as conn:
        conn.execute('GRANT SELECT ON aw_current_message_edges TO PUBLIC')
        before = route_view(conn)
        assert before and before[1]
    command.upgrade(config, 'head')
    command.upgrade(config, 'head')
    with psycopg.connect(dsn) as conn:
        assert route_view(conn) == before
        head = ScriptDirectory.from_config(config).get_current_head()
        assert conn.execute('SELECT version_num FROM aw_alembic_version').fetchone() == (head,)
    save_receipt('signed-view', {'same_oid': True, 'same_acl': True, 'same_definition': True,
                               'catalog_sha256': verify_signed_catalog(dsn)})
