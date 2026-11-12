"""One disposable PG17 physical recovery domain, its archive and exact replay receipts."""
import hashlib
import shlex
import shutil
import time

import psycopg
from sqlalchemy.engine import make_url


class PhysicalPeer:
    def __init__(self, stack, pg, pg_bin, root):
        self.stack, self.pg, self.bin, self.root = stack, pg, pg_bin, root
        root.mkdir()
        self.archive, self.source, self.image = (root / n for n in ("archive", "source", "base"))
        self.archive.mkdir()
        pg._run([pg_bin / "initdb", "-D", self.source, "-U", "nf_dev", "--encoding=UTF8",
                 "--locale=C", "--auth-local=reject", "--auth-host=trust", "--data-checksums"])
        (self.source / "pg_hba.conf").write_text(
            "host all nf_dev 127.0.0.1/32 trust\nhost replication nf_dev 127.0.0.1/32 trust\n"
            "host all all 127.0.0.1/32 scram-sha-256\n")
        segment = f"{shlex.quote(str(self.archive))}/%f"
        self.port = pg._port()
        self.settings = dict(listen_addresses="127.0.0.1", unix_socket_directories="", port=self.port,
            max_connections=35, shared_buffers="48MB", fsync="on", synchronous_commit="on",
            full_page_writes="on", wal_level="replica", archive_mode="on", max_wal_senders=4,
            archive_command=f"test -f {segment} || (cp %p {segment}.part && mv {segment}.part {segment})")
        config = root / "source.conf"
        pg._config(config, self.settings)
        stack.callback(pg._stop, pg._start(pg_bin, self.source, config, self.port, root / "source.log"))
        self.dsn = f"postgresql://nf_dev@127.0.0.1:{self.port}/postgres"
        self.admin = stack.enter_context(psycopg.connect(self.dsn, autocommit=True))
        identity, timeline, checksums = self.admin.execute(
            "SELECT s.system_identifier::text,c.timeline_id,current_setting('data_checksums') "
            "FROM pg_control_system() s, pg_control_checkpoint() c").fetchone()
        assert checksums == "on"
        self.evidence = {"system_identifier": identity, "source_timeline": timeline, "data_checksums": checksums,
                         "restore_points": {}, "recoveries": {}}

    def backup(self):
        self.pg._run([self.bin / "pg_basebackup", "-d", self.dsn, "-D", self.image, "-Fp",
                      "-Xstream", "--checkpoint=fast", "--manifest-checksums=SHA256"])
        result = self.pg._run([self.bin / "pg_verifybackup", self.image])
        digest = hashlib.sha256((self.image / "backup_manifest").read_bytes()).hexdigest()
        self.evidence.update(manifest_sha256=digest, verification=result)
        return result, digest

    def mark(self, name):
        lsn = self.admin.execute("SELECT pg_create_restore_point(%s)", (name,)).fetchone()[0]
        filename = self.admin.execute("SELECT pg_walfile_name(%s::pg_lsn)", (lsn,)).fetchone()[0]
        self.evidence["restore_points"][name] = {"lsn": lsn, "wal_segment": filename}
        return lsn

    def archive_targets(self):
        self.admin.execute("SELECT pg_switch_wal()")
        until = time.monotonic() + 60
        for record in self.evidence["restore_points"].values():
            path = self.archive / record["wal_segment"]
            while not path.exists():
                assert time.monotonic() < until, "WAL archive deadline exceeded"
                time.sleep(.1)
            record["wal_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()

    def restore(self, target, label=None):
        name, port = label or target, self.pg._port()
        data = self.root / ("data-" + name)
        shutil.copytree(self.image, data)
        (data / "recovery.signal").touch()
        config, log = self.root / (name + ".conf"), self.root / (name + ".log")
        self.pg._config(config, {**self.settings, "port": port, "archive_mode": "off",
            "restore_command": f"cp {shlex.quote(str(self.archive))}/%f %p",
            "recovery_target_name": target, "recovery_target_action": "promote"})
        self.stack.callback(self.pg._stop, self.pg._start(self.bin, data, config, port, log))
        dsn = make_url(self.dsn).set(port=port).render_as_string(hide_password=False)
        with psycopg.connect(dsn, autocommit=True) as ctl:
            until = time.monotonic() + 60
            while ctl.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
                assert time.monotonic() < until
                time.sleep(.1)
            ctl.execute("CHECKPOINT")
            system, timeline, replay = ctl.execute(
                "SELECT s.system_identifier::text,c.timeline_id,pg_last_wal_replay_lsn() "
                "FROM pg_control_system() s,pg_control_checkpoint() c").fetchone()
        assert f'recovery stopping at restore point "{target}"' in log.read_text()
        assert system == self.evidence["system_identifier"] and timeline > self.evidence["source_timeline"]
        assert replay == self.evidence["restore_points"][target]["lsn"]
        self.evidence["recoveries"][name] = {"target": target, "system_identifier": system,
                                            "promoted_timeline": timeline, "replay_lsn": replay}
        return port, replay
