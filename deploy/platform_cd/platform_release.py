#!/usr/bin/env python3
"""Forced-command CD endpoint. Local policy is root-owned; stdin is never shell code.

An image may replace one app service only. Databases, gateways, secrets, migration,
workers, DNS and KOTH source configuration are outside this endpoint's authority.
"""
import contextlib
import datetime
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time

try:
    from .ranking_scope import load_scope, image_override, check_compose_scope, inspect_running, ScopeError
except ImportError:
    from ranking_scope import load_scope, image_override, check_compose_scope, inspect_running, ScopeError

CONFIG=Path('/etc/ctf-platform-cd/config.json')
SHA=re.compile(r'[0-9a-f]{40}')
HASH=re.compile(r'[0-9a-f]{64}')
REPOS={'backend':'MSG-CTF/msg-backend','frontend':'MSG-CTF/front-team'}
MAX_ARCHIVE=2*1024**3


def validate_request(value,component):
    if not isinstance(value,dict) or set(value)!={'action','component','source_sha','archive_sha256','archive_bytes'}:
        raise ValueError('Invalid release schema')
    if value['component']!=component or component not in REPOS or value['action'] not in ['probe','deploy']:
        raise ValueError('Invalid release identity')
    if not isinstance(value['source_sha'],str) or not SHA.fullmatch(value['source_sha']):raise ValueError('Invalid source SHA')
    if not isinstance(value['archive_sha256'],str) or not HASH.fullmatch(value['archive_sha256']):raise ValueError('Invalid archive SHA')
    size=value['archive_bytes']
    if type(size) is not int or not 0<size<=MAX_ARCHIVE:raise ValueError('Invalid archive size')
    return value


def validate_archive(path,component,sha):
    """Accept exactly one Docker image, including containerd's OCI wrapper."""
    tag='ctf-platform-'+component+':'+sha
    with tarfile.open(path,'r:') as archive:
        members=[]
        for member in archive:
            if len(members)>=20000:raise ValueError('Too many archive entries')
            members.append(member)
        names=[m.name.rstrip('/') for m in members]
        if len(names)!=len(set(names)):raise ValueError('Duplicate archive entry')
        for member in members:
            name=PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsupported archive entry')
        def read(name,limit=2*1024**2):
            member=archive.getmember(name)
            if member.size>limit:raise ValueError('Oversized image metadata')
            return archive.extractfile(member).read()
        manifests=json.loads(read('manifest.json'))
        if not isinstance(manifests,list) or len(manifests)!=1:raise ValueError('Expected one image')
        manifest=manifests[0]
        if manifest.get('RepoTags')!=[tag]:raise ValueError('Unexpected repository tag')
        config_name=manifest['Config'];config_raw=read(config_name);config=json.loads(config_raw)
        labels=config.get('config',{}).get('Labels',{}) or {}
        if config.get('architecture')!='amd64' or config.get('os')!='linux':raise ValueError('Wrong platform')
        if labels.get('org.opencontainers.image.revision')!=sha or labels.get('org.opencontainers.image.source')!='https://github.com/'+REPOS[component]:
            raise ValueError('Image source identity differs')
        user=str(config.get('config',{}).get('User','')).split(':')[0]
        if user in ('','root') or (user.isdigit() and int(user)==0):raise ValueError('Root application image')
        allowed={'manifest.json',config_name,*manifest['Layers']}
        if 'index.json' in names:
            index=json.loads(read('index.json'))
            descriptors=index.get('manifests',[])
            if len(descriptors)!=1:raise ValueError('Unexpected OCI image count')
            descriptor=descriptors[0];digest=descriptor['digest']
            if not re.fullmatch(r'sha256:[0-9a-f]{64}',digest):raise ValueError('Invalid manifest digest')
            blob='blobs/sha256/'+digest[7:];raw=read(blob);oci=json.loads(raw)
            if hashlib.sha256(raw).hexdigest()!=digest[7:]:raise ValueError('Manifest digest differs')
            if oci['config']['digest']!='sha256:'+hashlib.sha256(config_raw).hexdigest():raise ValueError('OCI configuration differs')
            if ['blobs/sha256/'+x['digest'][7:] for x in oci['layers']]!=manifest['Layers']:raise ValueError('OCI layers differ')
            for key in ['io.containerd.image.name','org.opencontainers.image.ref.name']:
                value=descriptor.get('annotations',{}).get(key)
                expected='docker.io/library/'+tag if key=='io.containerd.image.name' else sha
                if value is not None and value!=expected:raise ValueError('Unexpected OCI tag')
            allowed.update(['index.json','oci-layout',blob])
        for member in members:
            if member.isdir():continue
            if member.name not in allowed:raise ValueError('Unreferenced archive object')
            if member.name.startswith('blobs/sha256/'):
                actual=hashlib.file_digest(archive.extractfile(member),'sha256').hexdigest()
                if actual!=member.name.rsplit('/',1)[-1]:raise ValueError('Image blob differs')
    return tag


def atomic(path,data):
    pending=path.with_name(path.name+'.next')
    with pending.open('w') as f:
        os.fchmod(f.fileno(),0o600);json.dump(data,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(pending,path)


def main():
    if os.geteuid()!=0 or len(sys.argv)!=2:raise RuntimeError('Restricted entry point required')
    os.umask(0o077)
    component=sys.argv[1]
    policy=json.loads(CONFIG.read_text())
    if socket.gethostname().split('.')[0]!=policy['vm']:raise RuntimeError('Host differs')
    line=sys.stdin.buffer.readline(4097)
    if len(line)>4096 or not line.endswith(b'\n'):raise ValueError('Invalid request header')
    request=validate_request(json.loads(line),component)
    root=Path(policy['root'])/'cd';root.mkdir(mode=0o700,exist_ok=True)
    lock=(root/'deployment.lock').open('a')
    try:
        deadline=time.monotonic()+600
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>deadline:raise RuntimeError('Another app deployment is running')
                time.sleep(1)
        return release_locked(policy,request,component,root)
    finally:
        lock.close()


def release_locked(policy,request,component,root):
    # A read-only probe does not deploy its caller's source revision.
    if request['action']=='deploy':
        upstream=subprocess.run(['git','ls-remote','https://github.com/'+REPOS[component]+'.git','refs/heads/main'],capture_output=True,timeout=45,check=True)
        main_sha=upstream.stdout.decode().split()[0]
        if not SHA.fullmatch(main_sha):raise RuntimeError('Invalid upstream revision')
        if main_sha!=request['source_sha']:
            print(json.dumps({'status':'SUPERSEDED','component':component,'source_sha':request['source_sha'],'current_main_sha':main_sha}));return
    for name,digest in policy['base_file_sha256'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Deployment template changed; operator review required')
    scope=load_scope(policy)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    work=Path(tempfile.mkdtemp(prefix=stamp+'-'+component+'-'+request['source_sha'][:8]+'-',dir=root))
    def run(command,name,timeout=180,environment=None):
        result=subprocess.run(command,capture_output=True,timeout=timeout,env=environment)
        # Docker inspect contains effective environment secrets. Never log it.
        if not (len(command)>1 and command[0]=='docker' and ('inspect' in command or ('config' in command and '--format' in command))):
            (work/(name+'.stdout')).write_bytes(result.stdout)
            (work/(name+'.stderr')).write_bytes(result.stderr)
        if result.returncode:raise RuntimeError(name+' failed; private host diagnostics retained')
        return result.stdout
    base=['docker','compose','--project-name',policy['compose_project'],'--env-file','/dev/null']
    environment=dict(os.environ)
    if policy.get('base_env_json'):environment.update(json.loads(Path(policy['base_env_json']).read_text()))
    if policy.get('base_env_file'):
        for line in Path(policy['base_env_file']).read_text().splitlines():
            if line and not line.startswith('#'):
                key,value=line.split('=',1);environment[key]=value
    for file in policy['base_files']:base+=['-f',file]
    services=policy['service_containers']
    old={name:json.loads(run(['docker','inspect',container],'inspect-'+name))[0]['Image'] for name,container in services.items()}
    for image in old.values():run(['docker','image','inspect',image],'old-image-exists')
    override=work/'images.override.json'
    atomic(override,image_override(old,scope))
    def compose(*args):return run(base+['-f',str(override)]+list(args),'compose-'+str(time.monotonic_ns()),environment=environment,timeout=240)
    compose('config','--quiet')
    check_compose_scope(json.loads(compose('config','--format','json')),scope)
    if request['action']=='probe':
        proof=inspect_running(services['backend'],scope,policy['domain'])
        print(json.dumps({'status':'PREFLIGHT_VERIFIED','track':policy['track'],'component':component,'source_sha':request['source_sha'],
                         'ranking_scope':proof,'migration_policy':'stop if pending; no automatic DDL'}));return
    if shutil.disk_usage(root).free<request['archive_bytes']*3+2*1024**3:raise RuntimeError('Insufficient safe image staging space')
    archive=work/'image.tar';remaining=request['archive_bytes'];hasher=hashlib.sha256()
    with archive.open('xb') as f:
        while remaining:
            block=sys.stdin.buffer.read(min(1024**2,remaining))
            if not block:raise RuntimeError('Truncated image archive')
            f.write(block);hasher.update(block);remaining-=len(block)
    if sys.stdin.buffer.read(1):raise ValueError('Trailing release payload')
    if hasher.hexdigest()!=request['archive_sha256']:raise ValueError('Release checksum differs')
    tag=validate_archive(archive,component,request['source_sha'])
    run(['docker','load','--input',str(archive)],'image-load',timeout=240)
    image=json.loads(run(['docker','image','inspect',tag],'new-image-inspect'))[0]
    if image['Architecture']!='amd64' or image['Config']['Labels']['org.opencontainers.image.revision']!=request['source_sha']:raise ValueError('Loaded image differs')
    new={**old,component:image['Id']}
    atomic(override,image_override(new,scope))
    check_compose_scope(json.loads(compose('config','--format','json')),scope)
    candidate='ctf-platform-cd-candidate-'+component
    def healthy(container,timeout=120):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            item=json.loads(run(['docker','inspect',container],'health-inspect'))[0]
            if item['State'].get('Health',{}).get('Status')=='healthy':return
            if not item['State']['Running']:raise RuntimeError('Candidate stopped')
            time.sleep(2)
        raise RuntimeError('Container health timed out')
    def https_health():
        conn=http.client.HTTPSConnection(policy['domain'],timeout=15,context=ssl.create_default_context())
        conn._create_connection=lambda unused,timeout,source_address=None:socket.create_connection(('127.0.0.1',443),timeout)
        conn.request('GET','/healthz');r=conn.getresponse();data=json.loads(r.read(16384));conn.close()
        if r.status!=200 or data!={'status':'ok','database':'ok','cache':'ok'}:raise RuntimeError('Application TLS/database/cache health failed')
    if component=='backend':
        compose('run','--rm','-T','--no-deps','--pull','never','-e','PGOPTIONS=-c default_transaction_read_only=on','backend','python','manage.py','migrate','--check')
        compose('run','--rm','-T','--no-deps','--pull','never','-e','PGOPTIONS=-c default_transaction_read_only=on','backend','python','manage.py','check','--deploy','--fail-level','WARNING')
    if subprocess.run(['docker','inspect',candidate],capture_output=True).returncode==0:raise RuntimeError('Previous candidate exists; operator inspection required')
    switched=False
    try:
        compose('run','--detach','--no-deps','--pull','never','--name',candidate,component)
        inspected=json.loads(run(['docker','inspect',candidate],'candidate-inspect'))[0]
        if any(component in (v.get('Aliases') or []) for v in inspected['NetworkSettings']['Networks'].values()):raise RuntimeError('Candidate acquired live service alias')
        if inspected['HostConfig']['PortBindings']:raise RuntimeError('Candidate published an unexpected host port')
        healthy(candidate)
        if component=='backend':inspect_running(candidate,scope,policy['domain'],mode='loopback')
        run(['docker','rm','-f',candidate],'remove-verified-candidate')
        latest=run(['git','ls-remote','https://github.com/'+REPOS[component]+'.git','refs/heads/main'],'main-before-promotion',timeout=45).decode().split()[0]
        if latest!=request['source_sha']:
            archive.unlink()
            print(json.dumps({'status':'SUPERSEDED','component':component,'source_sha':request['source_sha'],'current_main_sha':latest}));return
        # No migration ran, so the previous app remains compatible for rollback.
        switched=True
        compose('up','-d','--no-deps','--pull','never','--wait','--wait-timeout','120',component)
        https_health()
        actual=json.loads(run(['docker','inspect',services[component]],'active-image-inspect'))[0]
        if actual['Image']!=image['Id']:raise RuntimeError('Active image differs')
        proof=inspect_running(services['backend'],scope,policy['domain'])
        result={'status':'VERIFIED','track':policy['track'],'component':component,'source_sha':request['source_sha'],
                'image_id':image['Id'],'previous_image_id':old[component],'migration_applied':False,'DNS_changed':False,'ranking_scope':proof,
                'at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
        atomic(root/(component+'-current.json'),result);atomic(work/'result.json',result)
        archive.unlink();print(json.dumps(result))
    except Exception:
        if component=='frontend':
            diagnostic=subprocess.run(['docker','logs',candidate],capture_output=True,timeout=20)
            (work/'frontend-startup-failure.log').write_bytes((diagnostic.stdout+diagnostic.stderr)[-16384:])
        subprocess.run(['docker','rm','-f',candidate],capture_output=True,timeout=30)
        if switched:
            atomic(override,image_override(old,scope))
            compose('up','-d','--no-deps','--pull','never','--wait','--wait-timeout','120',component);https_health()
            inspect_running(services['backend'],scope,policy['domain'])
            atomic(work/'rollback.json',{'status':'PREVIOUS_APP_RESTORED','database_migration_ran':False})
        raise


if __name__=='__main__':
    try:main()
    except Exception as error:
        import traceback
        message=str(error)
        code='RANKING_SCOPE_FAILED' if isinstance(error,ScopeError) else 'UNVERIFIED_RELEASE'
        if message=='Deployment template changed; operator review required':code='BASE_TEMPLATE_CHANGED'
        if message.startswith('compose-') and 'failed' in message:code='MIGRATION_OR_SECURITY_CHECK_FAILED'
        diagnostic={'error_type':type(error).__name__,'error_code':code,'locations':[{'file':Path(x.filename).name,'line':x.lineno} for x in traceback.extract_tb(error.__traceback__)]}
        try:
            policy=json.loads(CONFIG.read_text())
            path=Path(policy['root'])/'cd/last-endpoint-error.json'
            path.write_text(json.dumps(diagnostic));path.chmod(0o600)
        except OSError:pass
        # Tracebacks may include secret-containing compose arguments. Withhold them.
        print('PLATFORM_DEPLOY_ERROR='+json.dumps({'error_code':code}),file=sys.stderr)
        raise SystemExit('Platform release rejected; inspect root-only host diagnostics') from None
