"""Move the Anthropic API key from ./.env into OpenBao (secret/evals/anthropic). The value is never printed.

    mise run import-key

./.env is only the *seed*: once the key is in OpenBao, the eval runner reads it from there with its own
AppRole, and you could delete it from .env. Rotating the key = put the new one in .env and re-run this.
"""
from observ.secrets import KV_MOUNT, admin_client, wait_until_ready
from scripts.bao_seed import read_dotenv_value


def main() -> None:
    key = read_dotenv_value("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is missing or empty in ./.env")
    if not key.startswith("sk-ant-"):
        raise SystemExit("ANTHROPIC_API_KEY in ./.env doesn't look like an Anthropic key (expected sk-ant-...)")
    wait_until_ready()
    client = admin_client()
    meta = client.secrets.kv.v2.create_or_update_secret(path="evals/anthropic", secret={"api_key": key},
                                                        mount_point=KV_MOUNT)
    print(f"stored secret/evals/anthropic version {meta['data']['version']} (value not shown)")
    client.logout()


if __name__ == "__main__":
    main()
