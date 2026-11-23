# Human operator. Can manage secrets, policies and auth roles, but is not root:
# it cannot change seal configuration or generate a root token.
path "secret/*"                  { capabilities = ["create", "read", "update", "patch", "delete", "list"] }
path "sys/policies/acl/*"        { capabilities = ["create", "read", "update", "delete", "list"] }
path "sys/mounts"                { capabilities = ["read"] }
path "sys/mounts/*"              { capabilities = ["create", "read", "update", "delete", "list"] }
path "sys/auth"                  { capabilities = ["read"] }
path "auth/approle/role/*"       { capabilities = ["create", "read", "update", "delete", "list"] }
path "database/*"                { capabilities = ["create", "read", "update", "delete", "list"] }
path "sys/leases/*"              { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
path "auth/token/lookup-self"    { capabilities = ["read"] }
