#!/usr/bin/env python3
"""Recovery-host verifier: encrypted tar input, tmpfs restoration, metadata output."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


def execute(args, data=None, timeout=40):
    result=subprocess.run(args,input=data,capture_output=True,timeout=timeout)
    if result.returncode:
        errors=[line for line in result.stderr.decode(errors='replace').splitlines() if 'ERROR:' in line or 'FATAL:' in line]
        classification=re.sub(r'([\"\']).*?\1','<redacted>',errors[0])[:180] if errors else 'command failed'
        raise RuntimeError("Recovery verification failed: "+classification)
    return result.stdout


def restore_sql(compressed):
    container=execute(['docker','run','-d','--name','tc622-restore-'+uuid.uuid4().hex[:10],
        '--label','msg.audit=tc622-restore','--network','none','--memory','512m',
        '--tmpfs','/var/lib/postgresql/data:rw,size=268435456',
        '-e','POSTGRES_PASSWORD='+secrets.token_urlsafe(30),'-e','POSTGRES_DB=tc622_restore',
        'postgres:16.14'],timeout=120).decode().strip()
    try:
        for _ in range(100):
            process=subprocess.run(['docker','exec',container,'cat','/proc/1/comm'],capture_output=True)
            ready=subprocess.run(['docker','exec',container,'psql','-X','-A','-t','-U','postgres','tc622_restore','-c','SELECT 1'],capture_output=True)
            if process.stdout.strip()==b'postgres' and ready.returncode==0:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("Isolated recovery database did not become ready")
        sql=gzip.decompress(compressed)
        # Plain pg_dump includes ownership. Recreate service owner roles in this
        # network-isolated database rather than editing the backup's SQL/data.
        owners=set(re.findall(rb'^ALTER [^\n]+ OWNER TO ("(?:[^"]|"")*"|[A-Za-z_][A-Za-z_0-9$]*);$',sql,re.MULTILINE))
        for owner in owners:
            name=owner.decode()
            if name.startswith('"'):
                name=name[1:-1].replace('""','"')
            if name=='postgres':
                continue
            execute(['docker','exec',container,'createuser','-U','postgres','--no-login','--',name])
        execute(['docker','exec','-i',container,'psql','-X','-v','ON_ERROR_STOP=1','-U','postgres','tc622_restore'],sql,timeout=100)
        count=int(execute(['docker','exec',container,'psql','-X','-A','-t','-U','postgres','tc622_restore','-c','SELECT count(*) FROM challenge_runtime_secrets']).strip())
        ciphertexts=execute(['docker','exec',container,'psql','-X','-A','-t','-U','postgres','tc622_restore','-c','SELECT encrypted_value FROM challenge_runtime_secrets']).splitlines()
        from cryptography.fernet import Fernet, MultiFernet
        keys=[]
        for path in Path('/etc/credstore.encrypted').glob('msgctf-service-key-recovery*'):
            payload=json.loads(execute(['systemd-creds','decrypt','--name=service-recovery.json',str(path),'-']))
            keys.extend(payload['secrets']['RUNTIME_SECRET_ENCRYPTION_KEYS'].split(','))
        cipher=MultiFernet([Fernet(key.encode()) for key in dict.fromkeys(keys)])
        for ciphertext in ciphertexts:
            cipher.decrypt(ciphertext)
        return {'database_restore_succeeded':True,'runtime_secret_rows':count,
            'restored_runtime_rows_decrypted':len(ciphertexts),'plaintext_written_to_persistent_storage':False}
    finally:
        execute(['docker','rm','--force',container])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--restore',required=True)
    args=parser.parse_args()
    os.umask(0o077)
    identity=execute(['systemd-creds','decrypt','--name=backup.age','/etc/credstore.encrypted/msgctf-backup-recovery','-'])
    verified=[]
    restoration=None
    with tempfile.TemporaryDirectory(prefix='msgctf-recover-',dir='/run') as temporary:
        identity_path=Path(temporary)/'backup.age'
        identity_path.write_bytes(identity)
        with tarfile.open(fileobj=sys.stdin.buffer,mode='r|*') as bundle:
            members=iter(bundle)
            first=next(members)
            if first.name!='manifest.json' or first.size>2_000_000:
                raise RuntimeError('Invalid backup verification manifest')
            manifest=json.load(bundle.extractfile(first))
            for member in members:
                if not member.isfile() or member.name not in manifest or member.size>1_000_000_000:
                    raise RuntimeError('Unexpected encrypted bundle member')
                encrypted=bundle.extractfile(member).read()
                compressed=execute(['age','--decrypt','--identity',str(identity_path)],encrypted)
                if hashlib.sha256(compressed).hexdigest()!=manifest[member.name]['compressed_plaintext_sha256']:
                    raise RuntimeError('Recovered backup hash mismatch')
                verified.append(member.name)
                if member.name==args.restore:
                    restoration=restore_sql(compressed)
        if set(verified)!=set(manifest) or restoration is None:
            raise RuntimeError('Not every encrypted backup was verified and restored')
    print(json.dumps({'encrypted_backups_verified':len(verified),'verified_names':verified,
        'restore_backup':args.restore,'restoration':restoration,'private_key_on_backend':False}))


if __name__=='__main__':
    main()
