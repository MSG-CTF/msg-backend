# Backend deployment through the existing broker

Both the read-only deployment preflight and main push deployment explicitly set
BACKEND_DEPLOY_TRANSPORT=broker. They require separate development environment
secrets BACKEND_DEPLOY_BROKER_SSH_KEY and BACKEND_DEPLOY_BROKER_KNOWN_HOSTS. Missing
broker credentials fail before any SSH connection; the workflow never falls back
to a direct external connection.

The fixed broker is 35.193.235.206. Its existing unprivileged forced-command helper
at /usr/local/lib/msgctf-deploy-broker/proxy.py connects only to 136.108.97.182:22.
For compatibility, the existing TCP-hop request remains proxy-frontend-v1; this
request does not grant frontend or backend application permissions. The backend
still authenticates its independent msg-backend-deploy key on the destination,
pins that server's existing host key and validates the same release identity,
approved protocol revision and official 50-team roster proof.

Register only the dedicated backend broker public key on the broker's existing
sla account with the same restrict and forced-command options as the reviewed
frontend hop. Preserve all existing administrator and frontend keys. This key
must not receive a shell, PTY, agent forwarding, SSH forwarding or another TCP
destination. Pin the already verified broker.mjsec.kr host key.

The runner deletes its temporary application and broker key files on success or
failure. Retain the encrypted environment secrets and restricted public key for
future deployments; remove local operator copies after verification. No firewall,
metrics exposure, participant account, migration or roster-policy change is needed.

The server scanner's 512MiB temporary filesystem matches the reviewed live fix
for vulnerability database downloads. Its total 1GiB memory limit, non-root user,
read-only root filesystem and HIGH/CRITICAL rejection remain unchanged.
