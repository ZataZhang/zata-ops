ui           = true
api_addr     = "https://127.0.0.1:8200"
cluster_addr = "https://127.0.0.1:8201"

# Raft uses mmap. Compose prevents this container from using swap instead.
disable_mlock = true

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "127.0.0.1:8201"
  tls_cert_file   = "/openbao/tls/server.crt"
  tls_key_file    = "/openbao/tls/server.key"
  tls_min_version = "tls12"
}

storage "raft" {
  path                   = "/openbao/file"
  node_id                = "openbao-1"
  performance_multiplier = 1
}

audit "file" "deployment" {
  description = "Record requests and responses to the persistent audit volume."
  options {
    file_path = "/openbao/logs/audit.jsonl"
    log_raw   = "false"
    mode      = "0600"
  }
}
