"""Pinned site rosters and read-only checks for managed app deployments."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid


SITES = {"external": ("EXTERNAL", 50, "msgctf.kr"), "internal": ("INTERNAL", 25, "internal.msgctf.kr")}
HASH = re.compile(r"[0-9a-f]{64}")
PREFIX = "RANKING_SCOPE_RESULT="


class ScopeError(RuntimeError):
    pass


def roster_digest(ids):
    return hashlib.sha256(json.dumps(sorted(ids), separators=(",", ":")).encode()).hexdigest()


def validate_scope(value, track):
    if track not in SITES or not isinstance(value, dict):
        raise ScopeError("Unknown site scope")
    if set(value) != {"schema_version", "track", "division", "team_ids", "source_workbook_sha256"}:
        raise ScopeError("Unexpected roster fields")
    division, count, _ = SITES[track]
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ScopeError("Unsupported roster version")
    if value["track"] != track or value["division"] != division:
        raise ScopeError("Roster belongs to another site")
    ids = value["team_ids"]
    if not isinstance(ids, list) or len(ids) != count or any(not isinstance(item, str) for item in ids):
        raise ScopeError("Official roster count differs")
    try:
        normalized = [str(uuid.UUID(item)) for item in ids]
    except (ValueError, TypeError, AttributeError):
        raise ScopeError("Invalid roster UUID") from None
    if ids != normalized or len(set(ids)) != count:
        raise ScopeError("Roster UUIDs must be canonical and unique")
    if not isinstance(value["source_workbook_sha256"], str) or not HASH.fullmatch(value["source_workbook_sha256"]):
        raise ScopeError("Invalid source workbook identity")
    return {**value, "team_ids": sorted(ids)}


def load_scope(policy, owner=0):
    path = Path(policy["ranking_scope_file"])
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != owner or metadata.st_mode & 0o022:
        raise ScopeError("Untrusted roster file")
    if path.resolve() != path or path.parent.stat().st_uid != owner or path.parent.stat().st_mode & 0o022:
        raise ScopeError("Untrusted roster directory")
    raw = path.read_bytes()
    if not HASH.fullmatch(policy.get("ranking_scope_sha256", "")) or hashlib.sha256(raw).hexdigest() != policy["ranking_scope_sha256"]:
        raise ScopeError("Roster checksum changed")
    return validate_scope(json.loads(raw), policy["track"])


def scope_environment(scope):
    return {"PUBLIC_RANKING_DIVISION": scope["division"], "PUBLIC_RANKING_TEAM_IDS": ",".join(scope["team_ids"])}


def image_override(images, scope):
    services = {name: {"image": value} for name, value in images.items()}
    if "backend" not in services:
        raise ScopeError("Managed backend is missing")
    services["backend"]["environment"] = scope_environment(scope)
    return {"services": services}


def check_compose_scope(model, scope):
    actual = model["services"]["backend"].get("environment", {})
    if any(actual.get(key) != value for key, value in scope_environment(scope).items()):
        raise ScopeError("Effective Compose roster differs")


def validate_proof(proof, scope):
    if not isinstance(proof, dict):
        raise ScopeError("Ranking scope proof differs")
    banned = proof.get("banned_official_team_ids", [])
    if (not isinstance(banned, list) or any(not isinstance(value, str) for value in banned)
            or len(set(banned)) != len(banned) or not set(banned).issubset(scope["team_ids"])):
        raise ScopeError("Invalid banned official team receipt")
    eligible = sorted(set(scope["team_ids"]) - set(banned))
    if not eligible:
        raise ScopeError("No eligible official team is available for API verification")
    official_digest = roster_digest(scope["team_ids"])
    if banned and proof.get("official_roster_sha256") != official_digest:
        raise ScopeError("Official roster identity differs")
    if ("official_roster_sha256" in proof and proof["official_roster_sha256"] != official_digest
            or type(proof.get("team_count")) is not int
            or type(proof.get("configured_roster_count")) is not int):
        raise ScopeError("Invalid official roster receipt")
    expected = {"track": scope["track"], "division": scope["division"], "team_count": len(eligible),
                "configured_roster_count": len(scope["team_ids"]), "roster_sha256": roster_digest(eligible),
                "api_verified": True, "database_probe_read_only": True}
    if any(proof.get(key) != value for key, value in expected.items()):
        raise ScopeError("Ranking scope proof differs")
    checked = proof.get("koth_leaderboards_checked")
    if proof.get("api_verified") is not True or proof.get("database_probe_read_only") is not True or type(checked) is not int or not 0 <= checked <= 30:
        raise ScopeError("Invalid ranking verification receipt")
    return {**expected, "koth_leaderboards_checked": checked,
            "official_roster_sha256": official_digest, "banned_official_team_ids": sorted(banned)}


PROGRAM = r'''
import ast,hashlib,http.client,json,os,socket,ssl,uuid
from pathlib import Path
packet=PACKET
stage='django_setup'
try:
 os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
 import django
 django.setup()
 from django.conf import settings
 from django.db import connection,transaction
 from apps.accounts.models import Team,User
 from apps.ranking.population import public_ranking_teams
 from apps.common.jwt import issue_access_token
 from apps.ranking import views as ranking_views
 from apps.leaderboard import views as graph_views
 from apps.koth import views as koth_views
 scope=packet['scope'];official=sorted(scope['team_ids'])
 def digest(ids):return hashlib.sha256(json.dumps(sorted(ids),separators=(',',':')).encode()).hexdigest()
 stage='readonly_roster_query'
 with transaction.atomic():
  with connection.cursor() as cursor:
   cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
   cursor.execute('SHOW transaction_read_only');assert cursor.fetchone()[0]=='on'
  stage='configured_roster'
  configured=sorted(str(uuid.UUID(str(value))) for value in settings.PUBLIC_RANKING_TEAM_IDS)
  assert settings.PUBLIC_RANKING_DIVISION==scope['division'] and configured==official
  stage='official_ban_state'
  banned=sorted(str(value) for value in Team.objects.filter(pk__in=official,is_banned=True).values_list('pk',flat=True))
  expected=sorted(set(official)-set(banned))
  assert expected
  stage='database_roster'
  actual=sorted(str(value) for value in public_ranking_teams().values_list('pk',flat=True))
  assert actual==expected
  stage='participant_identity'
  user=User.objects.filter(team_id__in=expected,role='PARTICIPANT').order_by('pk').first()
  assert user is not None
 stage='read_path_source'
 def scoped(node):return any(isinstance(row,ast.Call) and isinstance(row.func,ast.Name) and row.func.id=='public_ranking_teams' for row in ast.walk(node))
 tree=ast.parse(Path(koth_views.__file__).read_text());functions={row.name:row for row in tree.body if isinstance(row,ast.FunctionDef)}
 assert scoped(functions['leaderboard']) and scoped(functions['me']) and not scoped(functions['internal_teams'])
 assert scoped(ast.parse(Path(ranking_views.__file__).read_text())) and scoped(ast.parse(Path(graph_views.__file__).read_text()))
 assert 'koth:leaderboard:json:v4:' in Path(koth_views.__file__).read_text()
 token=issue_access_token(user)
 def read(path):
  if packet['mode']=='public':
   conn=http.client.HTTPSConnection(packet['domain'],timeout=15,context=ssl.create_default_context())
   conn._create_connection=lambda unused,timeout,source_address=None:socket.create_connection((packet['gateway'],443),timeout)
  else:conn=http.client.HTTPConnection('127.0.0.1',packet['backend_port'],timeout=15)
  try:
   conn.request('GET','/api/v1/'+path,headers={'Authorization':'Bearer '+token,'Accept':'application/json',
    'Host':packet['domain'],'X-Forwarded-Proto':'https','User-Agent':'MSGCTF-Deployment-Scope-Guard/1'})
   response=conn.getresponse();value=json.loads(response.read(2*1024**2))
   assert response.status==200 and value.get('code')=='SUCCESS'
   return value['data']
  finally:conn.close()
 stage='ranking_api'
 data=read('ranking?size=100');assert data['total_count']==len(expected)
 assert sorted(row['team_id'] for row in data['rankings'])==expected
 graph=read('leaderboard');assert all(row['team_id'] in expected for row in graph['teams'])
 stage='koth_api'
 clubs=read('koth/clubs');mine=read('koth/me')
 challenges=[row for club in clubs['clubs'] for row in club['challenges']];assert len(challenges)<=30
 for challenge in challenges:
  cid=challenge['koth_challenge_id'];data=read('koth/leaderboard?koth_challenge_id='+cid)
  assert all(row['team_id'] in expected for row in data['leaderboard'])
  own=next((row for row in data['leaderboard'] if row['team_id']==str(user.team_id)),None)
  personal=next(row for row in mine['challenges'] if row['koth_challenge_id']==cid)
  assert personal['rank']==(own['rank'] if own else None)
 token=None
 result={'status':'VERIFIED','track':scope['track'],'division':scope['division'],'team_count':len(actual),
  'configured_roster_count':len(configured),'roster_sha256':digest(actual),'api_verified':True,
  'database_probe_read_only':True,'koth_leaderboards_checked':len(challenges),
  'official_roster_sha256':digest(official),'banned_official_team_ids':banned}
 print('RANKING_SCOPE_RESULT='+json.dumps(result))
except Exception as error:
 print('RANKING_SCOPE_RESULT='+json.dumps({'status':'FAILED','stage':stage,'error_type':type(error).__name__}))
 raise SystemExit(1)
'''


def inspect_running(container, scope, domain, mode="public", backend_port=8080, run=subprocess.run):
    if domain != SITES[scope["track"]][2] or mode not in ["public", "loopback"]:
        raise ScopeError("Unexpected scope probe destination")
    response = run(["docker", "inspect", container], capture_output=True, timeout=30)
    if response.returncode:
        raise ScopeError("Scope probe container is unavailable")
    live = json.loads(response.stdout)[0]
    if not live["State"]["Running"]:
        raise ScopeError("Scope probe container is stopped")
    gateways = [value.get("Gateway", "") for value in live["NetworkSettings"]["Networks"].values()]
    gateway = next((value for value in gateways if value and ipaddress.ip_address(value).is_private), None)
    if mode == "public" and gateway is None:
        raise ScopeError("Private TLS gateway is unavailable")
    packet = {"scope": scope, "domain": domain, "mode": mode, "gateway": gateway, "backend_port": backend_port}
    program = PROGRAM.replace("PACKET", repr(packet))
    result = run(["docker", "exec", "-i", "-e", "PGOPTIONS=-c default_transaction_read_only=on", container, "python", "-B", "-"],
                 input=program.encode(), capture_output=True, timeout=150)
    # Never forward child output: tokens exist only in the child's memory.
    lines = [line[len(PREFIX):] for line in result.stdout.decode("utf-8", "replace").splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1:
        raise ScopeError("Scope probe did not return one receipt")
    proof = json.loads(lines[0])
    if result.returncode or proof.get("status") != "VERIFIED":
        raise ScopeError("Ranking scope failed at " + str(proof.get("stage", "unknown")))
    return validate_proof(proof, scope)
