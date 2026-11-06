"""Deployment decision tests; live read-only API evidence is collected separately."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if os.name == "posix":
    from platform_cd import platform_release as release
    from platform_cd.ranking_scope import ScopeError, image_override
    from test_platform_scope import roster, proof


@unittest.skipUnless(os.name == "posix", "The host deployment receiver is POSIX-only")
class DeploymentTransitionTests(unittest.TestCase):
    def exercise(self, failure=None, action="deploy", drift=False):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);template=root/"compose.json";template.write_text("{}")
        policy={"vm":"test-app","root":str(root),"track":"external","domain":"msgctf.kr","compose_project":"test",
                "base_files":[str(template)],"base_file_sha256":{str(template):hashlib.sha256(template.read_bytes()).hexdigest()},
                "service_containers":{"backend":"active-backend","frontend":"active-frontend"}}
        config=root/"policy.json";config.write_text(json.dumps(policy))
        if drift:template.write_text('{"unexpected":true}')
        request={"action":action,"component":"backend","source_sha":"a"*40,"archive_sha256":hashlib.sha256(b"x").hexdigest(),"archive_bytes":1}
        stdin=SimpleNamespace(buffer=io.BytesIO((json.dumps(request)+"\n").encode()+b"x"))
        state={"active":"old-backend","up":[],"commands":[],"probe_modes":[],"candidate_exists":False,"main_checks":0}
        def response(value=b""):
            return SimpleNamespace(returncode=0,stdout=value if isinstance(value,bytes) else json.dumps(value).encode(),stderr=b"")
        def runner(command,**kwargs):
            state["commands"].append(command)
            if command[0]=="git":
                state['main_checks']+=1
                return response(("b"*40 if failure=="superseded" and state['main_checks']>1 else "a"*40).encode()+b"\trefs/heads/main\n")
            if command[:2]==['docker','image']:
                return response([{"Id":"new-backend","Architecture":"amd64","Config":{"Labels":{"org.opencontainers.image.revision":"a"*40}}}])
            if command[:2]==['docker','inspect']:
                name=command[2]
                if name.startswith('ctf-platform-cd-candidate') and not state['candidate_exists']:
                    return SimpleNamespace(returncode=1,stdout=b'',stderr=b'')
                image=state["active"] if name=="active-backend" else "old-frontend"
                return response([{"Image":image,"State":{"Running":True,"Health":{"Status":"healthy"}},
                                  "HostConfig":{"PortBindings":{}},"NetworkSettings":{"Networks":{"web":{"Aliases":[]}}}}])
            if command[:2]==['docker','compose']:
                if '--detach' in command:state['candidate_exists']=True
                if 'config' in command and '--format' in command:
                    return response(json.loads(Path(command[max(index for index,value in enumerate(command) if value=='-f')+1]).read_text()))
                if 'up' in command:
                    override=Path(command[max(index for index,value in enumerate(command) if value=='-f')+1])
                    state['active']=json.loads(override.read_text())['services']['backend']['image'];state['up'].append(state['active'])
                return response()
            if command[:2]==['docker','rm']:state['candidate_exists']=False
            return response()
        def inspect_scope(container,*args,**kwargs):
            state["probe_modes"].append((container,kwargs.get("mode","public")))
            if failure=="candidate" and container.startswith("ctf-platform-cd-candidate"):
                raise ScopeError("Candidate has an unapproved roster")
            if failure=="public" and container=="active-backend" and state['active']=='new-backend':
                raise ScopeError("Public API includes another team")
            return proof()
        class HTTPS:
            def __init__(self,*args,**kwargs):pass
            def request(self,*args,**kwargs):pass
            def getresponse(self):return SimpleNamespace(status=200,read=lambda limit:b'{"status":"ok","database":"ok","cache":"ok"}')
            def close(self):pass
        patches=[patch.object(release,'CONFIG',config),patch.object(release.os,'geteuid',return_value=0),
                 patch.object(release.socket,'gethostname',return_value='test-app'),patch.object(release.sys,'argv',['release','backend']),
                 patch.object(release.sys,'stdin',stdin),patch.object(release,'load_scope',return_value=roster()),
                 patch.object(release,'inspect_running',side_effect=inspect_scope),patch.object(release.subprocess,'run',side_effect=runner),
                 patch.object(release,'validate_archive',return_value='ctf-platform-backend:'+'a'*40),
                 patch.object(release.http.client,'HTTPSConnection',HTTPS)]
        output=io.StringIO()
        with contextlib.ExitStack() as stack,contextlib.redirect_stdout(output):
            for item in patches:stack.enter_context(item)
            try:release.main();state['result']=json.loads(output.getvalue())
            except Exception as error:state['error']=error
        return state

    def test_candidate_scope_failure_never_replaces_the_live_backend(self):
        state=self.exercise('candidate')
        self.assertIsInstance(state['error'],ScopeError)
        self.assertEqual(state['up'],[])
        self.assertEqual(state['active'],'old-backend')

    def test_public_scope_failure_restores_previous_image_without_database_restore(self):
        state=self.exercise('public')
        self.assertIsInstance(state['error'],ScopeError)
        self.assertEqual(state['up'],['new-backend','old-backend'])
        self.assertEqual(state['active'],'old-backend')
        self.assertFalse(any('--noinput' in command or 'flush' in command for command in state['commands']))

    def test_superseded_revision_after_build_does_not_switch_the_app(self):
        state=self.exercise('superseded')
        self.assertEqual(state['result']['status'],'SUPERSEDED')
        self.assertEqual(state['up'],[])

    def test_template_drift_is_rejected_before_starting_or_loading_an_image(self):
        state=self.exercise(drift=True)
        self.assertIn('template changed',str(state['error']))
        self.assertEqual([command[0] for command in state['commands']],['git'])

    def test_readonly_probe_verifies_current_public_scope_and_runs_no_candidate(self):
        state=self.exercise(action='probe')
        self.assertEqual(state['result']['status'],'PREFLIGHT_VERIFIED')
        self.assertEqual(state['probe_modes'],[('active-backend','public')])
        self.assertFalse(any('up' in command or 'load' in command or 'run' in command for command in state['commands']))

    def test_success_verifies_candidate_then_real_public_api(self):
        state=self.exercise()
        self.assertNotIn('error',state)
        self.assertEqual(state['up'],['new-backend'])
        self.assertEqual(state['result']['ranking_scope']['team_count'],50)
        self.assertEqual(state['probe_modes'],[('ctf-platform-cd-candidate-backend','loopback'),('active-backend','public')])
