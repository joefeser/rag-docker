"""Host-only checks for the verify project harness (#152).

No real Docker command runs here. `docker` and `curl` are replaced by a stub
that records its arguments and returns canned output, so the tests can assert
exactly what scripts/verify/stack.sh and the live-stack guards would ask Docker
to do, and that they refuse before asking anything.

Run: python3 scripts/tests/test_verify_stack.py
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / 'scripts/verify'
STACK = VERIFY / 'stack.sh'

STUB = textwrap.dedent(r'''
    #!/usr/bin/env python3
    """Records argv as one JSON line and answers like a tiny Docker."""
    import json, os, sys
    tool = os.path.basename(sys.argv[0])
    args = sys.argv[1:]
    with open(os.environ['STUB_LOG'], 'a') as log:
        log.write(json.dumps([tool, *args]) + '\n')
    if tool == 'curl':
        print('000', end='')
        sys.exit(0)
    state_path = os.environ['STUB_STATE']
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    def once(key, text):
        if not state.get(key):
            state[key] = True
            json.dump(state, open(state_path, 'w'))
            print(text)
    if args[:1] == ['compose']:
        if 'config' in args:
            print(open(os.environ['STUB_CONFIG']).read())
        elif 'up' in args:
            sys.exit(1)
        elif 'ps' in args:
            print('api exited ')
        sys.exit(0)
    if args[:2] == ['ps', '-aq']:
        once('ps', 'c1')
    elif args[:2] == ['network', 'ls']:
        once('net', 'n1')
    elif args[:2] == ['volume', 'ls']:
        once('vol', 'v1\nrag-verify-ollama-models')
    elif args[:2] == ['image', 'inspect']:
        present = {'rag-verify-api:latest', 'ollama/ollama:0.3.14'} - set(state.get('removed', []))
        sys.exit(0 if args[2] in present else 1)
    elif args[:2] == ['image', 'rm']:
        state['removed'] = state.get('removed', []) + args[2:]
        json.dump(state, open(state_path, 'w'))
    elif args[:2] == ['volume', 'inspect']:
        sys.exit(0)
    elif args[:1] == ['run']:
        print('models: copied=0 repaired=0 removed=0')
    sys.exit(0)
''').lstrip()


def guard_block():
    source = STACK.read_text()
    blocks = re.findall(r"python3 - [^\n]*<<'GUARDPY'\n(.*?)\nGUARDPY", source, re.S)
    assert len(blocks) == 1, 'stack.sh must hold exactly one GUARDPY block'
    return blocks[0]


def passing_config(checkout, exports, port='8081'):
    checkout, exports = str(checkout), str(exports)
    return {
        'name': 'rag-verify',
        'services': {
            'weaviate': {'image': 'semitechnologies/weaviate:1.39.6',
                         'volumes': [{'type': 'volume', 'source': 'weaviate_data',
                                      'target': '/var/lib/weaviate', 'volume': {}}]},
            'ollama': {'image': 'ollama/ollama:0.3.14',
                       'volumes': [{'type': 'volume', 'source': 'ollama_models',
                                    'target': '/root/.ollama', 'volume': {}},
                                   {'type': 'bind', 'source': checkout + '/ollama/entrypoint.sh',
                                    'target': '/entrypoint.sh', 'read_only': True,
                                    'bind': {'create_host_path': True}}]},
            'api': {'image': 'rag-verify-api:latest',
                    'build': {'context': checkout + '/api', 'dockerfile': 'Dockerfile'},
                    'volumes': [{'type': 'volume', 'source': 'ingest_uploads', 'target': '/app/uploads'},
                                {'type': 'volume', 'source': 'rag_sources', 'target': '/app/sources'},
                                {'type': 'bind', 'source': exports, 'target': '/app/exports',
                                 'bind': {'create_host_path': True}},
                                {'type': 'volume', 'source': 'ollama_models', 'target': '/ollama'}]},
            'ui': {'image': 'rag-verify-ui:latest',
                   'build': {'context': checkout + '/ui', 'dockerfile': 'Dockerfile'}},
            'proxy': {'image': 'nginx:1.29-alpine',
                      'ports': [{'mode': 'ingress', 'host_ip': '127.0.0.1', 'target': 80,
                                 'published': port, 'protocol': 'tcp'}],
                      'volumes': [{'type': 'bind', 'source': checkout + '/proxy/nginx.conf',
                                   'target': '/etc/nginx/nginx.conf', 'read_only': True}]},
        },
        'volumes': {
            'weaviate_data': {'name': 'rag-verify_weaviate_data'},
            'ollama_models': {'name': 'rag-verify-ollama-models', 'external': True},
            'ingest_uploads': {'name': 'rag-verify_ingest_uploads'},
            'rag_sources': {'name': 'rag-verify_rag_sources'},
        },
    }


class Sandbox:
    """Scratch TMPDIR, lock path and stubbed docker/curl for one test."""

    def __init__(self, case):
        self.temp = tempfile.TemporaryDirectory()
        case.addCleanup(self.temp.cleanup)
        self.root = Path(os.path.realpath(self.temp.name))
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for tool in ('docker', 'curl'):
            path = self.bin / tool
            path.write_text(STUB)
            path.chmod(0o755)
        self.tmp = self.root / 'tmp'
        self.tmp.mkdir()
        self.log = self.root / 'stub.log'
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('COMPOSE_', 'RAG_'))}
        self.env.update({
            'PATH': f'{self.bin}:{os.environ["PATH"]}',
            'TMPDIR': str(self.tmp),
            'STUB_LOG': str(self.log),
            'STUB_STATE': str(self.root / 'stub.state'),
            'STUB_CONFIG': str(self.root / 'config.json'),
            'RAG_VERIFY_LOCK': str(self.root / 'verify.lock'),
        })

    @property
    def exports(self):
        return self.tmp / 'rag-verify-exports'

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def run(self, argv, cwd=ROOT, **extra):
        env = {**self.env, **extra}
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


class ConfigGuardTests(unittest.TestCase):
    """S13: the resolved configuration is refused unless it is the verify project's."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(os.path.realpath(self.temp.name))
        self.checkout = self.dir / 'checkout'
        self.exports = self.dir / 'rag-verify-exports'
        self.checkout.mkdir()
        self.exports.mkdir()
        self.block = guard_block()

    def guard(self, config, port='8081'):
        path = self.dir / 'config.json'
        path.write_text(json.dumps(config))
        return subprocess.run(['python3', '-c', self.block, str(self.checkout), str(self.exports), port, str(path)],
                              capture_output=True, text=True)

    def test_verify_configuration_passes(self):
        result = self.guard(passing_config(self.checkout, self.exports))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def variants(self):
        def mutate(change):
            config = passing_config(self.checkout, self.exports)
            change(config)
            return config
        S = lambda c: c['services']
        return [
            ('a', 'live volume name', mutate(lambda c: c['volumes']['weaviate_data'].update(name='rag-docker_weaviate_data'))),
            ('a', 'other external volume', mutate(lambda c: c['volumes']['rag_sources'].update(external=True))),
            ('a', 'driver_opts bind', mutate(lambda c: c['volumes']['rag_sources'].update(driver_opts={'type': 'none', 'o': 'bind', 'device': '/Users'}))),
            ('a', 'model volume not external', mutate(lambda c: c['volumes']['ollama_models'].pop('external'))),
            ('b', 'live api image', mutate(lambda c: S(c)['api'].update(image='rag-docker-api:latest'))),
            ('b', 'live image on another service', mutate(lambda c: S(c)['weaviate'].update(image='rag-docker-weaviate:latest'))),
            ('c', 'second port', mutate(lambda c: S(c)['proxy']['ports'].append({'host_ip': '127.0.0.1', 'target': 443, 'published': '8443', 'protocol': 'tcp'}))),
            ('c', 'all interfaces', mutate(lambda c: S(c)['proxy']['ports'][0].update(host_ip='0.0.0.0'))),
            ('c', 'live port', mutate(lambda c: S(c)['proxy']['ports'][0].update(published='8080'))),
            ('c', 'api publishes', mutate(lambda c: S(c)['api'].update(ports=[{'host_ip': '127.0.0.1', 'target': 8000, 'published': '8081', 'protocol': 'tcp'}]))),
            ('d', 'bind outside the checkout', mutate(lambda c: S(c)['ui'].update(volumes=[{'type': 'bind', 'source': '/Users/x/elsewhere', 'target': '/x'}]))),
            ('e', 'docker socket', mutate(lambda c: S(c)['ui'].update(volumes=[{'type': 'bind', 'source': '/var/run/docker.sock', 'target': '/var/run/docker.sock'}]))),
            ('f', 'checkout exports', mutate(lambda c: S(c)['api']['volumes'][2].update(source=str(self.checkout / 'exports')))),
            ('f', 'exports not mounted', mutate(lambda c: S(c)['api']['volumes'].pop(2))),
        ]

    def test_each_rule_refuses_its_adverse_variant(self):
        for rule, name, config in self.variants():
            with self.subTest(rule=rule, variant=name):
                result = self.guard(config)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(f'({rule})', result.stdout + result.stderr)

    def test_port_must_match(self):
        result = self.guard(passing_config(self.checkout, self.exports, port='8082'))
        self.assertEqual(result.returncode, 2)


class LiveGuardTests(unittest.TestCase):
    """S18 and S36: suites and all.sh refuse the live stack unless RAG_VERIFY_LIVE=1."""

    def setUp(self):
        self.box = Sandbox(self)

    def assert_refused(self, result):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('live rag-docker stack', result.stdout + result.stderr)
        self.assertEqual(self.box.calls(), [], 'no docker or curl call may come before the refusal')

    def test_live_project_is_refused(self):
        for script in ('all.sh', '02_ingest.sh'):
            with self.subTest(script=script):
                self.assert_refused(self.box.run(['bash', str(VERIFY / script)],
                                                 COMPOSE_PROJECT_NAME='rag-docker', RAG_API='http://localhost:9/api'))

    def test_live_port_is_refused(self):
        for script in ('all.sh', '02_ingest.sh'):
            with self.subTest(script=script):
                self.assert_refused(self.box.run(['bash', str(VERIFY / script)],
                                                 COMPOSE_PROJECT_NAME='rag-verify', RAG_API='http://localhost:8080/api'))
                self.assert_refused(self.box.run(['bash', str(VERIFY / script)],
                                                 COMPOSE_PROJECT_NAME='rag-verify'))

    def test_verify_target_passes_the_guard(self):
        result = self.box.run(['bash', str(VERIFY / 'all.sh')],
                              COMPOSE_PROJECT_NAME='rag-verify', RAG_API='http://localhost:9/api')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('live rag-docker stack', result.stdout + result.stderr)
        self.assertIn('No healthy API', result.stdout)
        result = self.box.run(['bash', str(VERIFY / '02_ingest.sh')],
                              COMPOSE_PROJECT_NAME='rag-verify', RAG_API='http://localhost:9/api')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Cannot reach a healthy API', result.stdout)

    def test_opt_in_warns_and_continues(self):
        result = self.box.run(['bash', str(VERIFY / 'all.sh')], COMPOSE_PROJECT_NAME='rag-docker',
                              RAG_API='http://localhost:9/api', RAG_VERIFY_LIVE='1')
        self.assertIn('warning', result.stderr.lower())
        self.assertIn('No healthy API', result.stdout)

    def test_folder_name_is_normalised_when_no_project_is_set(self):
        checkout = self.box.root / 'Rag-Docker'
        shutil.copytree(VERIFY, checkout / 'scripts/verify')
        result = self.box.run(['bash', str(checkout / 'scripts/verify/all.sh')], RAG_API='http://localhost:9/api')
        self.assert_refused(result)

    def test_helpers_outside_suites_are_not_guarded(self):
        result = self.box.run(['bash', '-c', f'. "{VERIFY}/lib.sh"; echo sourced-ok'],
                              COMPOSE_PROJECT_NAME='rag-docker')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sourced-ok', result.stdout)


class RestartGuardTests(unittest.TestCase):
    """S19 and D1: restarts never reach rag-docker, whatever RAG_VERIFY_LIVE says."""

    def reason(self, **env):
        box = Sandbox(self)
        result = box.run(['bash', '-c', f'. "{VERIFY}/lib.sh"; restart_refusal_reason'], **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_refusals(self):
        self.assertTrue(self.reason())
        self.assertTrue(self.reason(COMPOSE_PROJECT_NAME='rag-docker'))
        self.assertTrue(self.reason(COMPOSE_PROJECT_NAME='rag-docker', RAG_VERIFY_LIVE='1'))
        self.assertEqual(self.reason(COMPOSE_PROJECT_NAME='rag-verify'), '')

    def test_every_restart_section_asks_first(self):
        for name in ('01_infrastructure.sh', '04_goldstandard.sh', '05_transfer.sh'):
            with self.subTest(suite=name):
                text = (VERIFY / name).read_text()
                block = text[text.index('if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]'):]
                ask = block.index('restart_refusal_reason')
                acts = [m.start() for m in re.finditer(r'docker compose[^\n]*(restart|down|up -d)', block)]
                self.assertTrue(acts, 'restart command not found')
                self.assertLess(ask, min(acts))
        self.assertNotIn('COMPOSE_PROJECT_NAME:-rag-docker', (VERIFY / '01_infrastructure.sh').read_text())


class StackArgumentTests(unittest.TestCase):
    """S7 and S8: bad arguments are refused before any Docker command."""

    def test_refusals_before_docker(self):
        cases = [
            (['up'], {'RAG_VERIFY_PORT': '8080'}),
            (['up'], {'RAG_VERIFY_PORT': 'abc'}),
            (['up'], {'RAG_VERIFY_PORT': '80'}),
            (['up'], {'RAG_VERIFY_PORT': '70000'}),
            (['run'], {'RAG_VERIFY_PORT': '08080'}),
            (['up', '--checkout', '/nonexistent'], {}),
            (['up', '--checkout'], {}),
            (['down', '--pull'], {}),
            (['run', '--pull'], {}),
            (['bogus'], {}),
            ([], {}),
        ]
        for argv, env in cases:
            with self.subTest(argv=argv, env=env):
                box = Sandbox(self)
                result = box.run(['bash', str(STACK), *argv], **env)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(box.calls(), [])


class StackDockerCallTests(unittest.TestCase):
    """S6, S12 and S17: what stack.sh asks Docker to do."""

    ALLOWED_LIVE = {'rag-docker_ollama_models', 'rag-docker_ollama_models:/live:ro'}

    def assert_no_live_object(self, calls):
        for call in calls:
            for arg in call:
                if 'rag-docker' in arg:
                    self.assertIn(arg, self.ALLOWED_LIVE, call)

    def test_down_removes_only_the_verify_project(self):
        box = Sandbox(self)
        box.exports.mkdir()
        (box.exports / 'pkg.tar.gz').write_text('x')
        result = box.run(['bash', str(STACK), 'down'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = box.calls()
        docker = [c[1:] for c in calls if c[0] == 'docker']
        self.assertEqual(docker[0], ['compose', '-p', 'rag-verify', 'down', '-v', '--remove-orphans'])
        label = 'label=com.docker.compose.project=rag-verify'
        for call in docker[1:]:
            listing = call[:2] in (['ps', '-aq'], ['network', 'ls'], ['volume', 'ls'])
            removal = call in (['rm', '-f', 'c1'], ['network', 'rm', 'n1'], ['volume', 'rm', 'v1'],
                               ['image', 'rm', 'rag-verify-api:latest'])
            probe = call[:2] == ['image', 'inspect'] and call[2] in ('rag-verify-api:latest', 'rag-verify-ui:latest')
            self.assertTrue(listing or removal or probe, call)
            if listing:
                self.assertIn(label, call)
        self.assertIn(['image', 'rm', 'rag-verify-api:latest'], docker)
        self.assertNotIn(['volume', 'rm', 'rag-verify-ollama-models'], docker)
        self.assertFalse(any('rag-verify-ollama-models' in c and c[:2] == ['volume', 'rm'] for c in docker))
        self.assertFalse(any('--rmi' in c for c in docker))
        self.assertFalse(box.exports.exists())
        self.assert_no_live_object(calls)

    def test_up_tears_down_first_seeds_read_only_and_retries_once(self):
        box = Sandbox(self)
        (box.root / 'config.json').write_text(json.dumps(passing_config(ROOT, box.exports)))
        result = box.run(['bash', str(STACK), 'up'])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        docker = [c[1:] for c in box.calls() if c[0] == 'docker']
        self.assertEqual(docker[0], ['compose', '-p', 'rag-verify', 'down', '-v', '--remove-orphans'])
        runs = [c for c in docker if c[:1] == ['run']]
        self.assertEqual(len(runs), 1)
        seed = runs[0]
        self.assertIn('--rm', seed)
        self.assertIn('none', seed[seed.index('--network') + 1:seed.index('--network') + 2])
        self.assertIn('rag-docker_ollama_models:/live:ro', seed)
        self.assertIn('rag-verify-ollama-models:/copy', seed)
        compose = [c for c in docker if c[:1] == ['compose']]
        for call in compose:
            self.assertEqual(call[1:3], ['-p', 'rag-verify'], call)
        ups = [c for c in compose if 'up' in c]
        self.assertEqual(len(ups), 2)
        for call in ups:
            self.assertEqual(call[3:], ['up', '-d', '--wait', '--wait-timeout', '900'])
        order = [next(i for i, c in enumerate(docker) if c[:1] == ['run']),
                 next(i for i, c in enumerate(docker) if 'config' in c),
                 next(i for i, c in enumerate(docker) if 'build' in c),
                 next(i for i, c in enumerate(docker) if 'up' in c)]
        self.assertEqual(order, sorted(order), 'seed, guard, build, up must run in that order')
        self.assertIn('logs', [a for c in compose for a in c])
        self.assert_no_live_object(box.calls())

    def test_guard_refusal_stops_before_build(self):
        box = Sandbox(self)
        bad = passing_config(ROOT, box.exports)
        bad['services']['api']['image'] = 'rag-docker-api:latest'
        (box.root / 'config.json').write_text(json.dumps(bad))
        result = box.run(['bash', str(STACK), 'up'])
        self.assertEqual(result.returncode, 2)
        docker = [c[1:] for c in box.calls() if c[0] == 'docker']
        self.assertFalse(any('build' in c or 'up' in c for c in docker if c[:1] == ['compose']))


if __name__ == '__main__':
    unittest.main()
