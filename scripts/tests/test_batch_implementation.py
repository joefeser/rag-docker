"""Embedded runnable examples must match the completed-write boundary."""
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[2]


class ImplementationTests(unittest.TestCase):
    def test_examples_match_runtime_and_verification(self):
        implementation = (ROOT / 'IMPLEMENTATION.md').read_text()
        files = [(name, '```', 'python') for name in ('api/main.py', 'api/services/weaviate_client.py',
                 'api/services/batch_write.py', 'api/services/collection_recovery.py',
                 'api/services/importer.py', 'api/services/tuning.py')]
        files += [(name, '```', 'bash') for name in ('scripts/verify/01_infrastructure.sh', 'scripts/verify/05_transfer.sh')]
        files += [('scripts/verify/README.md', '````', 'markdown'), ('scripts/verify/batch_recovery.py', '```', 'python')]
        for name, fence, language in files:
            with self.subTest(file=name):
                header = '### ' + name + '\n\n' + fence + language + '\n'
                start = implementation.index(header) + len(header)
                end = implementation.index('\n' + fence + '\n', start)
                self.assertEqual(implementation[start:end], (ROOT / name).read_text().rstrip('\n'))


if __name__ == '__main__':
    unittest.main()
