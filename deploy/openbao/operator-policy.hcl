# Trusted human operator who manages the KV mount and provisions application roles.
path "kv/*" {
  capabilities = ["create", "read", "update", "patch", "delete", "list"]
}

path "sys/policies/acl" {
  capabilities = ["list"]
}

path "sys/policies/acl/app-*" {
  capabilities = ["create", "read", "update", "delete"]
}

path "auth/approle/role" {
  capabilities = ["list"]
}

path "auth/approle/role/app-*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "sys/storage/raft/snapshot" {
  capabilities = ["read", "sudo"]
}
