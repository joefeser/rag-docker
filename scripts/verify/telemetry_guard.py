"""Additional service-specific restrictions for the optional disposable collector."""
import json
from pathlib import Path

KEYS = {'profiles', 'mem_limit', 'cpus', 'stop_grace_period', 'read_only', 'logging'}
SERVICES = {'otel-collector', 'otel-capture'}


def check(config, root):
    problems = []
    services = config['services']
    pin = json.loads((Path(root) / 'telemetry/image.json').read_text())['image']
    for name in SERVICES & services.keys():
        svc = services[name]
        expected_image = pin if name == 'otel-collector' else 'rag-verify-api:latest'
        if svc.get('image') != expected_image or 'build' in svc:
            problems.append(f'{name}: unexpected telemetry image/build')
        if (svc.get('profiles') != ['telemetry'] or str(svc.get('mem_limit')) != '268435456'
                or svc.get('cpus') != 0.5 or svc.get('stop_grace_period') != '10s'
                or svc.get('read_only') is not True):
            problems.append(f'{name}: telemetry limits differ from approved bounds')
        if svc.get('logging') != {'driver': 'json-file', 'options': {'max-size': '5m', 'max-file': '2'}}:
            problems.append(f'{name}: unbounded telemetry logs')
        if svc.get('ports') or svc.get('depends_on'):
            problems.append(f'{name}: telemetry has published ports/dependencies')
    for name, svc in services.items():
        if name not in SERVICES and SERVICES & set(svc.get('depends_on') or {}):
            problems.append(f'{name}: collector is in the application dependency chain')
    return problems
