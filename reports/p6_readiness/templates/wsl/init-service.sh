#!/bin/sh
set -eu
case "$P6_SERVICE" in
  nf_service) db_name=novel_factory ;;
  aw_service) db_name=agent_workshop ;;
  *) exit 2 ;;
esac
export P6_SERVICE_PASSWORD="$(cat "/run/secrets/${P6_SERVICE}_password")"
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres -v role="$P6_SERVICE" -v db="$db_name" <<'SQL'
\getenv service_password P6_SERVICE_PASSWORD
CREATE ROLE :"role" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'service_password';
CREATE DATABASE :"db" OWNER :"role";
REVOKE CONNECT ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO :"role";
SQL
unset P6_SERVICE_PASSWORD
