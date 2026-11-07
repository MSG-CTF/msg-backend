#!/usr/bin/env python3
"""Keep existing repository SSH protocols; deploy only to the configured new VM."""
import fcntl,hashlib,json,os,re,shlex,shutil,subprocess,sys,time
from pathlib import Path
try:
 from .ranking_scope import load_scope,validate_proof,ScopeError
except ImportError:
 from ranking_scope import load_scope,validate_proof,ScopeError

ROOT=Path('/usr/local/lib/ctf-platform-cd')
POLICY=Path('/etc/ctf-platform-cd/config.json')
BACKEND_PIN='14fafb5620ca6f7eabf26ff8f920e7afd9d0aa08'
FRONTEND_PIN='40d6a567b35955471d2ce2b556340451a6b8e647'
REPOS={'backend':'MSG-CTF/msg-backend','frontend':'MSG-CTF/front-team'}
ERROR_CODES={'BASE_TEMPLATE_CHANGED','RANKING_SCOPE_FAILED','HELPER_CHANGED','MIGRATION_OR_SECURITY_CHECK_FAILED','UNVERIFIED_RELEASE'}

class ReleaseError(RuntimeError):
 def __init__(self,code):self.code=code if code in ERROR_CODES else 'UNVERIFIED_RELEASE';super().__init__(self.code)

def receipt(request,status,proof=None,current_main=None):
 value={'source_commit':request['source_sha'],'pipeline_revision':request['pipeline_revision'],
  'deployment_strategy':'verified_external_platform_image_update','status':status}
 if proof is not None:value['ranking_scope']=proof
 if current_main is not None:value['current_main_source_commit']=current_main
 return value

def emit(component,value):
 print(('BACKEND_DEPLOYMENT_RESULT=' if component=='backend' else '')+json.dumps(value))

def parse(component,original,raw=None):
 if component=='backend':
  if original!='deploy':raise ValueError('Unexpected SSH command')
  if raw is None or len(raw)>4096:raise ValueError('Invalid input')
  value=json.loads(raw)
  if set(value)!= {'action','source_sha','deployment_id','pipeline_revision'} or value['pipeline_revision']!=BACKEND_PIN:raise ValueError('Unapproved backend caller')
 elif component=='frontend':
  args=original.split()
  if len(args)!=4 or args[0] not in ['deploy-frontend','probe-frontend'] or args[3]!=FRONTEND_PIN:raise ValueError('Unapproved frontend caller')
  value={'action':'probe' if args[0]=='probe-frontend' else 'deploy','source_sha':args[1],'deployment_id':args[2],'pipeline_revision':args[3]}
 else:raise ValueError('Unknown component')
 if value['action'] not in ['probe','deploy'] or not re.fullmatch('[0-9a-f]{40}',value['source_sha']) or not re.fullmatch('[0-9]{1,20}-[0-9]{1,5}',value['deployment_id']):raise ValueError('Invalid identity')
 return value

def main():
 if os.geteuid()!=0 or len(sys.argv)!=3:raise RuntimeError('Fixed root entry point required')
 os.umask(0o077)
 component=sys.argv[1];request=parse(component,sys.argv[2],sys.stdin.buffer.read(4097) if component=='backend' else None)
 policy=json.loads(POLICY.read_text());root=Path(policy['root'])/'cd';root.mkdir(mode=0o700,exist_ok=True)
 for name,digest in policy['trusted_helper_sha256'].items():
  path=ROOT/name;meta=path.lstat()
  if meta.st_uid!=0 or meta.st_mode&0o022 or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ReleaseError('HELPER_CHANGED')
 scope=load_scope(policy)
 if request['action']=='deploy' and (root/'maintenance').exists():raise RuntimeError('Deployment maintenance is active')
 lock=(root/'build.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX)
 request_sha=request['source_sha'];tag='ctf-platform-'+component+':'+request_sha
 run=root/('build-'+component+'-'+request['deployment_id']);run.mkdir(mode=0o700,exist_ok=request['action']=='probe')
 if run.is_symlink() or run.resolve()!=run or run.stat().st_uid!=0 or run.stat().st_mode&0o077:raise RuntimeError('Unsafe private release directory')
 environment={'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/root','LANG':'C.UTF-8','GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1'}
 def execute(args,name,timeout=180):
  r=subprocess.run(args,capture_output=True,env=environment,timeout=timeout)
  # No environment, keys or full Docker inspections are logged here.
  (run/(name+'.log')).write_bytes(r.stdout+r.stderr)
  if r.returncode:raise RuntimeError(name+' failed; protected host diagnostics retained')
  return r.stdout
 if request['action']=='deploy':
  current=execute(['git','ls-remote','https://github.com/'+REPOS[component]+'.git','refs/heads/main'],'main-check',45).decode().split()[0]
  if current!=request_sha:emit(component,receipt(request,'superseded',current_main=current));return
 packet={'action':request['action'],'component':component,'source_sha':request_sha,'archive_sha256':'0'*64,'archive_bytes':1}
 def endpoint(payload,archive=None):
  process=subprocess.Popen(['python3',str(ROOT/'platform_release.py'),component],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment)
  try:
   try:
    process.stdin.write((json.dumps(payload)+'\n').encode())
    if archive:
     with archive.open('rb') as f:shutil.copyfileobj(f,process.stdin,1024**2)
   except BrokenPipeError:pass
   try:process.stdin.close()
   except BrokenPipeError:pass
   out=process.stdout.read();err=process.stderr.read();returncode=process.wait(timeout=600)
  except BaseException:process.kill();process.wait();raise
  (run/('endpoint-'+payload['action']+'-result.json')).write_bytes(out)
  if returncode:
   for line in err.decode('utf-8','replace').splitlines():
    if line.startswith('PLATFORM_DEPLOY_ERROR='):
     try:raise ReleaseError(json.loads(line.split('=',1)[1]).get('error_code'))
     except (ValueError,TypeError,AttributeError):pass
   raise ReleaseError('UNVERIFIED_RELEASE')
  result=json.loads(out)
  if result.get('source_sha')!=request_sha:raise RuntimeError('Release result differs')
  return result
 # Validate templates, persisted roster and actual public APIs before a costly build.
 preflight=endpoint({**packet,'action':'probe'})
 if preflight.get('status')!='PREFLIGHT_VERIFIED':raise RuntimeError('Preflight result differs')
 proof=validate_proof(preflight.get('ranking_scope'),scope)
 if request['action']=='probe':emit(component,receipt(request,'preflight_verified',proof));return
 archive=None
 if request['action']=='deploy':
  if shutil.disk_usage(root).free<6*1024**3:raise RuntimeError('Safe build space unavailable')
  source=run/'source';source.mkdir()
  execute(['git','init',str(source)],'git-init')
  execute(['git','-C',str(source),'remote','add','origin','https://github.com/'+REPOS[component]+'.git'],'git-origin')
  execute(['git','-C',str(source),'fetch','--depth=1','origin',request_sha],'git-fetch',120)
  execute(['git','-C',str(source),'checkout','--detach','FETCH_HEAD'],'git-checkout')
  assert execute(['git','-C',str(source),'rev-parse','HEAD'],'git-sha').decode().strip()==request_sha
  # The root-only release directory protects host access. Inside the image,
  # public Git files must retain readable modes for the non-root runtime.
  for current,dirs,names in os.walk(source):
   dirs[:]=[d for d in dirs if d!='.git'];Path(current).chmod(0o755)
   for name in names:
    path=Path(current)/name
    if path.is_file() and not path.is_symlink():path.chmod(0o755 if path.stat().st_mode&0o111 else 0o644)
  recipe=source/'Dockerfile'
  build=['docker','buildx','build','--builder','ctf-external-cd','--load','--platform','linux/amd64','--provenance=false',
   '--tag',tag,'--label','org.opencontainers.image.revision='+request_sha,'--label','org.opencontainers.image.source=https://github.com/'+REPOS[component]]
  if component=='frontend':
   recipe=ROOT/'Dockerfile.frontend-node24'
   contracts=run/'contracts';contracts.mkdir()
   for destination,original in [('Dockerfile',source/'Dockerfile'),('ci.yml',source/'.github/workflows/ci.yml')]:shutil.copyfile(original,contracts/destination)
   build+=['--build-context','contracts='+str(contracts),'--build-arg','VITE_PUBLIC_SITE_URL=https://'+policy['domain']]
  execute(build+['--file',str(recipe),str(source)],'image-build',900)
  archive=run/'image.tar';execute(['docker','save','--platform','linux/amd64','--output',str(archive),tag],'image-save',180)
  # Public application image; parent remains root-only. The scanner receives
  # only this read-only file, never the Docker socket or runtime credentials.
  archive.chmod(0o644)
  execute(['docker','run','--rm','--pull','never','--user','1001:1001','--cap-drop','ALL','--security-opt','no-new-privileges:true','--read-only','--memory','1g','--cpus','1','--pids-limit','128',
   '--tmpfs','/scanner-tmp:uid=1001,gid=1001,size=512m','--env','TMPDIR=/scanner-tmp','--mount','type=bind,src='+str(archive)+',dst=/scan/image.tar,readonly',
   '--mount','type=bind,src=/var/cache/ctf-platform-trivy,dst=/cache',policy['scanner_image'],
   'image','--input','/scan/image.tar','--cache-dir','/cache','--scanners','vuln','--severity','HIGH,CRITICAL','--ignore-unfixed=false','--ignorefile','/dev/null','--exit-code','1','--no-progress'],'image-scan',600)
  with archive.open('rb') as f:packet['archive_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
  packet['archive_bytes']=archive.stat().st_size
 result=endpoint(packet,archive)
 if archive:archive.unlink()
 if result.get('status')=='SUPERSEDED':
  current=result.get('current_main_sha','')
  if not re.fullmatch('[0-9a-f]{40}',current) or current==request_sha:raise RuntimeError('Superseded proof differs')
  emit(component,receipt(request,'superseded',current_main=current));return
 if result.get('status')!='VERIFIED':raise RuntimeError('Release result differs')
 proof=validate_proof(result.get('ranking_scope'),scope)
 emit(component,receipt(request,'verified',proof))

if __name__=='__main__':
 try:main()
 except Exception as error:
  import traceback
  code='RANKING_SCOPE_FAILED' if isinstance(error,ScopeError) else getattr(error,'code','UNVERIFIED_RELEASE')
  diagnostic={'error_type':type(error).__name__,'error_code':code,'locations':[{'file':Path(x.filename).name,'line':x.lineno} for x in traceback.extract_tb(error.__traceback__)]}
  try:
   path=Path('/opt/ctf-external/cd/last-entry-error.json');path.write_text(json.dumps(diagnostic));path.chmod(0o600)
  except OSError:pass
  print('PLATFORM_DEPLOY_ERROR='+json.dumps({'error_code':code}),file=sys.stderr)
  raise SystemExit('Restricted platform deployment rejected; consult administrator diagnostics') from None
