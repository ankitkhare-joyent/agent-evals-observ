"""Dynamic secrets demo: OpenBao creates a short-lived, read-only Postgres user on demand.

    mise run demo-dynamic-creds        (needs the stack up: it uses Langfuse's Postgres as the target)

Static secrets (KV) are passwords someone created and must rotate. Dynamic secrets don't exist until
asked for: OpenBao logs into the database with a privileged account it alone knows, runs CREATE ROLE with
a random password and an expiry, hands it out with a *lease*, and DROPs the role when the lease ends
or is revoked. A leaked credential is useless an hour later, and every one is traceable to who asked.

Steps (all as the human `admin`, whose policy allows managing database/*):
  1. enable the database secrets engine at database/
  2. configure a connection to Langfuse's Postgres, using the postgres password from secret/langfuse/infra
  3. rotate that root credential, so from now on only OpenBao knows the privileged password
     (skipped here: Langfuse itself uses it, and this is a demo on a shared database)
  4. define a role: SQL that creates a read-only user valid for 1 hour
  5. read database/creds/analyst-readonly -> a fresh username/password + lease
  6. prove it works, then revoke the lease and prove the user is gone
"""
import subprocess

import hvac

from observ.secrets import KV_MOUNT, admin_client

ROLE_SQL = [
    "CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}';",
    "GRANT pg_read_all_data TO \"{{name}}\";",
]


def psql(user: str, password: str, sql: str) -> str:
    r = subprocess.run(["docker", "compose", "exec", "-T", "-e", f"PGPASSWORD={password}", "postgres",
                        "psql", "-h", "127.0.0.1", "-U", user, "-d", "postgres", "-tAc", sql],
                       capture_output=True, text=True, check=False)
    return (r.stdout or r.stderr).strip()


def main() -> None:
    c = admin_client()
    if "database/" not in c.sys.list_mounted_secrets_engines()["data"]:
        c.sys.enable_secrets_engine("database", path="database")
        print("enabled database secrets engine at database/")
    pg_password = c.secrets.kv.v2.read_secret_version(path="langfuse/infra", mount_point=KV_MOUNT,
                                                      raise_on_deleted_version=True)["data"]["data"]["postgres_password"]
    c.secrets.database.configure(
        name="langfuse-postgres", plugin_name="postgresql-database-plugin",
        connection_url="postgresql://{{username}}:{{password}}@postgres:5432/postgres?sslmode=disable",
        username="postgres", password=pg_password, allowed_roles=["analyst-readonly"])
    c.secrets.database.create_role(name="analyst-readonly", db_name="langfuse-postgres",
                                   creation_statements=ROLE_SQL, default_ttl="1h", max_ttl="4h")
    print("role analyst-readonly: read-only, 1h TTL")

    creds = c.secrets.database.generate_credentials(name="analyst-readonly")
    user, password, lease = creds["data"]["username"], creds["data"]["password"], creds["lease_id"]
    print(f"issued user {user} (lease {lease.split('/')[-1][:8]}..., ttl {creds['lease_duration']}s)")
    print("  can read:", psql(user, password, "select count(*) || ' prompts' from prompts"))
    print("  can't write:", psql(user, password, "create table nope(x int)")[:80])

    c.sys.revoke_lease(lease)
    print("lease revoked ->", psql(user, password, "select 1")[:80])
    try:
        c.sys.read_lease(lease)
    except hvac.exceptions.InvalidRequest:
        print("lease no longer exists in OpenBao")
    c.logout()


if __name__ == "__main__":
    main()
