import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from platform_cd import ranking_scope as scope
import backend_release
import send_backend_release as sender


def roster(track="external"):
    division, count, _ = scope.SITES[track]
    return {"schema_version":1,"track":track,"division":division,
            "team_ids":[str(uuid.UUID(int=value)) for value in range(1,count+1)],"source_workbook_sha256":"a"*64}


def proof(track="external"):
    value=roster(track)
    return {"track":track,"division":value["division"],"team_count":len(value["team_ids"]),
            "configured_roster_count":len(value["team_ids"]),"roster_sha256":scope.roster_digest(value["team_ids"]),
            "api_verified":True,"database_probe_read_only":True,"koth_leaderboards_checked":6}


def banned_proof(track="internal", banned_count=1):
    value=roster(track)
    banned=value["team_ids"][:banned_count]
    return {**proof(track),"team_count":len(value["team_ids"])-banned_count,
            "roster_sha256":scope.roster_digest(value["team_ids"][banned_count:]),
            "official_roster_sha256":scope.roster_digest(value["team_ids"]),
            "banned_official_team_ids":banned}


class SiteScopeTests(unittest.TestCase):
    def test_banned_official_team_remains_in_pinned_roster_and_out_of_visible_population(self):
        for track,total in [("internal",25),("external",50)]:
            for banned_count in [1,2]:
                with self.subTest(track=track,banned_count=banned_count):
                    result=scope.validate_proof(banned_proof(track,banned_count),roster(track))
                    self.assertEqual(result["configured_roster_count"],total)
                    self.assertEqual(result["team_count"],total-banned_count)
                    self.assertEqual(result["official_roster_sha256"],scope.roster_digest(roster(track)["team_ids"]))
                    self.assertEqual(result["banned_official_team_ids"],roster(track)["team_ids"][:banned_count])

    def test_ban_receipt_cannot_hide_unrelated_omissions_or_include_an_outsider(self):
        valid=banned_proof()
        changes=[{"team_count":23},{"configured_roster_count":24},
                 {"roster_sha256":scope.roster_digest(roster("internal")["team_ids"][2:])},
                 {"official_roster_sha256":"b"*64},
                 {"banned_official_team_ids":[str(uuid.UUID(int=999))]},
                 {"banned_official_team_ids":valid["banned_official_team_ids"]*2},
                 {"banned_official_team_ids":"not-a-list"},
                 {"banned_official_team_ids":roster("internal")["team_ids"]},
                 {"team_count":True}, {"configured_roster_count":True}]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(scope.ScopeError):
                scope.validate_proof({**valid,**change},roster("internal"))
        without_digest={k:v for k,v in valid.items() if k!="official_roster_sha256"}
        with self.assertRaises(scope.ScopeError):scope.validate_proof(without_digest,roster("internal"))

    def test_missing_official_without_a_verified_ban_still_fails(self):
        with self.assertRaises(scope.ScopeError):
            scope.validate_proof({**proof("internal"),"team_count":24},roster("internal"))
    def test_each_site_has_its_exact_unique_official_population(self):
        for track,count in [("external",50),("internal",25)]:
            with self.subTest(track=track):
                self.assertEqual(len(scope.validate_scope(roster(track),track)["team_ids"]),count)
                self.assertEqual(scope.validate_proof(proof(track),roster(track))["team_count"],count)

    def test_other_site_count_duplicate_uuid_and_unapproved_fields_are_rejected(self):
        original=roster()
        variations=[{"division":"INTERNAL"},{"track":"internal"},{"team_ids":original["team_ids"][:-1]},
                    {"team_ids":[original["team_ids"][0]]*50},{"team_ids":["not-a-uuid"]*50},
                    {"schema_version":True},{"shell":"id"},{"source_workbook_sha256":""}]
        for change in variations:
            with self.subTest(change=list(change)),self.assertRaises(scope.ScopeError):
                scope.validate_scope({**original,**change},"external")

    def test_override_keeps_other_service_images_and_injects_only_public_settings(self):
        result=scope.image_override({"backend":"sha256:old","frontend":"sha256:unchanged"},roster("internal"))
        self.assertEqual(result["services"]["frontend"],{"image":"sha256:unchanged"})
        self.assertEqual(set(result["services"]["backend"]["environment"]),{"PUBLIC_RANKING_DIVISION","PUBLIC_RANKING_TEAM_IDS"})
        scope.check_compose_scope(result,roster("internal"))
        result["services"]["backend"]["environment"]["PUBLIC_RANKING_TEAM_IDS"]=""
        with self.assertRaises(scope.ScopeError):scope.check_compose_scope(result,roster("internal"))

    def test_equal_count_with_a_different_team_cannot_pass(self):
        altered=roster();altered["team_ids"][-1]=str(uuid.UUID(int=999))
        with self.assertRaises(scope.ScopeError):scope.validate_proof(proof(),altered)
        for changed in [{"team_count":75},{"configured_roster_count":0},{"api_verified":1},{"database_probe_read_only":False},{"koth_leaderboards_checked":"6"}]:
            with self.subTest(changed=changed),self.assertRaises(scope.ScopeError):scope.validate_proof({**proof(),**changed},roster())

    @unittest.skipUnless(os.name=="posix","POSIX owner and file-mode contract")
    def test_modified_symlinked_or_writable_roster_cannot_be_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/"roster.json";raw=json.dumps(roster()).encode();path.write_bytes(raw);path.chmod(0o600)
            policy={"track":"external","ranking_scope_file":str(path),"ranking_scope_sha256":hashlib.sha256(raw).hexdigest()}
            scope.load_scope(policy,owner=os.getuid())
            path.write_bytes(raw+b" ")
            with self.assertRaises(scope.ScopeError):scope.load_scope(policy,owner=os.getuid())
            path.write_bytes(raw);path.chmod(0o666)
            with self.assertRaises(scope.ScopeError):scope.load_scope(policy,owner=os.getuid())
            path.chmod(0o600);link=root/"linked.json";link.symlink_to(path)
            with self.assertRaises(scope.ScopeError):scope.load_scope({**policy,"ranking_scope_file":str(link)},owner=os.getuid())

    def test_probe_is_a_readonly_process_and_forwards_no_child_secret_output(self):
        calls=[]
        def run(args,**kwargs):
            calls.append((args,kwargs))
            if args[1]=="inspect":return SimpleNamespace(returncode=0,stdout=json.dumps([{"State":{"Running":True},"NetworkSettings":{"Networks":{"web":{"Gateway":"172.20.0.1"}}}}]).encode())
            self.assertIn("PGOPTIONS=-c default_transaction_read_only=on",args)
            return SimpleNamespace(returncode=0,stdout=("not-forwarded-sensitive-output\n"+scope.PREFIX+json.dumps({"status":"VERIFIED",**proof()})).encode())
        result=scope.inspect_running("owned-backend",roster(),"msgctf.kr",run=run)
        self.assertNotIn("not-forwarded",json.dumps(result))
        self.assertEqual(len(calls),2)
        with self.assertRaises(scope.ScopeError):scope.inspect_running("owned-backend",roster(),"other.example",run=run)

    @unittest.skipUnless(os.name=="posix","Administrator installer is POSIX-only")
    def test_persisting_roster_retains_every_unrelated_environment_byte(self):
        from platform_cd.install import persisted_environment
        original=b"PASSWORD=keep-$-literal\nOTHER=value\nPUBLIC_RANKING_DIVISION=WRONG\nPUBLIC_RANKING_TEAM_IDS=old\n"
        changed=persisted_environment(original,roster("internal"))
        self.assertTrue(changed.startswith(b"PASSWORD=keep-$-literal\nOTHER=value\n"))
        self.assertEqual(changed.count(b"PUBLIC_RANKING_DIVISION="),1)
        self.assertIn(b"PUBLIC_RANKING_DIVISION=INTERNAL\n",changed)
        self.assertEqual(changed,persisted_environment(changed,roster("internal")))


class SenderReceiptTests(unittest.TestCase):
    def test_sender_retains_ban_identity_and_exact_configured_population(self):
        value=banned_proof("external")
        result=sender.scope_proof(value)
        self.assertEqual(result["team_count"],49)
        self.assertEqual(result["configured_roster_count"],50)
        self.assertEqual(result["banned_official_team_ids"],value["banned_official_team_ids"])
        for change in [{"team_count":48},{"configured_roster_count":49},
                       {"official_roster_sha256":None},{"banned_official_team_ids":["invalid"]},
                       {"banned_official_team_ids":value["banned_official_team_ids"]*2}]:
            with self.subTest(change=change),self.assertRaises(sender.DeploymentFailure):
                sender.scope_proof({**value,**change})
    def env(self):
        return {"SOURCE_SHA":"a"*40,"DEPLOYMENT_ID":"12345-1","BACKEND_DEPLOY_HOST":"host.example", "BACKEND_DEPLOY_SSH_KEY":"fixture", "BACKEND_DEPLOY_KNOWN_HOSTS":"fixture"}

    def send(self,result,action="deploy"):
        with patch.object(sender.subprocess,"run",return_value=SimpleNamespace(returncode=0,stdout=backend_release.PREFIX+json.dumps(result),stderr="")):
            return sender.send(action,self.env())

    def receipt(self,**changes):
        return {"source_commit":"a"*40,"pipeline_revision":backend_release.CENTRAL_REVISION,"deployment_strategy":"verified_external_platform_image_update","status":"verified","ranking_scope":proof(),**changes}

    def test_a_superseded_run_is_explicitly_skipped_without_claiming_deployment(self):
        result=self.send(self.receipt(status="superseded",current_main_source_commit="b"*40,ranking_scope=None))
        self.assertEqual(result["status"],"superseded")
        self.assertNotIn("ranking_scope",result)
        for current in ["a"*40,"invalid",None]:
            with self.subTest(current=current),self.assertRaises(sender.DeploymentFailure):self.send(self.receipt(status="superseded",current_main_source_commit=current))
        with self.assertRaises(sender.DeploymentFailure):self.send(self.receipt(status="superseded",current_main_source_commit="b"*40),"probe")

    def test_verified_release_requires_a_full_scope_receipt(self):
        self.assertEqual(self.send(self.receipt())["ranking_scope"]["team_count"],50)
        for change in [None,{**proof(),"team_count":75},{**proof(),"division":"INTERNAL"},{**proof(),"configured_roster_count":0}]:
            with self.subTest(proof=change),self.assertRaises(sender.DeploymentFailure):self.send(self.receipt(ranking_scope=change))

    def test_error_codes_are_sanitized_instead_of_forwarding_stderr(self):
        for value,expected in [("BASE_TEMPLATE_CHANGED","BASE_TEMPLATE_CHANGED"),("arbitrary-private-output","UNVERIFIED_RELEASE")]:
            response=SimpleNamespace(returncode=1,stdout="",stderr='private secret\nPLATFORM_DEPLOY_ERROR='+json.dumps({"error_code":value}))
            with patch.object(sender.subprocess,"run",return_value=response),self.assertRaises(sender.DeploymentFailure) as raised:sender.send("deploy",self.env())
            self.assertEqual(raised.exception.code,expected)
            self.assertNotIn("private",str(raised.exception))
