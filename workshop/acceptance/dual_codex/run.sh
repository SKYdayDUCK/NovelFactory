#!/usr/bin/env bash
# Disposable POSIX/PG17 qualification. All created users, databases, keys and processes are scoped to this run.
set -euo pipefail

if [[ "${1:-}" != "--broker-session" ]]; then
    if [[ $# != 4 ]]; then
        echo "usage: run.sh NF_GIT_CHECKOUT WORKSHOP_SUBDIRECTORY CODEX_BINARY ARTIFACT_DIRECTORY" >&2
        exit 2
    fi
    : "${AW_DUAL_TEST_ADMIN_URL:?Set a disposable PostgreSQL 17 administrator URL}"
    nf_repo=$(git -C "$1" rev-parse --show-toplevel)
    aw_repo=$(git -C "$2" rev-parse --show-toplevel)
    aw_prefix=$(git -C "$2" rev-parse --show-prefix)
    codex=$(realpath "$3")
    mkdir -p "$4"
    out=$(cd "$4" && pwd -P)
    test "$("$codex" --version)" = 'codex-cli 0.167.0-alpha.4'
    test -x /usr/sbin/sshd
    sudo -n true
    r=$(sudo -n mktemp -d /tmp/nf-aw-dual.XXXXXXXX)
    trap 'sudo -n rm -rf --one-file-system -- "$r"' EXIT
    sudo chmod 0711 "$r"
    sudo install -d -o "$(id -un)" -m 0755 "$r/nf" "$r/aw"
    sudo install -m 0755 "$codex" "$r/codex"
    sudo install -o "$(id -un)" -m 0644 /dev/null "$r/challenge.json"
    export PROOF_NF_SHA PROOF_AW_SHA
    PROOF_NF_SHA=$(git -C "$nf_repo" rev-parse HEAD)
    PROOF_AW_SHA=$(git -C "$aw_repo" rev-parse HEAD)
    aw_tree=$(git -C "$aw_repo" rev-parse "HEAD:${aw_prefix%/}")
    git -C "$nf_repo" archive "$PROOF_NF_SHA" | tar -xf - -C "$r/nf"
    git -C "$aw_repo" archive "$aw_tree" | tar -xf - -C "$r/aw"
    unset UV_PROJECT_ENVIRONMENT VIRTUAL_ENV PYTHONPATH
    uv sync --project "$r/nf" --locked --group dev --python "${DUAL_SYSTEM_PYTHON:-/usr/bin/python3.12}"
    uv sync --project "$r/aw" --locked --extra streams --no-install-project --python "${DUAL_SYSTEM_PYTHON:-/usr/bin/python3.12}"
    uv build --wheel "$r/aw" --out-dir "$r/aw/dist"
    wheels=("$r/aw"/dist/*.whl)
    test "${#wheels[@]}" = 1
    uv pip install --python "$r/aw/.venv/bin/python" --no-deps "${wheels[0]}"
    "$r/aw/.venv/bin/python" -I -c 'from pathlib import Path; import sys, agent_workshop.server as s; assert Path(s.__file__).resolve().is_relative_to(Path(sys.prefix))'
    export PROOF_AW_WHEEL_SHA
    PROOF_AW_WHEEL_SHA=$(sha256sum "${wheels[0]}" | cut -d' ' -f1)
    export AW_NF_SOURCE="$r/nf" PYTHONPATH="$r/aw/integration:$r/aw/src:$r/nf:$r/nf/src"
    "$r/nf/.venv/bin/python" - "$r" "$out" <<'PY'
from pathlib import Path
import os
import subprocess
import sys
from conftest import database_pair
import psycopg
from sqlalchemy.engine import make_url

root = Path(sys.argv[1])
aw_admin = os.environ["AW_DUAL_TEST_ADMIN_URL"]
nf_admin = os.environ.get("AW_DUAL_TEST_NF_ADMIN_URL")
with database_pair(aw_admin, nf_admin) as urls:
    systems = {}
    for service, admin in (("aw", aw_admin), ("nf", nf_admin or aw_admin)):
        # Inspect the actual new database's owner and physical cluster via its fixture admin.
        identity = make_url(urls[service])
        control = make_url(admin).set(drivername="postgresql", database=identity.database)
        with psycopg.connect(control.render_as_string(hide_password=False)) as conn:
            system, owner = conn.execute(
                """SELECT system_identifier::text, pg_get_userbyid(datdba)
                   FROM pg_control_system(), pg_database WHERE datname = current_database()"""
            ).fetchone()
        assert owner == identity.username
        systems[service] = system
    if nf_admin:
        assert systems["aw"] != systems["nf"]
    env = {**os.environ, "NF_DATABASE_URL": urls["nf"], "AW_DATABASE_URL": urls["aw"],
           "DUAL_FIXTURE_ROOT": str(root), "PROOF_AW_SYSTEM": systems["aw"],
           "PROOF_NF_SYSTEM": systems["nf"]}
    for key in ("AW_DUAL_TEST_ADMIN_URL", "AW_DUAL_TEST_NF_ADMIN_URL"):
        env.pop(key, None)
    subprocess.run(["bash", str(root / "aw/acceptance/dual_codex/run.sh"), "--broker-session",
                    str(root), sys.argv[2]], env=env, check=True)
PY
    exit 0
fi

r=$2
out=$3
suffix=$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')
role="awh$suffix"
nf="nfh$suffix"
aw="awb$suffix"
admin="$r/private/admin"
created=()
made_runtime_dir=false
cleanup() {
    if [[ -s "$admin/sshd.pid" ]]; then
        sudo -n kill "$(cat "$admin/sshd.pid")" 2>/dev/null || true
    fi
    for account in "${created[@]}"; do
        sudo -n pkill -KILL -u "$account" 2>/dev/null || true
        sudo -n userdel "$account" || true
    done
    if "$made_runtime_dir"; then sudo -n rmdir /run/sshd 2>/dev/null || true; fi
}
trap cleanup EXIT
if [[ ! -d /run/sshd ]]; then
    sudo -n install -d -m 0755 /run/sshd
    made_runtime_dir=true
fi
sudo -n install -d -m 0711 "$r/users" "$r/private"
sudo -n install -d -m 0755 "$r/public"
sudo -n install -d -o "$(id -un)" -m 0700 "$r/private/"{nf,aw,admin}
for account in "$role" "$nf" "$aw"; do
    sudo -n useradd --no-create-home --no-log-init --user-group --home-dir "$r/users/$account" --shell /bin/sh "$account"
    created+=("$account")
    sudo -n usermod --password '*' "$account"
    sudo -n install -d -o "$account" -g "$account" -m 0700 "$r/users/$account" "$r/users/$account/.ssh"
done
rh="$r/users/$role"
for name in host nf aw; do ssh-keygen -q -t ed25519 -N '' -f "$admin/$name"; done
sudo cp "$admin/nf.pub" "$r/public/nf.pub"
sudo cp "$admin/aw.pub" "$r/public/aw.pub"
sudo install -d -o "$role" -m 0700 "$rh/.codex" "$rh/work"
sudo install -o "$role" -m 0600 "$r/aw/adapters/codex/role-dual.example.toml" "$rh/.codex/config.toml"
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
printf 'proof-loopback %s\n' "$(cat "$admin/host.pub")" | sudo tee "$rh/.ssh/known_hosts" >/dev/null
for kind in nf aw; do
    acct="$nf"
    command="$r/nf/.venv/bin/python -m novel_factory.interfaces.mcp_stdio --character-only --binding $r/private/nf/role.json"
    if [ "$kind" = aw ]; then
        acct="$aw"
        command="$r/aw/.venv/bin/aw-mcp --binding $r/private/aw/employee.json"
    fi
    printf 'restrict,command="%s" %s\n' "$command" "$(cat "$admin/$kind.pub")" | sudo tee "$r/users/$acct/.ssh/authorized_keys" >/dev/null
    cat <<EOF | sudo tee -a "$rh/.ssh/config" >/dev/null
Host $kind-role-service
    HostName 127.0.0.1
    Port $port
    User $acct
    IdentityAgent $rh/.ssh/agent.sock
    IdentitiesOnly yes
    IdentityFile $r/public/$kind.pub
    UserKnownHostsFile $rh/.ssh/known_hosts
    HostKeyAlias proof-loopback
    StrictHostKeyChecking yes
    ForwardAgent no
    BatchMode yes
EOF
done
for acct in "$nf" "$aw"; do
    sudo chown "$acct:$acct" "$r/users/$acct/.ssh/authorized_keys"
    sudo chmod 0600 "$r/users/$acct/.ssh/authorized_keys"
done
sudo chown "$role:$role" "$rh/.ssh/config" "$rh/.ssh/known_hosts"
sudo chmod 0600 "$rh/.ssh/config" "$rh/.ssh/known_hosts"
sudo -u "$role" ssh-agent -a "$rh/.ssh/agent.sock" -s >/dev/null
sudo env SSH_AUTH_SOCK="$rh/.ssh/agent.sock" ssh-add "$admin/nf" "$admin/aw" >/dev/null
cat > "$admin/sshd.conf" <<EOF
Port $port
ListenAddress 127.0.0.1
HostKey $admin/host
AuthorizedKeysFile .ssh/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
AllowUsers $nf $aw
AllowAgentForwarding no
AllowTcpForwarding no
X11Forwarding no
PermitTTY no
PermitTunnel no
PermitUserEnvironment no
UsePAM no
PidFile $admin/sshd.pid
LogLevel ERROR
EOF
sudo /usr/sbin/sshd -t -f "$admin/sshd.conf"
sudo /usr/sbin/sshd -f "$admin/sshd.conf"
export PYTHONPATH="$r/nf:$r/nf/src:$r/aw/src"
"$r/nf/.venv/bin/python" "$r/aw/acceptance/dual_codex/fixture.py"
sudo chown -R "$nf:$nf" "$r/private/nf"
sudo chown -R "$aw:$aw" "$r/private/aw"
sudo -u "$role" test ! -r "$admin/nf"
sudo -u "$role" test ! -r "$admin/aw"
sudo -u "$nf" test ! -r "$r/private/aw/employee.json"
sudo -u "$aw" test ! -r "$r/private/nf/role.json"
for host in nf aw; do
    answer=$(sudo -u "$role" -H ssh -T "$host-role-service" 'printf remote-shell-is-not-permitted' </dev/null)
    test "$answer" != remote-shell-is-not-permitted
done
public_probe="$r/aw/acceptance/dual_codex/probe.py"
sudo -u "$role" -H env -i HOME="$rh" USER="$role" LOGNAME="$role" PATH=/usr/bin:/bin LANG=C.UTF-8 \
    CODEX_HOME="$rh/.codex" PYTHONPATH="$r/nf/src" /bin/sh -c 'cd "$HOME/work" && exec "$@"' sh "$r/nf/.venv/bin/python" "$public_probe" --codex "$r/codex" --challenge "$r/challenge.json" > "$admin/measured.json"
python3 - "$admin/measured.json" "$out" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

receipt = json.loads(Path(sys.argv[1]).read_bytes())
assert receipt["model_turns"] == 0 and receipt["role_binding_reads_denied"] == 2
receipt.update(aw_postgres_system_identifier=os.environ["PROOF_AW_SYSTEM"],
               nf_postgres_system_identifier=os.environ["PROOF_NF_SYSTEM"],
               separate_service_clusters=os.environ["PROOF_AW_SYSTEM"] != os.environ["PROOF_NF_SYSTEM"],
               wheel_sha256=os.environ["PROOF_AW_WHEEL_SHA"],
               nf_sha=os.environ["PROOF_NF_SHA"], workshop_sha=os.environ["PROOF_AW_SHA"],
               transport="OpenSSH forced commands", separate_service_uids=True,
               role_signing_private_keys_unreadable=True, cross_service_binding_reads_denied=2)
with tempfile.NamedTemporaryFile("w", dir=sys.argv[2], delete=False) as f:
    json.dump(receipt, f, sort_keys=True)
    f.write("\n")
os.replace(f.name, Path(sys.argv[2]) / "receipt.json")
print(json.dumps(receipt, sort_keys=True))
PY
