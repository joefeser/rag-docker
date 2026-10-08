"""Ensure the copyable implementation matches the tested telemetry foundation."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
FILES = [('api/main.py', 'python'), ('api/services/telemetry.py', 'python'),
         ('api/requirements.in', 'text'), ('api/requirements.txt', 'text'),
         ('scripts/tests/test_telemetry.py', 'python'),
         ('scripts/tests/test_telemetry_implementation.py', 'python'),
         ('scripts/verify/01_infrastructure.sh', 'bash'),
         ('scripts/verify/README.md', 'markdown')]

class EmbeddedTelemetryTests(unittest.TestCase):
    def test_embedded_files_match(self):
        document = (ROOT/'IMPLEMENTATION.md').read_text()
        for name, language in FILES:
            with self.subTest(name=name):
                fence = '````' if language == 'markdown' else '```'
                # Existing dependency fences are intentionally plain text.
                header = '### '+name+'\n\n'+fence
                start = document.index('\n', document.index(header)+len(header))+1
                end = document.index('\n'+fence+'\n', start)
                self.assertEqual(document[start:end], (ROOT/name).read_text().rstrip('\n'))

if __name__ == '__main__':
    unittest.main()
