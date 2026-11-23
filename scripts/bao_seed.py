"""Seed OpenBao with every secret the stack needs, and hand each workload its AppRole credentials.

    uv run python -m scripts.bao_seed        (run by `mise run up`; idempotent)

What it does, as the human `admin` (never as root - root was revoked by self-initialization):
  1. Generates Langfuse's infrastructure secrets once (create-only, check-and-set=0), so restarting never
     rotates a database password out from under a running database.
  2. Stores the Langfuse project API key in two places with different policies: `langfuse/otlp` (Collector)
     and `langfuse/api` (eval runner).
  3. Stores the Anthropic key if ./.env has one.
  4. For each AppRole, writes role_id + a secret_id into .bao/approle/<role>/ for its sidecar or process.
No secret value is ever printed.
"""
import base64
import os
import secrets
import uuid

import hvac

from observ.secrets import BAO_DIR, KV_MOUNT, ROOT, admin_client, wait_until_ready

ROLES = ["langfuse-infra", "otel-collector", "eval-runner"]


def create_once(client: hvac.Client, path: str, data: dict) -> bool:
    """KV v2 check-and-set with cas=0 means "only if it doesn't exist yet". Returns True if created."""
    try:
        client.secrets.kv.v2.create_or_update_secret(path=path, secret=data, cas=0, mount_point=KV_MOUNT)
        return True
    except hvac.exceptions.InvalidRequest as e:
        if "check-and-set" in str(e):
            return False
        raise


def read_dotenv_value(name: str) -> str | None:
    env = ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        if line.startswith(f"{name}="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def main() -> None:
    wait_until_ready()
    client = admin_client()

    public_key = f"pk-lf-{uuid.uuid4()}"
    secret_key = f"sk-lf-{uuid.uuid4()}"
    infra = {
        # hex only: these end up inside URLs and shell env files, where special characters break things
        "postgres_password": secrets.token_hex(24),
        "clickhouse_password": secrets.token_hex(24),
        "redis_password": secrets.token_hex(24),
        "minio_password": secrets.token_hex(24),
        "salt": secrets.token_hex(32),
        "encryption_key": secrets.token_hex(32),      # Langfuse requires exactly 64 hex chars
        "nextauth_secret": base64.b64encode(os.urandom(32)).decode(),
        "init_user_password": secrets.token_hex(12),
        "project_public_key": public_key,
        "project_secret_key": secret_key,
    }
    if create_once(client, "langfuse/infra", infra):
        print("created secret/langfuse/infra")
    current = client.secrets.kv.v2.read_secret_version(path="langfuse/infra", mount_point=KV_MOUNT,
                                                       raise_on_deleted_version=True)["data"]["data"]
    api_key = {"public_key": current["project_public_key"], "secret_key": current["project_secret_key"],
               "host": "http://localhost:33000"}
    for path in ("langfuse/otlp", "langfuse/api"):
        if create_once(client, path, api_key):
            print(f"created secret/{path}")

    key = read_dotenv_value("ANTHROPIC_API_KEY")
    if key:
        client.secrets.kv.v2.create_or_update_secret(path="evals/anthropic", secret={"api_key": key},
                                                     mount_point=KV_MOUNT)
        print("stored secret/evals/anthropic from ./.env (value not shown)")
    else:
        print("no ANTHROPIC_API_KEY in ./.env yet: run `mise run import-key` when it is there")

    for role in ROLES:
        d = BAO_DIR / "approle" / role
        d.mkdir(parents=True, exist_ok=True)
        d.parent.chmod(0o755)
        d.chmod(0o755)
        role_id = client.auth.approle.read_role_id(role_name=role)["data"]["role_id"]
        (d / "role_id").write_text(role_id)
        if not (d / "secret_id").exists():
            secret_id = client.auth.approle.generate_secret_id(role_name=role)["data"]["secret_id"]
            (d / "secret_id").write_text(secret_id)
            print(f"issued AppRole secret_id for {role}")
        for f in d.iterdir():
            f.chmod(0o644)   # read by the sidecar container's uid; the .bao dir itself stays out of git
    client.logout()
    print("OpenBao seeded. Langfuse UI login: ankit@langfuse.local (password: `mise run langfuse-login`)")


if __name__ == "__main__":
    main()
