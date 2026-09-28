"""The executable implementation examples must preserve the session boundary."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SessionImplementationTests(unittest.TestCase):
    def test_service_examples_match_validated_implementation(self):
        implementation = (ROOT / 'IMPLEMENTATION.md').read_text()
        for name in ['api/services/goldstandard.py', 'api/services/importer.py',
                     'api/services/packager.py']:
            with self.subTest(path=name):
                header = '### ' + name + '\n\n```python\n'
                start = implementation.index(header) + len(header)
                end = implementation.index('\n```\n', start)
                self.assertEqual(implementation[start:end], (ROOT / name).read_text().rstrip('\n'))


if __name__ == '__main__':
    unittest.main()
