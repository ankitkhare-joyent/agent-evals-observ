"""Print the Langfuse UI login. Reads it from OpenBao as admin, so it is shown only in *your* terminal."""
from observ.secrets import KV_MOUNT, admin_client

client = admin_client()
infra = client.secrets.kv.v2.read_secret_version(path="langfuse/infra", mount_point=KV_MOUNT,
                                                 raise_on_deleted_version=True)["data"]["data"]
print("Langfuse: http://localhost:33000")
print("  email:    ankit@langfuse.local")
print(f"  password: {infra['init_user_password']}")
client.logout()
